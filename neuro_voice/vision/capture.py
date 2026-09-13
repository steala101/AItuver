"""mss による画面キャプチャ。縮小JPEGのbase64を返す。

monitor 設定:
  auto  … 最前面ウィンドウ (自アプリならマウスカーソル) があるモニタを撮る。
          デュアルモニタで「一緒に動画を見る」用途はこれが正解。
  0     … 全モニタを結合した仮想画面
  1,2.. … 固定のモニタ番号 (mssの番号)
"""
from __future__ import annotations

import base64
import contextlib
import io
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class CaptureFramePending(RuntimeError):
    """The source is alive, but has not delivered a fresh compositor frame yet."""


def _process_image_name(pid: int) -> str:
    """Best-effort executable name for titleless exclusive-fullscreen windows."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return f"process {pid}"
        try:
            size = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                return Path(path.value).name or f"process {pid}"
        finally:
            kernel32.CloseHandle(process)
    except Exception:
        logger.debug("Unable to resolve window process name", exc_info=True)
    return f"process {pid}"


def _is_likely_game_process(name: str) -> bool:
    normalized = (name or "").casefold()
    return normalized in {
        "javaw.exe", "java.exe", "minecraft.exe", "minecraftlauncher.exe",
        "prismlauncher.exe", "multimc.exe", "atlauncher.exe",
    }


def describe_capture_window(window_handle: int | str) -> dict[str, str]:
    """Return a stable picker entry for a visible foreground/game window."""
    if os.name != "nt":
        raise RuntimeError("Window capture is available on Windows only")
    import ctypes
    from ctypes import wintypes

    handle = int(getattr(window_handle, "value", window_handle) or 0)
    user32 = ctypes.windll.user32
    if not user32.IsWindow(handle):
        raise RuntimeError("Selected game window is no longer available")
    rect = wintypes.RECT()
    if not user32.GetWindowRect(handle, ctypes.byref(rect)):
        raise RuntimeError("Unable to read selected game window bounds")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 240 or height < 160:
        raise RuntimeError("Selected window is too small for game capture")
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    length = user32.GetWindowTextLengthW(handle)
    title = ctypes.create_unicode_buffer(max(1, length + 1))
    if length:
        user32.GetWindowTextW(handle, title, len(title))
    name = title.value.strip() or f"[{_process_image_name(pid.value)} / fullscreen or untitled]"
    return {"handle": str(handle), "title": f"{name} — {width}×{height}"}


def list_capture_windows(*, include_minimized_games: bool = True) -> list[dict[str, str]]:
    """Return capturable windows plus minimized games for the local picker."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    found: list[dict[str, str]] = []
    own_pid = os.getpid()

    @enum_proc
    def visit(hwnd, _lparam):
        handle = int(getattr(hwnd, "value", hwnd) or 0)
        if not handle:
            return True
        iconic = bool(user32.IsIconic(hwnd))
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True
        process_name = _process_image_name(pid.value)
        # Hidden helper/render HWNDs are not valid Windows Graphics Capture
        # targets.  Including them made the Minecraft auto-picker lock onto an
        # invisible, frozen surface instead of the actual game window.
        if not user32.IsWindowVisible(hwnd):
            return True
        # Skip owned popups unless this is a known game process in a special fullscreen mode.
        if user32.GetWindow(hwnd, 4):  # GW_OWNER
            if not _is_likely_game_process(process_name):
                return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width, height = rect.right - rect.left, rect.bottom - rect.top
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(max(1, length + 1))
        if length:
            user32.GetWindowTextW(hwnd, title, len(title))
        name = title.value.strip()
        if not name:
            name = f"[{process_name} / fullscreen or untitled]"
        is_game = _is_likely_game_process(process_name) or "minecraft" in name.casefold()
        if iconic:
            if not include_minimized_games or not is_game:
                return True
            found.append({
                "handle": str(handle),
                "title": f"{name} [最小化中：ゲームへ戻ると自動取得]",
                "capture_ready": False,
                "state": "minimized",
            })
            return True
        if width < 240 or height < 160:
            return True
        if handle:
            found.append({
                "handle": str(handle), "title": f"{name} — {width}×{height}",
                "capture_ready": True, "state": "ready",
            })
        return True

    if not user32.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return sorted(found, key=lambda item: item["title"].casefold())


