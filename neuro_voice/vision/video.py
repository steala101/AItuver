"""Low-rate camera/video-file observation without blocking live conversation."""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

from neuro_voice.media import MediaInput, MediaType
from neuro_voice.vision.media import hash_distance, prepare_image
from neuro_voice.vision.models import VideoAnalysis, VideoSegment, VisualObservation
from neuro_voice.vision.service import PerceptionService, video_instruction

logger = logging.getLogger(__name__)


class VideoObservationService:
    """Captures at a low FPS and only analyzes changed, recent frame windows."""

    def __init__(
        self, cfg, perception: PerceptionService, *, is_busy=lambda: False,
        on_observation: Callable[[VisualObservation], Awaitable[None] | None] | None = None,
    ):
        self._cfg = cfg
        self._perception = perception
        self._is_busy = is_busy
        self._source = cfg.get("video.source", 0)
        self._input_kind = str(cfg.get("video.input", "camera")).strip().lower()
        if self._input_kind not in {"camera", "screen", "window", "obs"}:
            self._input_kind = "camera"
        self._screen_monitor = cfg.get("video.screen_monitor", cfg.get("vision.monitor", "auto"))
        self._window_handle = cfg.get("video.window_handle", "")
        self._game_profile = str(cfg.get("video.game_profile", "")).strip().lower()
        self._configured_input_kind = self._input_kind
        self._auto_game_window = self._configured_input_kind == "window" and self._game_profile == "minecraft"
        self._game_fallback = False
        self._next_game_probe = 0.0
        self._instruction = video_instruction(self._game_profile)
        self._on_observation = on_observation
        latest_queue_size = max(
            1, min(2, int(cfg.get("vision.latest_frame_queue_size", 1))),
        )
        self._frames: deque[MediaInput] = deque(maxlen=latest_queue_size)
        capture_fps = max(.2, float(cfg.get(
            "vision.capture_fps", cfg.get("video.obs.capture_fps", 1.0),
        )))
        raw_seconds = max(1.0, float(cfg.get("vision.raw_frame_memory_seconds", 10.0)))
        self._raw_history: deque[MediaInput] = deque(
            maxlen=max(2, int(capture_fps * raw_seconds) + 2),
        )
        self._capture = None
        self._capture_task: asyncio.Task | None = None
        self._background_analysis_task: asyncio.Task | None = None
        self._foreground_analysis_task: asyncio.Task | None = None
        self._last_explicit_frame: MediaInput | None = None
        self._frame_event = asyncio.Event()
        self._last_analysis = 0.0
        self._last_hash: str | None = None
        self._last_motion_score = 0.0
        self._captured_frame_count = 0
        self._analyzed_frame_count = 0
        self._dropped_frame_count = 0
        self._last_analyzed_sample = 0
        self._priority_analysis_requested = False
        self._vision_failure_count = 0
        self._circuit_open_until = 0.0
        self._current_analysis_interval = float(cfg.get("video.analysis_interval_sec", 2.0))
        self._last_vision_metrics: dict[str, object] = {}
        self._analysis_times: deque[float] = deque(maxlen=240)
        self._latency_samples: deque[float] = deque(maxlen=60)
        self._gpu_cache_at = 0.0
        self._gpu_cache: dict[str, object] = {"gpu_percent": None, "vram_used_mb": None, "vram_total_mb": None}
        self._stopped = True
        self._failures = 0
        self._capture_lock = threading.Lock()
        self._last_capture_error = ""

    def _open_obs_capture(self):
        from neuro_voice.vision.obs import ObsCapture

        vision = self._cfg.section("vision")
        return ObsCapture(
            str(self._cfg.get("video.obs.host", "127.0.0.1")),
            int(self._cfg.get("video.obs.port", 4455)),
            str(self._cfg.get("video.obs.password", "")),
            str(self._cfg.get("video.obs.source_name", "")),
            max_width=int(self._cfg.get("video.capture_max_width", vision.get("max_width", 960))),
            jpeg_quality=int(vision.get("jpeg_quality", 75)),
            timeout=float(self._cfg.get("video.obs.request_timeout_sec", 3.0)),
        )

    @property
    def is_running(self) -> bool:
        return self._capture_task is not None and not self._capture_task.done()

    @property
    def input_kind(self) -> str:
        """Actual running source, used to reject stale UI/config mismatches."""
        return self._input_kind

    @property
    def is_game_fallback(self) -> bool:
        """True when Minecraft is not running and desktop capture is intentional."""
        return self._game_fallback

    @property
    def latest_frame(self) -> MediaInput | None:
        return self._frames[-1] if self._frames else None

    @property
    def last_explicit_frame(self) -> MediaInput | None:
        """Exact frame bound to the most recent explicit visual question."""
        return self._last_explicit_frame

    @property
    def last_capture_error(self) -> str:
        return self._last_capture_error

    @property
    def last_motion_score(self) -> float:
        return self._last_motion_score

    @property
    def captured_frame_count(self) -> int:
        return self._captured_frame_count

    def state_context(self, user_text: str = "") -> str | None:
        """Current semantic state plus 1fps capture freshness/flow telemetry."""
        latest = self.latest_frame
        return self._perception.memory.get_context_for_dialogue(
            user_text,
            latest_frame_at=latest.timestamp if latest is not None else None,
            motion_score=self._last_motion_score,
            max_chars=int(self._cfg.get("vision.max_visual_context_chars", 1600)),
        )

    @property
    def analyzed_frame_count(self) -> int:
        return self._analyzed_frame_count

    @property
    def dropped_frame_count(self) -> int:
        return self._dropped_frame_count

    def debug_status(self) -> dict[str, object]:
        latest = self.latest_frame
        current = self._perception.memory.current_scene
        now = time.time()
        recent_requests = sum(1 for item in self._analysis_times if time.monotonic() - item <= 60.0)
        gpu = self._gpu_status()
        return {
            "worker_state": (
                "circuit_open" if time.monotonic() < self._circuit_open_until else
                "analyzing" if self._foreground_analysis_task is not None and not self._foreground_analysis_task.done() else
                "analyzing" if self._background_analysis_task is not None and not self._background_analysis_task.done() else
                "capturing" if self.is_running else "stopped"
            ),
            "capture_count": self._captured_frame_count,
            "analysis_count": self._analyzed_frame_count,
            "latest_frame_queue_size": len(self._frames),
            "raw_frame_history_size": len(self._raw_history),
            "dropped_frame_count": self._dropped_frame_count,
            "latest_frame_age_sec": None if latest is None else round(max(0.0, now - float(latest.timestamp or now)), 3),
            "latest_observation_age_sec": None if current is None else round(max(0.0, now - float(current.captured_at or now)), 3),
            "motion_score": round(self._last_motion_score, 4),
            "analysis_interval_sec": round(self._current_analysis_interval, 3),
            "vision_requests_per_minute": recent_requests,
            "average_vision_latency_ms": (
                None if not self._latency_samples
                else round(sum(self._latency_samples) / len(self._latency_samples))
            ),
            "last_capture_error": self._last_capture_error,
            "current_scene": "" if current is None else current.summary,
            "recent_events": [item.summary for item in self._perception.memory.get_recent_events(30)[-6:]],
            "metrics": dict(self._last_vision_metrics),
            **gpu,
        }

    def _gpu_status(self) -> dict[str, object]:
        if time.monotonic() - self._gpu_cache_at < 5.0:
            return self._gpu_cache
        self._gpu_cache_at = time.monotonic()
        try:
            import pynvml

            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            self._gpu_cache = {
                "gpu_percent": int(util.gpu),
                "vram_used_mb": round(memory.used / (1024 * 1024)),
                "vram_total_mb": round(memory.total / (1024 * 1024)),
            }
        except Exception:
            self._gpu_cache = {"gpu_percent": None, "vram_used_mb": None, "vram_total_mb": None}
        return self._gpu_cache

    async def start(self) -> bool:
        if self.is_running:
            return True
        if self._input_kind == "obs":
            self._stopped = False
            self._capture = self._open_obs_capture()
            try:
                await self._capture.start()
            except Exception as exc:
                self._last_capture_error = str(exc)
                logger.warning("OBS video input could not be started: %s", exc)
                await self._capture.close()
                self._capture = None
                return False
            # Preserve an automatically resolved, unique Game Capture source
            # for diagnostics during this process.  It is deliberately not
            # persisted behind the user's back.
            if self._capture.source_name:
                self._cfg.set("video.obs.active_source_name", self._capture.source_name)
            self._capture_task = asyncio.create_task(self._capture_loop(), name="video-obs-capture")
            return True
        if self._input_kind in {"screen", "window"}:
            self._stopped = False
            try:
                self._capture = await asyncio.to_thread(self._open_capture)
            except Exception:
                logger.exception("Unable to open screen video input")
                return False
            self._capture_task = asyncio.create_task(self._capture_loop(), name=f"video-{self._input_kind}-capture")
            return True
        try:
            import cv2  # noqa: F401
        except ImportError:
            logger.warning("OpenCV が未導入のためカメラ映像認識を開始できません")
            return False
        self._stopped = False
        try:
            self._capture = await asyncio.to_thread(self._open_capture)
        except Exception:
            logger.exception("カメラを開けません")
            return False
        self._capture_task = asyncio.create_task(self._capture_loop(), name="video-capture")
        return True

    def _open_screen_capture(self):
        vision = self._cfg.section("vision")
        from neuro_voice.vision.capture import ScreenCapture

        return ScreenCapture(
            monitor=self._screen_monitor,
            max_width=int(self._cfg.get("video.capture_max_width", vision.get("max_width", 768))),
            jpeg_quality=int(vision.get("jpeg_quality", 75)),
        )

    def _open_window_capture(self, handle):
        vision = self._cfg.section("vision")
        from neuro_voice.vision.capture import WindowCapture

        return WindowCapture(
            handle,
            max_width=int(self._cfg.get("video.capture_max_width", vision.get("max_width", 768))),
            jpeg_quality=int(vision.get("jpeg_quality", 75)),
            backend=str(self._cfg.get("video.window_capture_backend", "auto")),
            auto_rebind_game=self._game_profile == "minecraft",
        )

    def _open_capture(self):
        if self._input_kind == "window":
            if self._auto_game_window:
                from neuro_voice.vision.capture import find_game_window

                try:
                    found = find_game_window(self._game_profile)
                except Exception:
                    logger.debug("Game window discovery is unavailable during startup", exc_info=True)
                    found = None
                if found is not None:
                    self._window_handle = found["handle"]
                    self._cfg.set("video.window_handle", self._window_handle)
                    self._game_fallback = False
                    return self._open_window_capture(self._window_handle)
                self._input_kind = "screen"
                self._game_fallback = True
                logger.info("Minecraft is not running; using desktop capture until it starts")
                return self._open_screen_capture()
            return self._open_window_capture(self._window_handle)
        if self._input_kind == "screen":
            return self._open_screen_capture()
        import cv2

        cap = cv2.VideoCapture(self._source)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"camera source unavailable: {self._source}")
        return cap

    @staticmethod
    def _close_capture(capture) -> None:
        if capture is None:
            return
        close = getattr(capture, "close", None)
        if callable(close):
            close()
            return
        release = getattr(capture, "release", None)
        if callable(release):
            release()

    def _refresh_game_source_locked(self) -> None:
        """Switch desktop <-> Minecraft window without restarting the app."""
        if not self._auto_game_window or time.monotonic() < self._next_game_probe:
            return
        self._next_game_probe = time.monotonic() + 1.0
        from neuro_voice.vision.capture import find_game_window

        try:
            found = find_game_window(self._game_profile)
        except Exception:
            logger.debug("Game window discovery failed; retaining current capture", exc_info=True)
            return
        if found is not None and (self._input_kind != "window" or str(found["handle"]) != str(self._window_handle)):
            old = self._capture
            self._window_handle = str(found["handle"])
            self._cfg.set("video.window_handle", self._window_handle)
            self._capture = self._open_window_capture(self._window_handle)
            self._input_kind = "window"
            self._game_fallback = False
            self._frames.clear()
            self._last_hash = None
            self._close_capture(old)
            logger.info("Minecraft window detected; switched live capture to handle=%s", self._window_handle)
        elif found is None and self._input_kind == "window":
            old = self._capture
            self._capture = self._open_screen_capture()
            self._input_kind = "screen"
            self._game_fallback = True
            self._frames.clear()
            self._last_hash = None
            self._close_capture(old)
            logger.info("Minecraft window closed; switched live capture back to desktop")

    async def stop(self) -> None:
        self._stopped = True
        self.cancel_background()
        if self._foreground_analysis_task is not None and not self._foreground_analysis_task.done():
            self._foreground_analysis_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._foreground_analysis_task
        self._foreground_analysis_task = None
        if self._background_analysis_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._background_analysis_task
        self._background_analysis_task = None
        if self._capture_task is not None:
            self._capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._capture_task
        self._capture_task = None
        self._perception.cancel_background()
        cap, self._capture = self._capture, None
        if cap is not None:
            if self._input_kind == "obs":
                await cap.close()
            else:
                await asyncio.to_thread(self._close_capture, cap)
        self._frames.clear()
        self._raw_history.clear()
        self._frame_event.clear()

    async def _capture_loop(self) -> None:
        if self._input_kind == "obs":
            fps = float(self._cfg.get(
                "vision.capture_fps", self._cfg.get("video.obs.capture_fps", 1.0),
            ))
        else:
            fps = float(self._cfg.get("video.target_capture_fps", 5.0))
        interval = 1.0 / max(0.2, fps)
        while not self._stopped:
            started = time.monotonic()
            try:
                frame = (
                    await self._read_obs_frame()
                    if self._input_kind == "obs"
                    else await asyncio.to_thread(self._read_frame)
                )
                if frame is not None:
                    self._append(frame)
                    await self._maybe_background_analyze()
                self._failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failures += 1
                logger.warning("カメラフレーム取得に失敗 (%d)", self._failures)
                if self._failures >= 3:
                    logger.error("カメラ取得を停止します。音声会話は継続します")
                    return
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

    async def _read_obs_frame(self) -> MediaInput | None:
        capture = self._capture
        if capture is None:
            return None
        try:
            image = await capture.capture_media()
        except Exception as exc:
            self._last_capture_error = str(exc)
            # A temporarily hidden source or an OBS restart must not terminate
            # the capture service and must never fall back to the desktop.
            logger.debug("OBS frame unavailable: %s", exc)
            return None
        self._last_capture_error = ""
        return MediaInput(
            media_type=MediaType.VIDEO_FRAME,
            data=image.data,
            mime_type=image.mime_type,
            timestamp=image.timestamp,
            metadata={**image.metadata, "source": "obs", "game_fallback": False},
        )

    def _read_frame(self) -> MediaInput | None:
        with self._capture_lock:
            self._refresh_game_source_locked()
            capture = self._capture
            kind = self._input_kind
            if capture is None:
                return None
            if kind in {"screen", "window"}:
                try:
                    image = capture.capture_media()
                except Exception as exc:
                    from neuro_voice.vision.capture import CaptureFramePending

                    if isinstance(exc, CaptureFramePending):
                        self._last_capture_error = str(exc)
                        return None
                    if not self._auto_game_window or kind != "window":
                        raise
                    # A game can disappear between the discovery and capture
                    # calls. Resume desktop capture instead of stopping video.
                    self._input_kind = "screen"
                    self._game_fallback = True
                    old = self._capture
                    self._capture = self._open_screen_capture()
                    self._close_capture(old)
                    image = self._capture.capture_media()
                self._last_capture_error = ""
                return MediaInput(
                    media_type=MediaType.VIDEO_FRAME,
                    data=image.data,
                    mime_type=image.mime_type,
                    timestamp=image.timestamp,
                    metadata={
                        **image.metadata, "source": self._input_kind,
                        "game_fallback": self._game_fallback,
                    },
                )
            ok, frame = capture.read()
        if not ok:
            raise RuntimeError("camera read failed")
        import cv2

        ok, encoded = cv2.imencode(".jpg", frame)
        if not ok:
            return None
        v = self._cfg.section("vision")
        return prepare_image(
            bytes(encoded), max_width=int(v.get("max_image_width", v.get("max_width", 1280))),
            max_height=int(v.get("max_image_height", 1280)),
            jpeg_quality=int(v.get("jpeg_quality", 85)), media_type=MediaType.VIDEO_FRAME,
        )

    def _append(self, frame: MediaInput) -> None:
        current_hash = str(frame.metadata.get("fingerprint", ""))
        motion = hash_distance(self._last_hash, current_hash) if self._last_hash else 1.0
        self._last_motion_score = motion
        self._captured_frame_count += 1
        frame.metadata.update({
            "motion_score": motion,
            "sample_index": self._captured_frame_count,
            "sample_interval_target_sec": (
                1.0 / max(.2, float(self._cfg.get(
                    "vision.capture_fps", self._cfg.get("video.obs.capture_fps", 1.0),
                )))
                if self._input_kind == "obs" else None
            ),
        })
        self._last_hash = current_hash
        previous = self._frames[-1] if self._frames else None
        if previous is not None:
            previous_sample = int(previous.metadata.get("sample_index", 0) or 0)
            if previous_sample > self._last_analyzed_sample:
                self._dropped_frame_count += 1
        # Analysis queue: latest-frame-wins. Raw history is a separate bounded
        # ring used only for representative temporal frames.
        self._frames.clear()
        self._frames.append(frame)
        self._frame_event.set()
        self._raw_history.append(frame)
        cutoff = float(frame.timestamp or time.time()) - max(
            1.0, float(self._cfg.get("vision.raw_frame_memory_seconds", 10.0)),
        )
        while self._raw_history and float(self._raw_history[0].timestamp or 0.0) < cutoff:
            self._raw_history.popleft()
        if self._input_kind == "obs" and self._captured_frame_count % 5 == 0:
            logger.info(
                "OBS realtime sampling: source=%s size=%sx%s frames=%d "
                "latest_seq=%s motion=%.3f queue=%d dropped=%d",
                frame.metadata.get("obs_source_name", "-"),
                frame.metadata.get("width", "?"),
                frame.metadata.get("height", "?"),
                self._captured_frame_count,
                frame.metadata.get("capture_sequence", "-"),
                motion,
                len(self._frames),
                self._dropped_frame_count,
            )

    def _adaptive_interval(self, *, priority: bool = False) -> float:
        minimum = max(.5, float(self._cfg.get("vision.analysis_min_interval_sec", 1.0)))
        maximum = max(minimum, float(self._cfg.get("vision.analysis_max_interval_sec", 5.0)))
        motion_floor = max(0.0, float(self._cfg.get("vision.change_threshold", .15)))
        important = max(motion_floor + .01, float(self._cfg.get("video.scene_change_threshold", .22)))
        if priority:
            return minimum
        motion = max(0.0, self._last_motion_score)
        if motion <= motion_floor:
            base = maximum
        elif motion >= important:
            base = minimum
        else:
            ratio = (motion - motion_floor) / max(.001, important - motion_floor)
            base = maximum - (maximum - minimum) * ratio
        # Inference latency is the most direct pressure signal available from
        # the local runner. If it is slower than the desired cadence, back off
        # rather than keeping the GPU permanently queued.
        previous_ms = float(self._last_vision_metrics.get("total_ms") or 0.0)
        if previous_ms > 0:
            base = max(base, min(maximum, previous_ms / 1000.0))
        return base

    def request_priority_analysis(self) -> bool:
        """Request newest-frame analysis without making dialogue wait for it."""
        if not self._frames:
            return False
        self._priority_analysis_requested = True
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._maybe_background_analyze(force=True))
        except RuntimeError:
            return False
        return True

    async def _maybe_background_analyze(self, *, force: bool = False) -> None:
        if self._is_busy() or len(self._frames) < 1:
            return
        if self._foreground_analysis_task is not None and not self._foreground_analysis_task.done():
            return
        if self._background_analysis_task is not None and not self._background_analysis_task.done():
            return
        now = time.monotonic()
        if now < self._circuit_open_until:
            return
        priority = bool(force or self._priority_analysis_requested)
        interval = self._adaptive_interval(priority=priority)
        self._current_analysis_interval = interval
        if not priority and now - self._last_analysis < interval:
            return
        self._last_analysis = now
        self._priority_analysis_requested = False
        # Gemma vision can take many seconds.  Running it inline stopped the
        # capture loop for that entire time, so the "latest" frame was really
        # the frame from before analysis began.  Keep capture at its configured
        # FPS and let only one background inference run independently.
        task = asyncio.create_task(
            self.analyze_recent_window(background=True),
            name="video-background-analysis",
        )
        self._background_analysis_task = task
        latest = self._frames[-1]
        logger.info(
            "Video background analysis started: minimum_interval=%.1fs frames<=%d "
            "latest_seq=%s frame_age=%.0fms backend=%s",
            interval,
            max(1, int(self._cfg.get("video.max_frames_per_analysis", 4))),
            latest.metadata.get("capture_sequence", "-"),
            max(0.0, (time.time() - (latest.timestamp or time.time())) * 1000.0),
            latest.metadata.get("capture_backend", "camera"),
        )
        task.add_done_callback(self._background_analysis_finished)

    def _background_analysis_finished(self, task: asyncio.Task) -> None:
        if self._background_analysis_task is task:
            self._background_analysis_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            self._vision_failure_count += 1
            if self._vision_failure_count >= max(1, int(self._cfg.get("vision.circuit_breaker_failures", 3))):
                cooldown = max(5.0, float(self._cfg.get("vision.circuit_breaker_cooldown_sec", 20.0)))
                self._circuit_open_until = time.monotonic() + cooldown
                logger.error("Vision circuit breaker opened for %.1fs", cooldown)
            logger.warning("Background video analysis failed", exc_info=True)
        finally:
            latest = self.latest_frame
            if (
                latest is not None
                and int(latest.metadata.get("sample_index", 0) or 0) > self._last_analyzed_sample
                and not self._is_busy()
            ):
                asyncio.create_task(self._maybe_background_analyze())

    async def analyze_recent_window(self, *, background: bool = False) -> VisualObservation | None:
        if not self._frames:
            return None
        window_s = max(.1, float(self._cfg.get("video.frame_window_sec", 4.0)))
        latest_frame = self._frames[-1]
        latest = latest_frame.timestamp or time.time()
        history = self._raw_history if self._raw_history else self._frames
        frames = [item for item in history if latest - (item.timestamp or latest) <= window_s]
        frames = frames[-max(1, int(self._cfg.get("video.max_frames_per_analysis", 4))):]
        if not frames:
            return None
        if background and self._is_busy():
            return None
        queue_wait_ms = round(max(0.0, time.time() - float(frames[-1].timestamp or time.time())) * 1000)
        timeout = max(2.0, float(self._cfg.get("vision.background_timeout_sec", 15.0)))
        started = time.perf_counter()
        observation = await asyncio.wait_for(
            self._perception.analyze(
                frames, request_id=uuid4().hex, temporal=len(frames) > 1, background=background,
                instruction=self._instruction,
            ),
            timeout=timeout,
        )
        self._last_analyzed_sample = int(frames[-1].metadata.get("sample_index", 0) or 0)
        cache_hit = bool(getattr(self._perception, "last_was_cache_hit", False))
        if not cache_hit:
            self._analyzed_frame_count += 1
        self._vision_failure_count = 0
        self._last_analysis = time.monotonic()
        self._last_vision_metrics = {
            "frame_capture_ms": frames[-1].metadata.get("frame_capture_ms"),
            "frame_encode_ms": frames[-1].metadata.get("frame_encode_ms"),
            "queue_wait_ms": queue_wait_ms,
            **getattr(self._perception, "last_metrics", {}),
            "cache_hit": cache_hit,
            "total_ms": round((time.perf_counter() - started) * 1000),
        }
        if not cache_hit:
            self._analysis_times.append(time.monotonic())
            self._latency_samples.append(float(self._last_vision_metrics["total_ms"]))
        logger.info(
            "[Vision] capture=%sms encode=%sms queue_wait=%dms inference=%sms parse=%sms "
            "memory=%sms total=%dms captures=%d analyses=%d dropped=%d",
            self._last_vision_metrics.get("frame_capture_ms"),
            self._last_vision_metrics.get("frame_encode_ms"),
            queue_wait_ms,
            self._last_vision_metrics.get("vision_inference_ms"),
            self._last_vision_metrics.get("vision_parse_ms"),
            self._last_vision_metrics.get("visual_memory_update_ms"),
            self._last_vision_metrics["total_ms"],
            self._captured_frame_count,
            self._analyzed_frame_count,
            self._dropped_frame_count,
        )
        if background and not cache_hit and self._on_observation is not None:
            result = self._on_observation(observation)
            if inspect.isawaitable(result):
                await result
        return observation

    async def capture_current_frame(
        self, *, request_id: str = "",
    ) -> MediaInput | None:
        """Capture a fresh frame for an explicit user question, never reuse the queue."""
        # A direct question has priority over speculative background vision.
        # This also prevents two Ollama requests from competing for the same
        # model and causing the 10-20 second response stalls seen in the UI.
        self.cancel_background()
        # Never let a previous successful capture survive a failed current
        # request and then appear in the UI as this turn's "current screen".
        self._last_explicit_frame = None
        requested_at = time.time()
        frame = None
        # A newly restored fullscreen game needs a few producer ticks before
        # its DXGI ring buffer has a first frame.  Poll only this explicit
        # "not ready yet" state for at most 250ms; do not turn it into a false
        # no-frame response or reuse an older menu image.
        attempts = 2 if self._input_kind == "obs" else 6
        for attempt in range(attempts):
            frame = (
                await self._read_obs_frame()
                if self._input_kind == "obs"
                else await asyncio.to_thread(self._read_frame)
            )
            if frame is not None:
                break
            if self._input_kind == "obs":
                # The websocket client already reconnects once.  A short
                # second attempt covers OBS scene activation without serving
                # an old queued frame.
                if attempt + 1 < attempts:
                    await asyncio.sleep(.08)
                continue
            if "まだ受信" not in self._last_capture_error or attempt >= attempts - 1:
                break
            await asyncio.sleep(.05)
        if frame is not None:
            frame.metadata.update({
                "explicit_request_id": str(request_id or ""),
                "explicit_requested_at": requested_at,
            })
            # The queue is for temporal background analysis.  An explicit
            # question must not accidentally be answered from an old scene.
            self._last_explicit_frame = frame
            self._append(frame)
            logger.info(
                "Fresh %s capture seq=%s backend=%s fingerprint=%s",
                self._input_kind,
                frame.metadata.get("capture_sequence", "-"),
                frame.metadata.get("capture_backend", "camera"),
                str(frame.metadata.get("fingerprint", ""))[:16],
            )
        return frame

    async def analyze_current_frame(self, *, request_id: str) -> VisualObservation | None:
        frame = await self.capture_current_frame(request_id=request_id)
        if frame is None:
            return None
        capture_sequence = int(frame.metadata.get("capture_sequence", 0) or 0)
        started = time.perf_counter()
        observation = await self._perception.analyze(
            [frame], request_id=request_id, temporal=False, instruction=self._instruction,
        )
        self._last_analyzed_sample = int(
            frame.metadata.get("sample_index", self._captured_frame_count) or 0
        )
        cache_hit = bool(getattr(self._perception, "last_was_cache_hit", False))
        if not cache_hit:
            self._analyzed_frame_count += 1
            self._analysis_times.append(time.monotonic())
            self._latency_samples.append((time.perf_counter() - started) * 1000.0)
        self._last_analysis = time.monotonic()
        self._last_vision_metrics = {
            "frame_capture_ms": frame.metadata.get("frame_capture_ms"),
            "frame_encode_ms": frame.metadata.get("frame_encode_ms"),
            "capture_sequence": capture_sequence,
            **getattr(self._perception, "last_metrics", {}),
            "cache_hit": cache_hit,
            "total_ms": round((time.perf_counter() - started) * 1000),
        }
        observation.metrics = {
            **getattr(observation, "metrics", {}),
            "capture_sequence": capture_sequence,
        }
        latest = self.latest_frame
        latest_sequence = (
            int(latest.metadata.get("capture_sequence", 0) or 0)
            if latest is not None else capture_sequence
        )
        latest_fingerprint = (
            str(latest.metadata.get("fingerprint", ""))
            if latest is not None else observation.frame_hash
        )
        logger.info(
            "Explicit vision completed: request=%s analyzed_seq=%s latest_seq=%s "
            "capture_age=%.1fs scene_distance=%.3f",
            request_id,
            capture_sequence,
            latest_sequence,
            max(0.0, time.time() - float(observation.captured_at or time.time())),
            hash_distance(observation.frame_hash, latest_fingerprint),
        )
        return observation

    async def wait_for_first_frame(self, *, timeout_s: float = 3.0) -> MediaInput | None:
        """Wait for proof that the selected source is producing frames."""
        latest = self.latest_frame
        if latest is not None:
            return latest
        try:
            await asyncio.wait_for(self._frame_event.wait(), timeout=max(0.1, timeout_s))
        except asyncio.TimeoutError:
            return None
        return self.latest_frame

    async def ensure_current_analysis(
        self, *, request_id: str, timeout_s: float | None = None,
        force_fresh: bool = False,
    ) -> tuple[VisualObservation | None, bool]:
        """Analyze a frame, optionally requiring capture after this request.

        Returns ``(observation, pending)``.  A timeout means the inference keeps
        running and will still update PerceptionMemory for the next turn.

        Normal background/cold-start callers share one in-flight task.  An
        explicit "what is on screen now?" turn sets ``force_fresh`` so it does
        not wait for a frame captured before the user asked the question.
        """
        if force_fresh:
            # A cold-start or background inference may be ten seconds old by
            # the time the user asks about "now".  Cancel that generation and
            # bind this turn to a newly captured frame instead.
            self.cancel_background()
            foreground = self._foreground_analysis_task
            if foreground is not None and not foreground.done():
                foreground.cancel()
            self._foreground_analysis_task = None
            await asyncio.sleep(0)

        task = self._foreground_analysis_task
        if task is not None and task.done():
            self._foreground_analysis_task = None
            task = None
        if task is None:
            background = self._background_analysis_task
            if not force_fresh and background is not None and not background.done():
                task = background
            else:
                task = asyncio.create_task(
                    self.analyze_current_frame(request_id=request_id),
                    name="video-foreground-analysis",
                )
                self._foreground_analysis_task = task
                task.add_done_callback(self._foreground_analysis_finished)
        try:
            if timeout_s is None:
                return await asyncio.shield(task), False
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=max(0.1, timeout_s),
            ), False
        except asyncio.TimeoutError:
            return None, True

    def _foreground_analysis_finished(self, task: asyncio.Task) -> None:
        if self._foreground_analysis_task is task:
            self._foreground_analysis_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.warning("Foreground video analysis failed", exc_info=True)
        finally:
            latest = self.latest_frame
            if (
                latest is not None
                and int(latest.metadata.get("sample_index", 0) or 0) > self._last_analyzed_sample
                and not self._is_busy()
            ):
                asyncio.create_task(self._maybe_background_analyze())

    def cancel_background(self) -> None:
        if self._background_analysis_task is not None and not self._background_analysis_task.done():
            self._background_analysis_task.cancel()
        cancel = getattr(self._perception, "cancel_background", None)
        if callable(cancel):
            cancel()


