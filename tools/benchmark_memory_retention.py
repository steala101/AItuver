"""Synthetic-only Stage M retention/search diagnostic.

This tool never accepts a database path.  It creates disposable databases in a
temporary directory so an operator cannot accidentally benchmark production
Memory data.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Iterable

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from neuro_voice.mind.store import MemoryStore
from neuro_voice.mind.episodes import EpisodicMemoryService
from neuro_voice.mind.mind import Mind


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction + 0.999999)))
    return ordered[index]


def _sample(root: Path, count: int, rounds: int) -> dict[str, int | float]:
    # Include fixture construction, index build, restart, and queries in the
    # process-memory observation.  Starting after row construction understated
    # the 100k case and made the reported RAM number misleadingly small.
    tracemalloc.start()
    path = root / f"synthetic-{int(count)}.db"
    store = MemoryStore(path)
    now = time.time()
    rng = np.random.default_rng(907)
    vectors = rng.standard_normal((64, 384), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    target = vectors[0]
    noise = rng.standard_normal(384, dtype=np.float32)
    noise -= target * float(noise @ target)
    noise /= np.linalg.norm(noise)
    fast_query_vector = (
        target * .99985 + noise * np.sqrt(1.0 - .99985**2)
    ).astype(np.float32)
    fast_query = fast_query_vector.tobytes()
    full_query_vector = (
        target * .90 + noise * np.sqrt(1.0 - .90**2)
    ).astype(np.float32)
    full_query = full_query_vector.tobytes()
    rows = [
        (
            "episode",
            "計測用の合言葉は青い灯台",
            3,
            "synthetic",
            1.0,
            "observation",
            "conversation",
            "active",
            "{}",
            "b",
            "persona_private",
            target.tobytes(),
            384,
        )
    ]
    rows.extend(
        (
            "episode",
            f"synthetic unrelated record {index}",
            1,
            "synthetic",
            now + index,
            "observation",
            "conversation",
            "active",
            "{}",
            "b",
            "persona_private",
            vectors[(index % 63) + 1].tobytes(),
            384,
        )
        for index in range(1, int(count))
    )
    store._source_write(lambda conn: conn.executemany(
        "INSERT INTO memories"
        " (kind,text,importance,source,created_at,information_type,event_type,status,meta,persona_id,scope,embedding,dim)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    ))
    transcript_rows = [
        (
            "synthetic", "自動車が好き", "覚えておく", "", "",
            target.tobytes(), 384, now - 3600.0,
        )
    ]
    transcript_rows.extend(
        (
            "synthetic", f"unrelated transcript {index}", "irrelevant", "", "",
            vectors[(index % 63) + 1].tobytes(), 384, now + index,
        )
        for index in range(1, int(count))
    )
    store._source_write(lambda conn: conn.executemany(
        "INSERT INTO transcripts"
        " (speaker,user_text,assistant_text,emotion,topic,embedding,dim,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        transcript_rows,
    ))
    before = int(store.counts()["total"])
    transcript_before = int(store._conn.execute(
        "SELECT COUNT(*) FROM transcripts"
    ).fetchone()[0])
    mark = time.perf_counter()
    store.rebuild_search_index()
    index_build_ms = (time.perf_counter() - mark) * 1000.0
    index_bytes = store._search_path.stat().st_size if store._search_path.exists() else 0
    store.close()

    mark = time.perf_counter()
    store = MemoryStore(path)
    startup_ms = (time.perf_counter() - mark) * 1000.0
    recall_rebuilds = 0
    original_rebuild = store.rebuild_search_index

    def counted_rebuild() -> dict[str, int]:
        nonlocal recall_rebuilds
        recall_rebuilds += 1
        return original_rebuild()

    store.rebuild_search_index = counted_rebuild  # type: ignore[method-assign]
    store.bind_persona("b", strict=True)
    mind = object.__new__(Mind)
    mind._store = store

    class BenchmarkConfig:
        @staticmethod
        def get(key, default=None):
            return False if key == "memory.write_enabled" else default

    mind._episodes = EpisodicMemoryService(store, BenchmarkConfig())
    query_vector = [fast_query_vector]
    mind._embedder = type("BenchmarkEmbedder", (), {
        "encode": lambda self, _texts, is_query=False: np.stack(query_vector),
    })()
    mind._top_k = 5
    mind._min_score = .2
    mind._transcript_enabled = True
    mind._transcript_top_k = 5

    memory_candidate_limit = 0
    memory_candidates: list[dict] = []
    original_memory_candidates = store.search_episode_embeddings

    def measured_memory_candidates(*args, **kwargs):
        nonlocal memory_candidate_limit, memory_candidates
        memory_candidate_limit = int(kwargs.get("limit", 0))
        memory_candidates = original_memory_candidates(*args, **kwargs)
        return memory_candidates

    transcript_candidate_limit = 0
    transcript_candidates: list[dict] = []
    original_transcript_candidates = store.search_transcript_embeddings

    def measured_transcript_candidates(*args, **kwargs):
        nonlocal transcript_candidate_limit, transcript_candidates
        transcript_candidate_limit = int(kwargs.get("limit", 0))
        transcript_candidates = original_transcript_candidates(*args, **kwargs)
        return transcript_candidates

    store.search_episode_embeddings = measured_memory_candidates  # type: ignore[method-assign]
    store.search_transcript_embeddings = measured_transcript_candidates  # type: ignore[method-assign]

    timings: list[float] = []
    fast_semantic_timings: list[float] = []
    full_semantic_timings: list[float] = []
    fast_transcript_timings: list[float] = []
    full_transcript_timings: list[float] = []
    lexical_candidates: list[dict] = []
    tier_measurements: dict[str, dict[str, int]] = {}
    for _ in range(max(1, int(rounds))):
        mark = time.perf_counter()
        lexical_candidates = store.search_episodes(
            "前に覚えてもらった計測用の合言葉を教えて",
            limit=40,
            persona_id="b",
            include_archived=True,
        )
        timings.append((time.perf_counter() - mark) * 1000.0)
        query_vector[0] = fast_query_vector
        mark = time.perf_counter()
        store.search_transcript_embeddings(
            fast_query, dim=384, limit=40,
        )
        fast_transcript_timings.append((time.perf_counter() - mark) * 1000.0)
        mark = time.perf_counter()
        fast_final_memories, fast_final_transcripts = mind._recall_semantic(
            "クルマの好み"
        )
        fast_semantic_timings.append((time.perf_counter() - mark) * 1000.0)
        tier_measurements["fast"] = {
            "memory_candidate_limit": memory_candidate_limit,
            "memory_candidate_count": len(memory_candidates),
            "transcript_candidate_limit": transcript_candidate_limit,
            "transcript_candidate_count": len(transcript_candidates),
            "final_memory_count": len(fast_final_memories),
            "final_transcript_count": len(fast_final_transcripts),
        }
        query_vector[0] = full_query_vector
        mark = time.perf_counter()
        store.search_transcript_embeddings(
            full_query, dim=384, limit=40,
        )
        full_transcript_timings.append((time.perf_counter() - mark) * 1000.0)
        mark = time.perf_counter()
        full_final_memories, full_final_transcripts = mind._recall_semantic(
            "クルマの好み"
        )
        full_semantic_timings.append((time.perf_counter() - mark) * 1000.0)
        tier_measurements["full"] = {
            "memory_candidate_limit": memory_candidate_limit,
            "memory_candidate_count": len(memory_candidates),
            "transcript_candidate_limit": transcript_candidate_limit,
            "transcript_candidate_count": len(transcript_candidates),
            "final_memory_count": len(full_final_memories),
            "final_transcript_count": len(full_final_transcripts),
        }
        # Model a real recall's access bookkeeping.  It must not invalidate
        # text/ACL/status/vector index contents.
        store.touch([int(row["id"]) for row in lexical_candidates])
    after = int(store.counts()["total"])
    transcript_after = int(store._conn.execute(
        "SELECT COUNT(*) FROM transcripts"
    ).fetchone()[0])
    store.close()

    store = MemoryStore(path)
    restart_rebuilds = 0
    original_restart_rebuild = store.rebuild_search_index

    def counted_restart_rebuild() -> dict[str, int]:
        nonlocal restart_rebuilds
        restart_rebuilds += 1
        return original_restart_rebuild()

    store.rebuild_search_index = counted_restart_rebuild  # type: ignore[method-assign]
    store.search_episodes(
        "前に覚えてもらった計測用の合言葉を教えて",
        limit=8,
        persona_id="b",
        include_archived=True,
    )
    store.search_episode_embeddings(
        fast_query, dim=384, limit=80, persona_id="b", include_archived=True,
    )
    store.search_transcript_embeddings(fast_query, dim=384, limit=40)
    store.search_episode_embeddings(
        full_query, dim=384, limit=80, persona_id="b", include_archived=True,
    )
    store.search_transcript_embeddings(full_query, dim=384, limit=40)
    store.close()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "count": int(count),
        "source_rows_before": before,
        "source_rows_after": after,
        "transcript_rows_before": transcript_before,
        "transcript_rows_after": transcript_after,
        "lexical_candidate_limit": 40,
        "lexical_candidate_count": len(lexical_candidates),
        "fast_target_cosine": .99985,
        "full_target_cosine": .90,
        "fast_semantic_path": "Mind._recall_semantic",
        "full_semantic_path": "Mind._recall_semantic",
        "fast_probe_tier": "4-table-hamming-1",
        "full_probe_tier": "14-table-hamming-2",
        **{
            f"{tier}_{key}": value
            for tier, values in tier_measurements.items()
            for key, value in values.items()
        },
        "fast_final_memory_limit": 5,
        "fast_final_transcript_limit": 5,
        "full_final_memory_limit": 5,
        "full_final_transcript_limit": 5,
        "query_p50_ms": round(float(statistics.median(timings)), 3),
        "query_p95_ms": round(float(_percentile(timings, 0.95)), 3),
        "fast_semantic_query_p50_ms": round(
            float(statistics.median(fast_semantic_timings)), 3
        ),
        "fast_semantic_query_p95_ms": round(
            float(_percentile(fast_semantic_timings, 0.95)), 3
        ),
        "full_semantic_query_p50_ms": round(
            float(statistics.median(full_semantic_timings)), 3
        ),
        "full_semantic_query_p95_ms": round(
            float(_percentile(full_semantic_timings, 0.95)), 3
        ),
        "fast_transcript_query_p50_ms": round(
            float(statistics.median(fast_transcript_timings)), 3
        ),
        "fast_transcript_query_p95_ms": round(
            float(_percentile(fast_transcript_timings, 0.95)), 3
        ),
        "full_transcript_query_p50_ms": round(
            float(statistics.median(full_transcript_timings)), 3
        ),
        "full_transcript_query_p95_ms": round(
            float(_percentile(full_transcript_timings, 0.95)), 3
        ),
        "startup_ms": round(startup_ms, 3),
        "index_rebuilds_during_recall": int(recall_rebuilds),
        "index_rebuilds_after_touch_restart": int(restart_rebuilds),
        "index_build_ms": round(index_build_ms, 3),
        "peak_ram_bytes": int(peak),
        "ram_measurement_scope": "dataset_build_index_and_queries",
        "index_bytes": int(index_bytes),
    }


def run_benchmark(
    *, counts: Iterable[int] = (1000, 10_000, 100_000), rounds: int = 9,
) -> dict[str, object]:
    safe_counts = tuple(int(value) for value in counts if 0 < int(value) <= 100_000)
    safe_rounds = max(1, int(rounds))
    with tempfile.TemporaryDirectory(prefix="aituber-memory-retention-") as directory:
        root = Path(directory)
        samples = [_sample(root, count, safe_rounds) for count in safe_counts]
    return {
        "synthetic": True,
        "counts": list(safe_counts),
        "rounds": safe_rounds,
        "samples": samples,
        "note": "Exploratory machine-local measurements; not a production latency guarantee.",
        "ram_measurement_note": (
            "tracemalloc observes Python allocations only; it excludes OS RSS and "
            "SQLite/filesystem page cache memory."
        ),
        "percentile_note": (
            f"This report's p95 uses only {safe_rounds} query rounds and is exploratory, "
            "not a statistically stable tail-latency claim."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=9)
    args = parser.parse_args()
    print(json.dumps(run_benchmark(rounds=args.rounds), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
