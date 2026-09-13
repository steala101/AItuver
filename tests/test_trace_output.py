import json
from pathlib import Path

from neuro_voice.cognition.async_trace_writer import TraceWriter
from neuro_voice.cognition.trace import CognitiveTrace


def _trace(turn_id: str = "turn-a") -> CognitiveTrace:
    return CognitiveTrace(
        turn_id=turn_id,
        active_persona_id="b",
        active_persona_version="v1",
        persona_epoch=2,
        memory_retrieval_trigger="legacy_context",
        persona_switch_state="stable",
    )


def test_trace_contains_safe_delivery_counts_and_recall_evidence():
    trace = _trace()
    trace.recall_evidence_memory_ids = [1]
    trace.speech_delivery = {
        "speech_request": {"attempted": 1, "accepted": 1, "rejected": 0},
        "tts_job": {"attempted": 1, "accepted": 1, "rejected": 0},
        "playback": {"attempted": 1, "accepted": 1, "rejected": 0},
        "first_duplicate_stage": "none",
    }
    record = trace.snapshot()
    assert record["memory"]["diagnostics"]["recall_evidence_memory_ids"] == [1]
    assert record["speech_delivery"]["playback"]["accepted"] == 1
    assert record["turn_latency"] == {}


def test_trace_disabled_creates_no_jsonl(tmp_path: Path):
    writer = TraceWriter("logs/cognitive_trace.jsonl", enabled=False, project_root=tmp_path)
    writer.write(_trace())
    assert not (tmp_path / "logs" / "cognitive_trace.jsonl").exists()


def test_trace_resolves_against_project_root_not_cwd(tmp_path: Path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    root = tmp_path / "project"
    writer = TraceWriter("logs/cognitive_trace.jsonl", enabled=True, project_root=root)
    writer.write(_trace())
    output = root / "logs" / "cognitive_trace.jsonl"
    assert output.exists()
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["turn_id"] == "turn-a"
    assert record["active_persona_id"] == "b"
    assert record["persona_leak"] is False
    assert not (elsewhere / "logs" / "cognitive_trace.jsonl").exists()
    writer.close()


def test_trace_deduplicates_same_turn(tmp_path: Path):
    writer = TraceWriter("logs/cognitive_trace.jsonl", enabled=True, project_root=tmp_path)
    assert writer.emit(_trace("same-turn")) is True
    assert writer.emit(_trace("same-turn")) is False
    writer.flush()
    lines = (tmp_path / "logs" / "cognitive_trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    writer.close()


def test_trace_write_error_is_recorded_without_raising(tmp_path: Path):
    writer = TraceWriter("logs/cognitive_trace.jsonl", enabled=True, project_root=tmp_path)
    writer._path = tmp_path / "logs"  # a directory cannot be opened as a JSONL file
    assert writer.emit(_trace("write-error")) is True
    writer.flush()
    assert writer.status()["trace_last_error"].startswith("write:")
    writer.close()


def test_trace_snapshot_contains_no_conversation_body_fields():
    record = _trace().snapshot()
    serialized = json.dumps(record, ensure_ascii=False)
    for forbidden in ('"prompt"', '"content"', '"text"'):
        assert forbidden not in serialized
