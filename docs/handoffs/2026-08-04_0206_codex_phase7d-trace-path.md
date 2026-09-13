# Phase 7D Trace output path repair — Codex handoff

- Status: completed for the requested local AItuber route.
- Scope respected: no Memory DB, persona configuration, migration, or 204-record data was changed.

## Cause confirmed

`config/config.yaml` has `cognition.trace.enabled: true`, but the prior writer was only called from cognitive decision/outcome paths. The active smoke configuration has `cognition.enabled: false`, so normal local conversation never constructed/emitted a `CognitiveTrace`. The old writer also used the relative configured path, wrote synchronously, and logged write failures only at debug level.

## Changed route

`VoicePipeline.__init__` → `TraceWriter` initialization/startup diagnostic → `respond_text` TurnFrame/commit → `Mind` context/recall → `_emit_legacy_turn_trace(metrics)` → bounded queue → background JSONL append.

The cognitive paths also use `emit`, not direct synchronous `write`. The writer resolves `logs/cognitive_trace.jsonl` relative to the source project root, creates `logs/` safely, does not call `fsync`, deduplicates a turn ID, exposes queue/error counters, and does a short bounded shutdown flush.

## Startup log fields

The existing logger receives one `Cognitive Trace startup:` line per `VoicePipeline` instance with only: `cognitive_trace_enabled`, `trace_writer_initialized`, `trace_output_path`, `trace_queue_enabled`, `trace_last_error`, `project_root`, `config_source_path`, and `runtime_source_root`.

## Checks completed

- `compileall` for the changed modules: PASS.
- Direct isolated writer check: PASS for disabled/no file, arbitrary CWD, missing `logs/`, one line, duplicate turn suppression, and non-fatal write error.
- `Config.load` runtime check could not run: bundled Python lacks PyYAML; project virtualenv points at a missing Python 3.11 base interpreter. No dependency installation was attempted.
- At 02:06 JST no `python`/`pythonw` process was running and no root `logs/cognitive_trace.jsonl` existed, so runtime provenance cannot yet be collected.

## Real-GUI verification (2026-08-04 02:17 JST)

- Main launcher: PID 45028, started 02:15:07, `C:\Users\coala\Desktop\Grok_API\Codex\AItuber\.venv\Scripts\python.exe`, command line `".venv\Scripts\python.exe" run.py`.
- Runtime child: PID 35476, parent 45028, `C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe`, command line `"...Python311\python.exe" run.py`.
- The relative command line plus the startup diagnostic resolved the active project root to `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`; the loaded config was `C:\Users\coala\Desktop\Grok_API\Codex\AItuber\config\config.yaml`.
- Startup line: enabled=true, writer initialized=true, queue enabled=true, output path under the same local project, last error empty.
- `logs/cognitive_trace.jsonl` exists, has current mtime, and contains exactly one row for turn `c328dfc3fd4a`.
- Required metadata is present. It reports `active_persona_id=neuro`, version 1, epoch 0, retrieved records only from `neuro` with `persona_private` scope, `persona_leak=false`, `trace_enqueue_ms=0.12`, queue depth 1, and drops 0.
- A recursive key check found no `prompt`, `content`, `text`, `message`, or `query_text` field.

## Next action (do not resume Memory smoke without user direction)

1. Trace is now a passing gate; retain the existing smoke-test configuration.
2. Do not proceed to b/c/neuro Memory smoke or Phase 8 until directed by the user.

## Remaining holes

- The current repair is for the requested local AItuber route. Discord has not been given an equivalent normal-turn emitter in this change; do not claim cross-transport Trace parity without a separate scoped task.