def find_game_window(profile: str) -> dict[str, str] | None:
    """Return one unambiguous running game window for automatic capture."""
    normalized = str(profile or "").strip().casefold()
    if normalized != "minecraft":
        return None
    candidates = list_capture_windows(include_minimized_games=False)
    minecraft = [item for item in candidates if "minecraft" in str(item.get("title", "")).casefold()]
    if len(minecraft) == 1:
        return minecraft[0]
    if minecraft:
        # Prefer a real titled game window.  A process can expose hidden or
        # helper HWNDs alongside it, but list_capture_windows deliberately
        # removes those because WGC cannot capture them.
        titled = [item for item in minecraft if not str(item.get("title", "")).startswith("[")]
        if titled:
            return max(titled, key=lambda item: _window_entry_area(item))
        return max(minecraft, key=lambda item: _window_entry_area(item))
    # Java Edition often has a titleless exclusive-fullscreen HWND. The picker
    # exposes it as javaw.exe; use it only if it is the sole Java game window.
    java = [
        item for item in candidates
        if "javaw.exe" in str(item.get("title", "")).casefold()
        or "java.exe" in str(item.get("title", "")).casefold()
    ]
    return java[0] if len(java) == 1 else None


def _window_entry_area(item: dict[str, str]) -> int:
    """Best-effort area parser for the picker label (falls back to zero)."""
    import re

    match = re.search(r"(\d+)\D+(\d+)\s*$", str(item.get("title", "")))
    if match is None:
        return 0
    return int(match.group(1)) * int(match.group(2))


def _window_client_rect(hwnd: int) -> dict[str, int]:
    """Resolve a window client area into an mss rectangle."""
    if os.name != "nt":
        raise RuntimeError("Window capture is available on Windows only")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    # Exclusive/fullscreen games can own a valid foreground HWND which Windows
    # reports as not normally visible.  Reject a destroyed or minimized window,
    # but do not reject that fullscreen presentation mode.
    if not user32.IsWindow(hwnd):
        raise RuntimeError("Selected game window is no longer available")
    if user32.IsIconic(hwnd):
        raise RuntimeError("Selected game window is minimized")
    client = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(client)):
        raise RuntimeError("Unable to read selected game window bounds")
    top_left = wintypes.POINT(client.left, client.top)
    bottom_right = wintypes.POINT(client.right, client.bottom)
    if not user32.ClientToScreen(hwnd, ctypes.byref(top_left)) or not user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
        raise RuntimeError("Unable to resolve selected game window position")
    width = bottom_right.x - top_left.x
    height = bottom_right.y - top_left.y
    if width < 32 or height < 32:
        raise RuntimeError("Selected game window has no visible client area")
    return {"left": top_left.x, "top": top_left.y, "width": width, "height": height}