async def analyze_video_file(path: str | Path, cfg, perception: PerceptionService) -> VideoAnalysis:
    """Analyze a bounded video as short chronological segments, not all frames."""
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("動画解析には opencv-python が必要です") from exc
    video_path = Path(path)
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    cap = await asyncio.to_thread(cv2.VideoCapture, str(video_path))
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 1.0
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = count / fps
        max_duration = float(cfg.get("video_file.max_video_duration_sec", 600))
        duration = min(duration, max_duration)
        segment_s = max(1.0, float(cfg.get("video_file.segment_duration_sec", 10)))
        max_frames = max(1, int(cfg.get("video_file.max_frames_per_segment", 6)))
        timeline: list[VideoSegment] = []
        for start in range(0, int(duration), int(segment_s)):
            end = min(duration, start + segment_s)
            frames: list[MediaInput] = []
            for index in range(max_frames):
                pos = start + (end - start) * index / max(1, max_frames - 1)
                cap.set(cv2.CAP_PROP_POS_MSEC, pos * 1000)
                ok, frame = cap.read()
                if not ok:
                    continue
                ok, encoded = cv2.imencode(".jpg", frame)
                if ok:
                    v = cfg.section("vision")
                    frames.append(prepare_image(bytes(encoded), max_width=int(v.get("max_image_width", 1280)), max_height=int(v.get("max_image_height", 1280)), jpeg_quality=int(v.get("jpeg_quality", 85)), media_type=MediaType.VIDEO_FRAME, timestamp=pos))
            if frames:
                observation = await perception.analyze(frames, request_id=uuid4().hex, temporal=True)
                timeline.append(VideoSegment(float(start), float(end), observation))
        summary = " ".join(segment.observation.summary for segment in timeline if segment.observation.summary)[:2000]
        return VideoAnalysis(
            summary=summary, timeline=timeline,
            detected_actions=list(dict.fromkeys(x for s in timeline for x in s.observation.actions)),
            detected_objects=list(dict.fromkeys(x for s in timeline for x in s.observation.objects)),
            visible_text=list(dict.fromkeys(x for s in timeline for x in s.observation.visible_text)),
        )
    finally:
        await asyncio.to_thread(cap.release)
