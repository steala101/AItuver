"""Non-blocking, privacy-safe writer for CognitiveTrace JSONL records."""
from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock, Thread
from typing import Any

from neuro_voice.cognition.trace import CognitiveTrace

logger = logging.getLogger(__name__)
# Local / Discord may share one JSONL target in the same process.  The writers
# stay independent and bounded, but append is serialized so two worker threads
# cannot interleave a JSON record.
_OUTPUT_APPEND_LOCK = Lock()


class TraceWriter:
    """A bounded background writer.  ``emit`` never waits for disk I/O."""

    def __init__(
        self,
        path: str | Path | None,
        *,
        enabled: bool = False,
        project_root: str | Path | None = None,
        queue_size: int = 256,
    ) -> None:
        self.enabled = bool(enabled and path)
        self.project_root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
        self._path: Path | None = None
        self._queue: Queue[dict[str, Any] | None] | None = None
        self._thread: Thread | None = None
        self._lock = Lock()
        self._seen_turns: OrderedDict[str, None] = OrderedDict()
        self._dropped_count = 0
        self._last_error = ""
        self._last_write_at = 0.0
        self._last_enqueue_ms = 0.0
        self._initialized = False
        if not self.enabled:
            return
        try:
            self._path = self._resolve_path(path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._queue = Queue(maxsize=max(1, int(queue_size)))
            self._thread = Thread(target=self._run, name="cognitive-trace", daemon=True)
            self._thread.start()
            self._initialized = True
        except OSError as exc:
            self._record_error("initialize", exc)

    def _resolve_path(self, path: str | Path | None) -> Path:
        requested = Path(path or "logs/cognitive_trace.jsonl")
        resolved = (requested if requested.is_absolute() else self.project_root / requested).resolve()
        try:
            resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise OSError("trace output path must be inside project root") from exc
        return resolved

    @property
    def path(self) -> Path | None:
        return self._path

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "cognitive_trace_enabled": self.enabled,
                "trace_writer_initialized": self._initialized,
                "trace_output_path": str(self._path) if self._path else "",
                "trace_queue_enabled": self._queue is not None,
                "trace_queue_depth": self._queue.qsize() if self._queue else 0,
                "trace_dropped_count": self._dropped_count,
                "trace_enqueue_ms": round(self._last_enqueue_ms, 3),
                "trace_last_write_at": self._last_write_at,
                "trace_last_error": self._last_error,
                "project_root": str(self.project_root),
            }

    def emit(self, trace: CognitiveTrace) -> bool:
        if not self.enabled or self._queue is None:
            return False
        started = time.perf_counter()
        record = trace.snapshot()
        turn_id = str(record.get("turn_id") or record.get("trace_id") or "")
        with self._lock:
            if turn_id and turn_id in self._seen_turns:
                return False
            try:
                self._queue.put_nowait(record)
            except Full:
                self._dropped_count += 1
                self._last_enqueue_ms = (time.perf_counter() - started) * 1000
                logger.warning("Cognitive Trace queue full; trace dropped count=%s", self._dropped_count)
                return False
            if turn_id:
                self._seen_turns[turn_id] = None
                if len(self._seen_turns) > 2048:
                    self._seen_turns.popitem(last=False)
            self._last_enqueue_ms = (time.perf_counter() - started) * 1000
            record["trace_writer"] = {
                "trace_enqueue_ms": round(self._last_enqueue_ms, 3),
                "trace_queue_depth": self._queue.qsize(),
                "trace_dropped_count": self._dropped_count,
            }
        return True

    def write(self, trace: CognitiveTrace) -> None:
        self.emit(trace)
        self.flush()

    def flush(self, timeout: float = 0.5) -> None:
        if self._queue is None:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    def close(self, timeout: float = 0.5) -> None:
        if self._queue is None or self._thread is None:
            return
        self.flush(timeout=max(0.0, timeout * 0.7))
        try:
            self._queue.put_nowait(None)
        except Full:
            pass
        self._thread.join(timeout=max(0.0, timeout * 0.3))

    def _run(self) -> None:
        assert self._queue is not None
        while True:
            try:
                record = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                if record is None:
                    return
                assert self._path is not None
                with _OUTPUT_APPEND_LOCK:
                    with self._path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                with self._lock:
                    self._last_write_at = time.time()
            except OSError as exc:
                self._record_error("write", exc)
            finally:
                self._queue.task_done()

    def _record_error(self, stage: str, exc: OSError) -> None:
        message = f"{stage}: {type(exc).__name__}: {exc}"
        with self._lock:
            self._last_error = message[:500]
        logger.error("Cognitive Trace %s failed: %s", stage, message)