def _foreground_monitor(monitors: list[dict]) -> dict | None:
    """最前面ウィンドウがあるモニタを返す (Windows専用。失敗時 None)。

    最前面が自分自身 (このGUI) の場合は、マウスカーソルのあるモニタを使う。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        def monitor_at(x: int, y: int) -> dict | None:
            for mon in monitors[1:]:
                if (mon["left"] <= x < mon["left"] + mon["width"]
                        and mon["top"] <= y < mon["top"] + mon["height"]):
                    return mon
            return None

        hwnd = user32.GetForegroundWindow()
        if hwnd:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != os.getpid():
                rect = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    mon = monitor_at(
                        (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
                    )
                    if mon is not None:
                        return mon
        # 自分のウィンドウが最前面 (テキスト入力中など) → カーソル位置のモニタ
        pt = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            return monitor_at(pt.x, pt.y)
    except Exception:
        logger.debug("最前面モニタの特定に失敗", exc_info=True)
    return None


class ScreenCapture:
    """指定モニタのスクリーンショットを撮り、LLMに渡せる形式にする。"""

    def __init__(
        self,
        monitor: int | str = "auto",
        max_width: int = 1024,
        jpeg_quality: int = 70,
        save_last: bool = False,
        save_dir: str = "logs",
    ):
        self._monitor = monitor
        self._max_width = int(max_width)
        self._quality = int(jpeg_quality)
        self._save_last = bool(save_last)
        self._save_dir = Path(save_dir)
        self._dark_warned = False

    def _pick_monitor(self, monitors: list[dict]) -> dict:
        m = self._monitor
        if isinstance(m, str) and m.strip().lower() == "auto":
            mon = _foreground_monitor(monitors)
            if mon is not None:
                return mon
            return monitors[1]
        try:
            idx = int(m)
        except (TypeError, ValueError):
            idx = 1
        if 0 <= idx < len(monitors):
            return monitors[idx]  # 0 = 全モニタ結合
        return monitors[1]

    def capture_jpeg_b64(self) -> str:
        """スクリーンショットを撮影し、JPEGのbase64文字列を返す。"""
        return base64.b64encode(self.capture_media().data or b"").decode("ascii")

    def capture_media(self):
        """Capture a normalized image without exposing base64 to LLM callers."""
        import mss
        from PIL import Image, ImageStat
        from neuro_voice.media import MediaType
        from neuro_voice.vision.media import prepare_image

        start = time.perf_counter()
        with mss.mss() as sct:
            shot = sct.grab(self._pick_monitor(sct.monitors))
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        # ほぼ真っ黒なら警告 (DRM保護動画やフルスクリーン排他はGDIで黒くなる)
        brightness = sum(ImageStat.Stat(img).mean) / 3
        if brightness < 10 and not self._dark_warned:
            self._dark_warned = True
            logger.warning(
                "キャプチャがほぼ真っ黒です (平均輝度 %.1f)。DRM保護された動画 "
                "(Netflix/Prime等) やフルスクリーン排他モードは撮影できません。"
                "ブラウザのハードウェアアクセラレーションOFFやウィンドウ表示を試してください",
                brightness,
            )
        elif brightness >= 10:
            self._dark_warned = False

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        media = prepare_image(
            buf.getvalue(), max_width=self._max_width, max_height=self._max_width,
            jpeg_quality=self._quality, media_type=MediaType.IMAGE, timestamp=time.time(),
        )
        data = media.data or b""

        if self._save_last:
            try:
                self._save_dir.mkdir(parents=True, exist_ok=True)
                (self._save_dir / "last_capture.jpg").write_bytes(data)
            except Exception:
                logger.debug("last_capture.jpg の保存に失敗", exc_info=True)

        logger.debug(
            "画面キャプチャ %.0fms (%dx%d, %.0fKB)",
            (time.perf_counter() - start) * 1000, media.metadata.get("width", 0), media.metadata.get("height", 0), len(data) / 1024,
        )
        return media


class WindowCapture:
    """Capture the selected HWND itself, including occluded game windows."""

    def __init__(
        self, window_handle: int | str, *, max_width: int = 1024, jpeg_quality: int = 70,
        backend: str = "auto", auto_rebind_game: bool = False,
    ):
        try:
            self._window_handle = int(getattr(window_handle, "value", window_handle) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("A game window must be selected") from exc
        self._max_width = int(max_width)
        self._quality = int(jpeg_quality)
        self._backend = str(backend).strip().lower()
        if self._backend not in {"auto", "directx", "monitor-winrt", "winrt", "wgc", "gdi"}:
            self._backend = "auto"
        self._wgc_capture = None
        self._wgc_control = None
        self._wgc_frame = None
        self._wgc_sequence = 0
        self._wgc_timestamp = 0.0
        self._wgc_monotonic = 0.0
        self._wgc_error: str | None = None
        self._wgc_condition = threading.Condition()
        self._dx_camera = None
        self._dx_output_index: int | None = None
        self._dx_backend: str | None = None
        self._dx_region: tuple[int, int, int, int] | None = None
        self._dx_frame_sequence = 0
        self._dx_source_timestamp: float | None = None
        self._dx_source_updated_monotonic = 0.0
        self._directx_warned = False
        self._capture_sequence = 0
        self._auto_rebind_game = bool(auto_rebind_game)

    def _client_rect(self) -> dict[str, int]:
        """Resolve the selected window and recover after a game recreates its HWND."""
        try:
            return _window_client_rect(self._window_handle)
        except RuntimeError:
            if not self._auto_rebind_game:
                raise
        # Fullscreen Java games may replace their HWND when changing display
        # mode. Rebind only when there is one unambiguous game candidate.
        candidates = [
            item for item in list_capture_windows(include_minimized_games=False)
            if "minecraft" in str(item.get("title", "")).casefold()
            or "javaw.exe" in str(item.get("title", "")).casefold()
            or "java.exe" in str(item.get("title", "")).casefold()
        ]
        minecraft = [item for item in candidates if "minecraft" in str(item.get("title", "")).casefold()]
        candidates = minecraft or candidates
        if len(candidates) != 1:
            raise RuntimeError(
                "Selected game window is no longer available. Select Minecraft again in Settings."
            )
        rebound = int(candidates[0]["handle"])
        if rebound == self._window_handle:
            raise RuntimeError("Selected game window is no longer available")
        logger.warning("Minecraft window was recreated; rebound capture handle %s -> %s", self._window_handle, rebound)
        self._window_handle = rebound
        if self._dx_camera is not None:
            with contextlib.suppress(Exception):
                release = getattr(self._dx_camera, "release", None)
                (release() if callable(release) else self._dx_camera.stop())
        self._dx_camera = None
        self._dx_output_index = None
        return _window_client_rect(self._window_handle)

    def _encode_image(self, image):
        from neuro_voice.media import MediaType
        from neuro_voice.vision.media import prepare_image

        encoded = io.BytesIO()
        image.save(encoded, format="PNG")
        return prepare_image(
            encoded.getvalue(), max_width=self._max_width, max_height=self._max_width,
            jpeg_quality=self._quality, media_type=MediaType.IMAGE, timestamp=time.time(),
        )

    def _with_capture_metadata(
        self, media, *, backend: str, output_index: int | None = None,
        source_sequence: int | None = None, captured_monotonic: float | None = None,
    ):
        """Stamp every frame so callers can distinguish a new capture from history."""
        if source_sequence is None:
            self._capture_sequence += 1
            source_sequence = self._capture_sequence
        else:
            self._capture_sequence = max(self._capture_sequence, source_sequence)
        captured_monotonic = captured_monotonic or time.monotonic()
        metadata = {
            **media.metadata,
            "capture_backend": backend,
            "capture_sequence": source_sequence,
            "capture_handle": str(self._window_handle),
            "captured_monotonic": captured_monotonic,
            "frame_age_ms": max(0.0, (time.monotonic() - captured_monotonic) * 1000.0),
        }
        if output_index is not None:
            metadata["capture_output_index"] = output_index
        return type(media)(
            media_type=media.media_type, data=media.data, mime_type=media.mime_type,
            timestamp=media.timestamp, metadata=metadata,
        )

    def _start_wgc(self) -> None:
        """Start one persistent Windows Graphics Capture session for this HWND."""
        if self._wgc_capture is not None:
            return
        from windows_capture import WindowsCapture

        capture = WindowsCapture(
            # ``None`` maps to CursorCaptureSettings::Default.  Do not
            # explicitly toggle the WGC cursor property: GLFW/OpenGL games can
            # own and confine the cursor themselves, and forcing either False
            # or True may interfere with that ownership on some drivers.
            cursor_capture=None,
            draw_border=None,
            minimum_update_interval=16,
            window_hwnd=self._window_handle,
        )

        def on_frame_arrived(frame, _control):
            # The native callback owns its buffer.  Copy it before returning or
            # subsequent frames can mutate an image while it is being encoded.
            bgra = frame.frame_buffer
            rgb = bgra[:, :, [2, 1, 0]].copy()
            with self._wgc_condition:
                self._wgc_frame = rgb
                self._wgc_sequence += 1
                self._wgc_timestamp = time.time()
                self._wgc_monotonic = time.monotonic()
                self._wgc_error = None
                self._wgc_condition.notify_all()

        def on_closed():
            with self._wgc_condition:
                self._wgc_error = "Selected game window was closed"
                self._wgc_condition.notify_all()

        capture.frame_handler = on_frame_arrived
        capture.closed_handler = on_closed
        try:
            control = capture.start_free_threaded()
        except Exception:
            self._wgc_capture = None
            self._wgc_control = None
            raise
        self._wgc_capture = capture
        self._wgc_control = control

    def _capture_wgc(self):
        """Return the newest compositor frame from the selected HWND."""
        from PIL import Image

        self._start_wgc()
        with self._wgc_condition:
            previous = self._wgc_sequence
            # Usually a current frame is already available.  If it has aged,
            # wait briefly for the next compositor callback instead of showing
            # a frame from before the user's question.
            age = time.monotonic() - self._wgc_monotonic if self._wgc_monotonic else float("inf")
            if self._wgc_frame is None or age > 0.25:
                self._wgc_condition.wait_for(
                    lambda: self._wgc_sequence > previous or self._wgc_error is not None,
                    timeout=1.5 if self._wgc_frame is None else 0.35,
                )
            if self._wgc_frame is None:
                raise RuntimeError(self._wgc_error or "Windows Graphics Capture did not return a frame")
            if self._wgc_error is not None and self._wgc_sequence <= previous:
                raise RuntimeError(self._wgc_error)
            image = self._wgc_frame.copy()
            sequence = self._wgc_sequence
            captured_monotonic = self._wgc_monotonic
        return self._with_capture_metadata(
            self._encode_image(Image.fromarray(image, mode="RGB")),
            backend="windows-graphics-capture",
            source_sequence=sequence,
            captured_monotonic=captured_monotonic,
        )

    def _capture_directx(self):
        """Capture the physical monitor output with DXGI or monitor-level WinRT."""
        import dxcam
        import mss
        from PIL import Image

        rect = self._client_rect()
        center_x = rect["left"] + rect["width"] // 2
        center_y = rect["top"] + rect["height"] // 2
        with mss.mss() as sct:
            monitors = sct.monitors[1:]
        output_index, monitor = next((
            (index, item) for index, item in enumerate(monitors)
            if item["left"] <= center_x < item["left"] + item["width"]
            and item["top"] <= center_y < item["top"] + item["height"]
        ), (0, monitors[0] if monitors else {"left": 0, "top": 0}))
        if self._dx_camera is None or self._dx_output_index != output_index:
            if self._dx_camera is not None:
                with contextlib.suppress(Exception):
                    release = getattr(self._dx_camera, "release", None)
                    (release() if callable(release) else self._dx_camera.stop())
            requested = (
                ("winrt", "dxgi") if self._backend == "auto" else
                ("winrt",) if self._backend in {"winrt", "monitor-winrt"} else ("dxgi",)
            )
            self._dx_camera = None
            self._dx_backend = None
            last_error = None
            for backend in requested:
                try:
                    self._dx_camera = dxcam.create(
                        output_idx=output_index, output_color="BGR", backend=backend,
                    )
                    self._dx_backend = backend
                    break
                except Exception as exc:
                    last_error = exc
            if self._dx_camera is None:
                raise RuntimeError(f"Unable to initialize DirectX capture: {last_error}")
            self._dx_output_index = output_index
        # Win32 virtualizes coordinates for this DPI-unaware desktop process.
        # Minecraft's 2560x1440 client on a 150% 4K display is really the full
        # 3840x2160 output.  Scale the region back to physical output pixels.
        try:
            import ctypes

            dpi = int(ctypes.windll.user32.GetDpiForWindow(self._window_handle) or 96)
        except Exception:
            dpi = 96
        scale = max(1.0, dpi / 96.0)
        scaled_width = round(rect["width"] * scale)
        scaled_height = round(rect["height"] * scale)
        is_full_output = (
            abs(scaled_width - monitor["width"]) <= max(8, monitor["width"] * .03)
            and abs(scaled_height - monitor["height"]) <= max(8, monitor["height"] * .03)
        )
        if is_full_output:
            region = (0, 0, monitor["width"], monitor["height"])
        else:
            left = round((rect["left"] - monitor["left"]) * scale)
            top = round((rect["top"] - monitor["top"]) * scale)
            region = (
                max(0, left), max(0, top),
                min(monitor["width"], left + scaled_width),
                min(monitor["height"], top + scaled_height),
            )
        # Keep one cursor-independent DXGI producer alive. Reopening one-shot
        # duplication on each observation was prone to returning an old menu
        # frame after an exclusive-fullscreen transition.
        if self._dx_region != region or not self._dx_camera.is_capturing:
            if self._dx_camera.is_capturing:
                self._dx_camera.stop()
            # Poll continuously instead of opening a one-shot duplication for
            # every 5fps observation. This keeps a current, cursor-independent
            # ring buffer while the OpenGL game is actually presenting.
            self._dx_camera.start(region=region, target_fps=30, video_mode=True)
            self._dx_region = region
            self._dx_source_timestamp = None
            self._dx_source_updated_monotonic = time.monotonic()
        # ``get_latest_frame`` blocks until the first frame and cannot be
        # cancelled from asyncio.to_thread. ``grab`` reads the ring buffer
        # without blocking; the duplicator timestamp tells us whether the GPU
        # actually presented a new frame or video_mode merely repeated one.
        frame = self._dx_camera.grab(copy=True)
        if frame is None:
            raise CaptureFramePending("ゲーム画面のフレームをまだ受信していません")
        source_timestamp = float(getattr(self._dx_camera._duplicator, "latest_frame_time", 0.0))
        now = time.monotonic()
        if self._dx_source_timestamp is None or source_timestamp != self._dx_source_timestamp:
            self._dx_source_timestamp = source_timestamp
            self._dx_source_updated_monotonic = now
            self._dx_frame_sequence += 1
        elif now - self._dx_source_updated_monotonic > 2.0:
            raise CaptureFramePending(
                "Minecraftの排他的フルスクリーン描画が更新されていません。"
                "F11でウィンドウ表示に切り替えるとリアルタイム取得できます"
            )
        return self._with_capture_metadata(
            self._encode_image(Image.fromarray(frame[:, :, ::-1])),
            backend=f"monitor-{self._dx_backend or 'dxgi'}", output_index=output_index,
            source_sequence=self._dx_frame_sequence,
            captured_monotonic=self._dx_source_updated_monotonic,
        )

    def capture_media(self):
        # winrt is retained as the existing config value, but now means a real
        # HWND-targeted Windows Graphics Capture session rather than a monitor
        # capture cropped to the window's coordinates.
        if self._backend in {"auto", "winrt", "wgc"}:
            return self._capture_wgc()
        if self._backend in {"directx", "monitor-winrt"}:
            try:
                return self._capture_directx()
            except Exception:
                raise
        import mss
        from PIL import Image

        rect = self._client_rect()
        with mss.mss() as sct:
            shot = sct.grab(rect)
            image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return self._with_capture_metadata(self._encode_image(image), backend="gdi")

    def close(self) -> None:
        """Release native window and desktop capture sessions."""
        control, self._wgc_control = self._wgc_control, None
        if control is not None:
            with contextlib.suppress(Exception):
                control.stop()
                # stop() only requests shutdown.  Waiting here is essential:
                # otherwise the native WGC worker can keep its game capture
                # session alive until the whole Python process exits.
                control.wait()
            logger.info("Windows Graphics Capture session stopped handle=%s", self._window_handle)
        self._wgc_capture = None
        with self._wgc_condition:
            self._wgc_frame = None
            self._wgc_error = "Capture stopped"
            self._wgc_condition.notify_all()
        if self._dx_camera is not None:
            with contextlib.suppress(Exception):
                release = getattr(self._dx_camera, "release", None)
                (release() if callable(release) else self._dx_camera.stop())
        self._dx_camera = None
        self._dx_region = None
        self._dx_source_timestamp = None
