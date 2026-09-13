from neuro_voice.tts.pronunciation import (
    extract_explicit_pronunciation_correction,
    infer_pronunciation_correction,
)


def test_explicit_reading_commands_are_learned():
    assert extract_explicit_pronunciation_correction(
        "深掘りはふかぼりって読むんだよ"
    ) == ("深掘り", "ふかぼり")
    assert extract_explicit_pronunciation_correction(
        "深掘りの読み方はふかぼり"
    ) == ("深掘り", "ふかぼり")
    assert extract_explicit_pronunciation_correction(
        "深掘りはふかぼりって覚えて"
    ) == ("深掘り", "ふかぼり")


def test_conversation_correction_is_not_saved_as_reading():
    text = "今から始まる言葉をお願いねじゃなくて、今から始まる言葉を言うのはポッポだよ"
    assert infer_pronunciation_correction(text, "次は、まから始まる言葉をお願いね。") is None


def test_short_natural_correction_requires_previous_surface():
    assert infer_pronunciation_correction(
        "深掘りじゃなくて、ふかぼり", "そこを深掘りしてみよう。",
    ) == ("深掘り", "ふかぼり")
    assert infer_pronunciation_correction(
        "深掘りじゃなくて、ふかぼりって読むんだよ", "そこを深掘りしてみよう。",
    ) == ("深掘り", "ふかぼり")
    assert infer_pronunciation_correction(
        "深掘りじゃなくて、ふかぼり", "別の話をしていた。",
    ) is None
