"""Run the synthetic conversation-quality contract evaluation offline."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from neuro_voice.dialogue.conversation_quality_eval import (
    evaluate_offline_corpus, load_corpus,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("offline",), default="offline")
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    corpus = load_corpus(args.corpus)
    report = evaluate_offline_corpus(corpus)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    payload["unmeasured_axes"] = sorted(report.unmeasured_axes)
    payload["corpus_sha256"] = hashlib.sha256(args.corpus.read_bytes()).hexdigest()
    payload["mode"] = args.mode
    (args.output_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(item.passed for item in report.case_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
