"""num_ctx を 16384 へ上げて大丈夫か、実際に載せて確かめる。

## なぜ確かめるのか

上げたい理由ははっきりしている。実ログでは毎ターン履歴が捨てられている。

    文脈長のため古い会話を24件除外 (見積8651tok / 予算6912tok / ctx=8192)

systemブロックだけで5000〜6000トークンあるので、8192では会話が1000トークン
ぶんしか残らない。24往復ぶん捨てているなら、ポッポが少し前の話を
覚えていないのは当然である。

危ないのは**VRAM**だけ。num_ctx を倍にするとKVキャッシュも増え、
GPUに載りきらなくなるとOllamaは層をCPUへ追い出す。そうなると
「文脈は伸びたが応答は数倍遅い」という、いちばん困る状態になる。

数字で決められる話なので、推測せずに測る。

    python tools/check_context_length.py

## 読み方

`size` と `size_vram` が一致していれば全部GPUに載っている。
16384 でも一致するなら上げてよい。ずれたらCPUへこぼれている。

## 上げると決めたら

`config/config.yaml` の `llm.num_ctx` だけでは効かない。
**`OLLAMA_CONTEXT_LENGTH` も同じ値にする**（`SetupOllamaEnv.bat` を実行し直す）。
サーバ側の上限で頭打ちになるため。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:11434"
MODEL = "gemma4:12b-it-qat"
SIZES = (8192, 16384)
#: 実際の会話に近い長さの入力。空プロンプトではKVキャッシュがほぼ使われない。
FILLER = "爆弾解除の話をしている。" * 200


def post(path: str, body: dict, timeout: float = 300.0) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get(path: str, timeout: float = 30.0) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def megabytes(value: int) -> str:
    return f"{value / 1024 / 1024:,.0f}MB"


def load_at(size: int) -> dict:
    """その num_ctx でモデルを載せ、載り方と初回応答の速さを見る。"""
    started = time.perf_counter()
    post("/api/chat", {
        "model": MODEL, "stream": False, "think": False,
        "keep_alive": "5m",
        "messages": [{"role": "user", "content": FILLER + "\n短く返して。"}],
        "options": {"num_ctx": size, "num_predict": 16},
    })
    elapsed = (time.perf_counter() - started) * 1000
    running = get("/api/ps").get("models") or []
    entry = next((item for item in running if MODEL in str(item.get("name", ""))), None)
    if entry is None:
        return {"size": 0, "vram": 0, "ms": elapsed}
    return {
        "size": int(entry.get("size", 0) or 0),
        "vram": int(entry.get("size_vram", 0) or 0),
        "ms": elapsed,
    }


def unload() -> None:
    """次の計測へ影響を残さないよう、いったん降ろす。

    降ろせなくても計測は続ける。失敗しても分かるのは「前の num_ctx の
    ままだった」だけで、その時は数字が同じになるので気づける。
    """
    try:
        post("/api/chat", {"model": MODEL, "messages": [], "keep_alive": 0}, timeout=60)
    except Exception:
        pass


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")
    print(f"モデル: {MODEL}")
    print("実ログでは毎ターン最大24件の履歴が文脈長のため捨てられている。")
    print("16384へ上げてもGPUに載りきるかを見る。\n")
    results = {}
    try:
        for size in SIZES:
            unload()
            time.sleep(1.0)
            result = load_at(size)
            results[size] = result
            fits = result["size"] and result["size"] == result["vram"]
            print(
                f"num_ctx={size:>6}: 合計={megabytes(result['size'])} / "
                f"VRAM={megabytes(result['vram'])} / 初回={result['ms']:.0f}ms "
                f"— {'全部GPU' if fits else '★CPUへこぼれている'}"
            )
    except urllib.error.URLError as error:
        print(f"Ollamaへ接続できませんでした: {error}")
        return 1
    except Exception as error:                                   # pragma: no cover
        print(f"計測に失敗: {error}")
        return 1

    big = results.get(16384) or {}
    small = results.get(8192) or {}
    print()
    if big.get("size") and big["size"] == big["vram"]:
        extra = big["size"] - (small.get("size") or 0)
        print(
            f"16384でも全部GPUに載る（増えたのは{megabytes(max(0, extra))}）。\n"
            "上げてよい。**`config/config.yaml` の `llm.num_ctx` だけでは効かない**ので、\n"
            "`OLLAMA_CONTEXT_LENGTH` も 16384 にすること（SetupOllamaEnv.bat を実行し直す）。"
        )
    else:
        print(
            "16384ではCPUへこぼれる。上げると文脈は伸びるが応答が数倍遅くなる。\n"
            "8192のままにして、代わりに systemブロック（5000〜6000tok）を削る方へ回すこと。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
