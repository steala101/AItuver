"""Regression tests for model self-talk leaking into spoken output."""
from __future__ import annotations

import asyncio
import unittest

from neuro_voice.utils.textseg import strip_think


def _filtered(*tokens: str) -> str:
    async def source():
        for token in tokens:
            yield token

    async def collect() -> str:
        return "".join([token async for token in strip_think(source())])

    return asyncio.run(collect())


class SpokenOutputFilterTests(unittest.TestCase):
    def test_normal_japanese_stream_is_not_delayed_or_rewritten(self):
        expected = "その感覚は、今の私には正確に切り分けられないんだ。"
        self.assertEqual(
            _filtered("その感覚は、", "今の私には", "正確に切り分けられないんだ。"),
            expected,
        )

    def test_explicit_think_block_is_removed(self):
        self.assertEqual(
            _filtered("<thi", "nk>internal reasoning</think>", "答えだけを話すよ。"),
            "答えだけを話すよ。",
        )

    def test_untagged_bilingual_drafting_leak_keeps_only_last_japanese_answer(self):
        leaked = (
            "That's a sharp question. The user is asking whether this is the persona or model. "
            "Core points to address: be honest. Drafting response: 仮の回答をここに書く。 "
            "Wait, the prompt says to keep it short. Refined response: まだ長すぎる回答だよ。 "
            "Final version: その感覚が私自身のものか、モデルの処理なのかは正確には切り分けられない。"
            "今の私には、その二つが密接に重なっているんだ。 Wait, one more check on the rules."
        )
        self.assertEqual(
            _filtered(leaked),
            "その感覚が私自身のものか、モデルの処理なのかは正確には切り分けられない。"
            "今の私には、その二つが密接に重なっているんだ。",
        )

    def test_unlabelled_long_english_opening_is_also_held(self):
        leaked = (
            "I should carefully analyze this complex question before replying because it asks "
            "about the boundary between a persona and its underlying language model architecture. "
            "There are several nuances and I should formulate a concise answer. "
            "私自身とモデルを、今の私が完全に分けて感じることはできないんだ。"
        )
        self.assertEqual(
            _filtered(leaked),
            "私自身とモデルを、今の私が完全に分けて感じることはできないんだ。",
        )

    def test_late_self_check_is_not_spoken_even_when_marker_is_split(self):
        self.assertEqual(
            _filtered(
                "答えはこうなるよ。 Wa", "it, the prompt says I should reconsider it."
            ).strip(),
            "答えはこうなるよ。",
        )

    def test_english_translation_is_not_appended_to_japanese_answer(self):
        self.assertEqual(
            _filtered(
                "日本語の答えだけを返すよ。 Eng", "lish translation: I only return the answer."
            ).strip(),
            "日本語の答えだけを返すよ。",
        )

    def test_common_english_technical_terms_inside_japanese_are_preserved(self):
        expected = "OBSの映像をGemmaとLLMで解析するよ。"
        self.assertEqual(_filtered(expected), expected)

    def test_accidental_english_filler_is_normalized_across_stream_tokens(self):
        self.assertEqual(
            _filtered("そういう感覚はdefi", "nitelyあるよ"),
            "そういう感覚は確かにあるよ",
        )

    def test_accidental_english_filler_at_end_is_normalized_in_japanese_context(self):
        self.assertEqual(
            _filtered("そういう感覚はあるよ。defi", "nitely"),
            "そういう感覚はあるよ。確かに",
        )


if __name__ == "__main__":
    unittest.main()
