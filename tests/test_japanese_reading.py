import unittest

from neuro_voice.activity.japanese_reading import (
    JapaneseReadingService,
    first_kana,
    last_kana,
)


class JapaneseReadingTests(unittest.TestCase):
    def test_common_kanji_and_polite_prefix_share_one_key(self):
        readings = JapaneseReadingService()
        self.assertEqual(readings.extract_word("米"), ("米", "こめ"))
        self.assertEqual(readings.extract_word("お米"), ("お米", "こめ"))

    def test_katakana_normalizes_to_hiragana(self):
        readings = JapaneseReadingService()
        self.assertEqual(readings.extract_word("パンダ"), ("パンダ", "ぱんだ"))

    def test_long_vowel_last_kana_is_phonetic(self):
        self.assertEqual(first_kana("コーヒー"), "こ")
        self.assertEqual(last_kana("コーヒー"), "い")


if __name__ == "__main__":
    unittest.main()
