"""会話ログ記憶 (要約でなく実際の発言+感情の長期記憶) のテスト。"""
import datetime as dt
import time
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

import numpy as np

from neuro_voice.mind.mind import (
    Mind,
    assistant_transcript_wording_required,
    dedupe_transcript_rows,
    temporal_query_window,
    transcript_context_line,
)
from neuro_voice.mind.store import MemoryStore


class TranscriptStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.store = MemoryStore(Path(self._tmp.name) / "mind.db")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_roundtrip_with_emotion(self):
        vec = np.random.rand(8).astype(np.float32)
        tid = self.store.add_transcript(
            "昨日の話だよ", "そうだね、雲の話をしたね",
            speaker="チビ", emotion="joy", topic="雲",
            embedding=vec.tobytes(), dim=8,
        )
        self.assertGreater(tid, 0)
        rows = self.store.transcript_embeddings()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["emotion"], "joy")
        self.assertEqual(rows[0]["speaker"], "チビ")

    def test_between_filters_by_time(self):
        self.store.add_transcript("あ", "い")
        now = time.time()
        self.assertEqual(len(self.store.transcripts_between(now - 60, now + 60)), 1)
        self.assertEqual(self.store.transcripts_between(now + 100, now + 200), [])

    def test_recent_transcripts_is_bounded_and_chronological(self):
        for index in range(5):
            self.store.add_transcript(f"質問{index}", f"回答{index}")
        rows = self.store.recent_transcripts(limit=3)
        self.assertEqual(
            [row["user_text"] for row in rows],
            ["質問2", "質問3", "質問4"],
        )

    def test_automatic_prune_retains_expired_source_record(self):
        self.store.add_transcript("古い話", "うん")
        self.store._conn.execute(
            "UPDATE transcripts SET created_at = ?", (time.time() - 90 * 86400,),
        )
        self.store._conn.commit()
        self.assertEqual(self.store.prune_transcripts(45), 0)
        self.assertEqual(len(self.store.all_transcripts()), 1)


class TemporalQueryWindowTests(unittest.TestCase):
    def test_yesterday_window(self):
        window = temporal_query_window("昨日何を話したっけ？")
        self.assertIsNotNone(window)
        y0 = dt.datetime.combine(dt.date.today() - dt.timedelta(days=1), dt.time.min).timestamp()
        y1 = dt.datetime.combine(dt.date.today(), dt.time.min).timestamp()
        self.assertAlmostEqual(window[0], y0, delta=1)
        self.assertAlmostEqual(window[1], y1, delta=1)

    def test_requires_recall_intent(self):
        # 「昨日」があっても想起意図の語が無ければ発火しない
        self.assertIsNone(temporal_query_window("昨日は暑かったね"))
        self.assertIsNone(temporal_query_window("今日の天気は？"))
        self.assertIsNotNone(temporal_query_window("この前の話の続きだけど覚えてる？"))

    def test_day_before_yesterday_ends_at_yesterday(self):
        window = temporal_query_window("一昨日なにした？")
        y0 = dt.datetime.combine(dt.date.today() - dt.timedelta(days=1), dt.time.min).timestamp()
        self.assertAlmostEqual(window[1], y0, delta=1)


class TranscriptSearchTests(unittest.TestCase):
    def test_semantic_search_with_forgetting_curve(self):
        tmp = TemporaryDirectory()
        store = MemoryStore(Path(tmp.name) / "mind.db")
        mind = object.__new__(Mind)
        mind._transcript_enabled = True
        mind._transcript_top_k = 3
        mind._transcript_cache = None
        mind._store = store

        q = np.zeros(8, dtype=np.float32); q[0] = 1.0
        fresh = q.copy()
        far = np.zeros(8, dtype=np.float32); far[1] = 1.0
        store.add_transcript("雲の話", "面白かったね", emotion="fun",
                             embedding=fresh.tobytes(), dim=8)
        old_id = store.add_transcript("雲の話(古)", "うん", embedding=fresh.tobytes(), dim=8)
        store.add_transcript("無関係", "そう", embedding=far.tobytes(), dim=8)
        store._conn.execute(
            "UPDATE transcripts SET created_at = ? WHERE id = ?",
            (time.time() - 60 * 86400, old_id),
        )
        store._conn.commit()

        texts = [hit["user_text"] for hit in mind._search_transcripts(q)]
        self.assertIn("雲の話", texts)          # 新しい高類似はヒット
        self.assertNotIn("雲の話(古)", texts)   # 60日前は忘却カーブで落ちる
        self.assertNotIn("無関係", texts)       # 低類似は落ちる
        store.close()
        tmp.cleanup()


class TranscriptPromptTests(unittest.TestCase):
    def test_topic_continuation_does_not_request_old_assistant_wording(self):
        self.assertFalse(assistant_transcript_wording_required(
            "さっきのAI感の話だけど、結局どこが一番効くと思う？",
        ))

    def test_question_about_the_assistants_prior_claim_does_request_it(self):
        self.assertTrue(assistant_transcript_wording_required(
            "おすすめした新作ゲームって何？",
        ))
        self.assertTrue(assistant_transcript_wording_required("さっき何て言った？"))

    def test_repeated_user_surface_is_injected_only_once(self):
        rows = [
            {"id": 3, "user_text": "なるほど。", "assistant_text": "新しい回答"},
            {"id": 2, "user_text": "なるほど", "assistant_text": "古い回答"},
            {"id": 1, "user_text": "別の話", "assistant_text": "別の回答"},
        ]
        result = dedupe_transcript_rows(rows)
        self.assertEqual([row["id"] for row in result], [3, 1])

    def test_ordinary_recall_does_not_expose_old_assistant_wording(self):
        row = {
            "speaker": "チビ",
            "user_text": "なるほど",
            "assistant_text": "なるほど、それは重要ですね",
            "emotion": "neutral",
        }
        line = transcript_context_line(
            row, when="以前", include_assistant_wording=False,
        )
        self.assertIn("チビが「なるほど」について話した", line)
        self.assertNotIn("それは重要ですね", line)

    def test_explicit_conversation_recall_can_use_old_assistant_wording(self):
        row = {
            "speaker": "チビ",
            "user_text": "おすすめは？",
            "assistant_text": "このゲームがおすすめ",
            "emotion": "neutral",
        }
        line = transcript_context_line(
            row, when="昨日", include_assistant_wording=True,
        )
        self.assertIn("私「このゲームがおすすめ」", line)


if __name__ == "__main__":
    unittest.main()
