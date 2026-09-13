from neuro_voice.tts.volume import parse_tts_volume_command


def test_explicit_ai_voice_volume_commands():
    command = parse_tts_volume_command("ポッポの声を80パーセントにして")
    assert command is not None
    assert command.mode == "set"
    assert command.value == 0.8

    louder = parse_tts_volume_command("読み上げ音量を少し上げて")
    assert louder is not None
    assert louder.mode == "adjust"
    assert louder.value == 0.1

    quieter = parse_tts_volume_command("AIの声が大きすぎるから下げて")
    assert quieter is not None
    assert quieter.mode == "adjust"
    assert quieter.value == -0.1


def test_music_and_ambiguous_volume_commands_are_not_captured():
    assert parse_tts_volume_command("音楽の音量を上げて") is None
    assert parse_tts_volume_command("音量を下げて") is None
    assert parse_tts_volume_command("この曲の声が小さいね") is None
    assert parse_tts_volume_command("この曲の声を上げて") is None


def test_volume_is_clamped_to_supported_range():
    command = parse_tts_volume_command("TTSの音量を500%にして")
    assert command is not None
    assert command.value == 2.0
