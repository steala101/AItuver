"""遠隔API (スマートフォン用)。Tailscale IP限定・認証つきHTTPサーバー。

指示書14章 (API)、15章 (長時間処理=operation)、17章 (認証)、18章 (セキュリティ)。
標準ライブラリのみで実装し、ランチャーを軽量に保つ。
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from neuro_voice.remote.launcher import Controller, LauncherConfig
from neuro_voice.remote.state import StateError

logger = logging.getLogger("remote_launcher.api")

_SNOWFLAKE_MAX = 32
_MAX_BODY = 4096

# PWA静的ファイルは固定ディレクトリの許可リストからのみ配信する
# (ディレクトリトラバーサル防止。認証は不要=秘匿情報を含まないため)。
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/manifest.json": ("manifest.json", "application/manifest+json; charset=utf-8"),
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/favicon.ico": ("icon.svg", "image/svg+xml"),
}


def _valid_id(value: str) -> bool:
    return value == "" or (value.isdigit() and len(value) <= _SNOWFLAKE_MAX)


# ---------- セッション・レート制限・操作管理 ----------

class Sessions:
    def __init__(self, ttl_minutes: int):
        self._ttl = ttl_minutes * 60
        self._data: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._data[sid] = time.time() + self._ttl
        return sid

    def valid(self, sid: str) -> bool:
        if not sid:
            return False
        with self._lock:
            exp = self._data.get(sid)
            if exp is None:
                return False
            if exp <= time.time():
                self._data.pop(sid, None)
                return False
            return True

    def drop(self, sid: str) -> None:
        with self._lock:
            self._data.pop(sid, None)


class RateLimiter:
    """IPごとの固定窓レート制限 (1分)。"""

    def __init__(self, per_minute: int):
        self._limit = max(1, per_minute)
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < 60.0]
            if len(hits) >= self._limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


class Operations:
    """長時間処理の進捗管理 (PENDING/RUNNING/SUCCEEDED/FAILED)。"""

    def __init__(self):
        self._ops: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, name: str, fn: Callable[[], dict]) -> str:
        op_id = secrets.token_urlsafe(12)
        with self._lock:
            self._ops[op_id] = {"state": "PENDING", "name": name, "result": None,
                                "created_at": time.time()}

        def _run():
            with self._lock:
                self._ops[op_id]["state"] = "RUNNING"
            try:
                result = fn()
                state = "SUCCEEDED" if result.get("ok", True) else "FAILED"
                with self._lock:
                    self._ops[op_id].update(state=state, result=result)
            except StateError as e:
                with self._lock:
                    self._ops[op_id].update(state="FAILED", result={"ok": False, "message": str(e)})
            except Exception:
                logger.exception("操作 %s でエラー", name)
                with self._lock:
                    self._ops[op_id].update(
                        state="FAILED", result={"ok": False, "message": "内部エラー"})

        threading.Thread(target=_run, name=f"op-{name}", daemon=True).start()
        return op_id

    def get(self, op_id: str) -> dict | None:
        with self._lock:
            op = self._ops.get(op_id)
            return dict(op) if op else None


# ---------- HTTPサーバー ----------

class LauncherApi:
    def __init__(self, controller: Controller, cfg: LauncherConfig):
        self.controller = controller
        self.cfg = cfg
        self.sessions = Sessions(cfg.session_timeout_minutes)
        self.rate = RateLimiter(cfg.rate_limit)
        self.ops = Operations()
        self._httpd: ThreadingHTTPServer | None = None

    def serve(self, host: str, port: int) -> None:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((host, port), handler)
        logger.info("遠隔API を起動: http://%s:%d", host, port)
        self._httpd.serve_forever()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def check_admin(self, token: str) -> bool:
        if not self.cfg.admin_token:
            return False
        # バイト列で比較する。スマホ入力で非ASCII文字 (全角・スマートクォート等)
        # が紛れても compare_digest が例外を投げず、単に不一致(False)になる。
        # 前後の空白/改行はコピペ時に混ざりやすいので取り除いてから比較する。
        try:
            return hmac.compare_digest(
                (token or "").strip().encode("utf-8"),
                self.cfg.admin_token.encode("utf-8"),
            )
        except Exception:
            return False


def _make_handler(api: "LauncherApi"):
    cfg = api.cfg

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):  # noqa: N802 - Authorization等をログに残さない
            return

        # ----- 応答 -----

        def _headers(self, extra: dict | None = None) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            )
            for k, v in (extra or {}).items():
                self.send_header(k, v)

        def _json(self, code: int, payload: dict, extra: dict | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._headers(extra)
            self.end_headers()
            self.wfile.write(body)

        # ----- 補助 -----

        @property
        def client_ip(self) -> str:
            return self.client_address[0] if self.client_address else "unknown"

        def _cookie_session(self) -> str:
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    if k == "nvsession":
                        return v
            return ""

        def _authed(self) -> bool:
            # Cookie セッション、または Authorization: Bearer <admin_token>
            if api.sessions.valid(self._cookie_session()):
                return True
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return api.check_admin(auth[7:].strip())
            return False

        def _csrf_ok(self) -> bool:
            # Cookie利用時のCSRF緩和: 独自ヘッダを必須にする (単純リクエストで送れない)
            return self.headers.get("X-Requested-With", "") == "NeuroLauncher"

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            if length > _MAX_BODY:
                raise ValueError("body too large")
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8")) if raw else {}

        def _rate_ok(self) -> bool:
            if not api.rate.allow(self.client_ip):
                self._json(429, {"error": "リクエストが多すぎます。しばらく待ってください"})
                return False
            return True

        # ----- GET -----

        def _serve_static(self, path: str) -> bool:
            # path は固定の許可リストのキーのみ受理する (ユーザー入力をパスに使わない
            # ため、ディレクトリトラバーサルは原理的に発生しない)。
            entry = _STATIC_FILES.get(path)
            if entry is None:
                return False
            filename, content_type = entry
            try:
                data = (_STATIC_DIR / filename).read_bytes()
            except OSError:
                self._json(404, {"error": "not found"})
                return True
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            # Service Worker はルートスコープで動く必要があるためキャッシュ短め
            if filename == "sw.js":
                self.send_header("Cache-Control", "no-cache")
            self._headers()
            self.end_headers()
            self.wfile.write(data)
            return True

        def do_GET(self):  # noqa: N802
            # 静的ファイル (PWA) は認証不要で配信する
            path = self.path.split("?", 1)[0]
            if self._serve_static(path):
                return
            if self.path == "/api/health":
                return self._json(200, {"launcher_running": True})
            # GET(状態取得/操作ポーリング)はレート制限しない。画面が数秒ごとに
            # 状態を取得するため、ここを絞ると正常利用でも詰まってしまう。
            # レート制限は状態変更のPOST(ログイン含む)側だけで行う。
            if not self._authed():
                return self._json(401, {"error": "認証が必要です"})
            if self.path == "/api/status":
                return self._json(200, {"launcher_running": True,
                                        **api.controller.state.snapshot()})
            if self.path == "/api/discord/channels":
                return self._json(200, api.controller.channels())
            if self.path == "/api/personas":
                return self._json(200, api.controller.personas())
            if self.path == "/api/speakers":
                return self._json(200, api.controller.speakers())
            if self.path == "/api/mind":
                return self._json(200, api.controller.mind_status())
            if self.path == "/api/stt":
                return self._json(200, api.controller.stt_status())
            if self.path == "/api/tts":
                return self._json(200, api.controller.tts_status())
            if self.path == "/api/runtime":
                return self._json(200, api.controller.runtime_info())
            if self.path.startswith("/api/operations/"):
                op = api.ops.get(self.path.rsplit("/", 1)[-1])
                if op is None:
                    return self._json(404, {"error": "不明な操作IDです"})
                return self._json(200, {"state": op["state"], "result": op["result"]})
            return self._json(404, {"error": "not found"})

        # ----- POST -----

        def do_POST(self):  # noqa: N802
            if not self._rate_ok():
                return
            try:
                body = self._read_json()
            except Exception:
                return self._json(400, {"error": "不正なリクエストです"})

            if self.path == "/api/login":
                token = str(body.get("token", ""))
                if not api.check_admin(token):
                    return self._json(401, {"error": "認証に失敗しました"})
                sid = api.sessions.create()
                cookie = (
                    f"nvsession={sid}; HttpOnly; SameSite=Strict; Path=/; "
                    f"Max-Age={cfg.session_timeout_minutes * 60}"
                )
                return self._json(200, {"ok": True}, extra={"Set-Cookie": cookie})

            if not self._authed():
                return self._json(401, {"error": "認証が必要です"})
            if not self._csrf_ok():
                return self._json(403, {"error": "CSRF検証に失敗しました"})

            if self.path == "/api/logout":
                api.sessions.drop(self._cookie_session())
                return self._json(200, {"ok": True},
                                  extra={"Set-Cookie": "nvsession=; Max-Age=0; Path=/"})

            # ペルソナ/話者の切替は短時間で終わるので即時実行 (operation不要)
            if self.path == "/api/persona":
                key = str(body.get("key", ""))
                if not re.match(r"^[A-Za-z0-9_-]{1,40}$", key):
                    return self._json(400, {"error": "ペルソナ指定が不正です"})
                return self._json(200, api.controller.set_persona(key, self.client_ip))
            if self.path == "/api/speaker":
                sid = str(body.get("speaker_id", ""))
                if len(sid) > 80:
                    return self._json(400, {"error": "話者指定が不正です"})
                return self._json(200, api.controller.set_speaker(sid, self.client_ip))

            # STT/TTSの設定変更。エンジン/デバイス切替はモデル再読込で数秒かかるが、
            # サーバーはスレッド化されており状態ポーリングは詰まらない。音量は即時。
            if self.path == "/api/stt/device":
                device = str(body.get("device", "")).strip().lower()
                if device not in ("cuda", "cpu"):
                    return self._json(400, {"error": "device は cuda か cpu を指定してください"})
                return self._json(200, api.controller.set_stt_device(device, self.client_ip))
            if self.path == "/api/tts/volume":
                try:
                    vol = float(body.get("volume"))
                except (TypeError, ValueError):
                    return self._json(400, {"error": "音量の値が不正です"})
                if not (0.0 <= vol <= 2.0):
                    return self._json(400, {"error": "音量は0.0〜2.0で指定してください"})
                return self._json(200, api.controller.set_tts_volume(vol, self.client_ip))
            if self.path == "/api/tts/engine":
                engine = str(body.get("engine", "")).strip().lower()
                if engine not in ("voicevox", "style_bert_vits2", "sbv2"):
                    return self._json(400, {"error": "engine の指定が不正です"})
                return self._json(200, api.controller.set_tts_engine(engine, self.client_ip))

            ip = self.client_ip
            gid = str(body.get("guild_id", ""))
            cid = str(body.get("channel_id", ""))
            if self.path in ("/api/discord/join", "/api/actions/start-and-join"):
                if not (_valid_id(gid) and _valid_id(cid)):
                    return self._json(400, {"error": "guild_id / channel_id の形式が不正です"})

            routes = {
                "/api/app/start": ("start_app", lambda: api.controller.start_app(ip)),
                "/api/app/stop": ("stop_app", lambda: api.controller.stop_app(ip)),
                "/api/discord/join": ("join", lambda: api.controller.join(cid, ip)),
                "/api/discord/leave": ("leave", lambda: api.controller.leave(ip)),
                "/api/actions/start-and-join":
                    ("start_and_join", lambda: api.controller.start_and_join(cid, ip)),
                "/api/actions/leave-and-stop":
                    ("leave_and_stop", lambda: api.controller.leave_and_stop(ip)),
            }
            if self.path in routes:
                name, fn = routes[self.path]
                op_id = api.ops.start(name, fn)
                return self._json(202, {"accepted": True, "operation_id": op_id,
                                        "state": "PENDING"})
            return self._json(404, {"error": "not found"})

    return Handler
