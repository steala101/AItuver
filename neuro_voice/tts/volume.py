"""Explicit voice commands for the assistant's own TTS output volume."""
from __future__ import annotations

import re
from dataclasses import dataclass


_TARGET = re.compile(
    r"(?:ポッポ|ぽっぽ|AI|ＡＩ|エーアイ|あなた|きみ|君|TTS|しゃべる声|喋る声|話す声|声)"
)
_EXPLICIT_AI_TARGET = re.compile(r"(?:ポッポ|ぽっぽ|AI|ＡＩ|エーアイ|あなた|きみ|君|TTS)")
#: 「読み上げ」 names this feature only when a volume word follows it.  On its
#: own it is the ordinary verb: 「一行を読み上げてみる」 is not a request to
#: change anything, and treating it as one made the assistant answer a story
#: turn with 「声の音量を30パーセントにしたよ」.
_READ_ALOUD_TARGET = re.compile(
    r"読み上げ\s*(?:の|音声)?\s*(?:音量|ボリューム|ヴォリューム)"
)
#: Compound verbs that merely end in 上げる / 下げる.  「読み上げて」 must not
#: be read as 「(音量を)上げて」.
_COMPOUND_VERB = re.compile(
    r"(?:読み|よみ|申し|もうし|取り|とり|持ち|もち|引き|ひき|巻き|まき|"
    r"積み|つみ|盛り|もり|立ち|たち|打ち|うち|仕|し|作り|つくり|数え|かぞえ|"
    r"見|み|吊る|つる|ぶら|棚|たな)(上げ|下げ)"
)
_MUSIC_TARGET = re.compile(r"(?:音楽|曲|BGM|ＢＧＭ|YouTube|ユーチューブ)")
_PERCENT = re.compile(r"(\d{1,3})\s*(?:%|％|パーセント)")


@dataclass(frozen=True)
class TTSVolumeCommand:
    mode: str  # set / adjust
    value: float


def parse_tts_volume_command(text: str) -> TTSVolumeCommand | None:
    """Parse only commands explicitly targeting the AI voice, not music."""
    source = str(text or "").strip()
    # Strip compound verbs first so 「読み上げて」 cannot supply either half of
    # a volume command.  One verb must not be both the target and the action.
    verbs = _COMPOUND_VERB.sub("", source)
    if not (_TARGET.search(verbs) or _READ_ALOUD_TARGET.search(source)):
        return None
    # A request about the song's vocals belongs to music control, not TTS.
    if _MUSIC_TARGET.search(source) and not (
        _EXPLICIT_AI_TARGET.search(verbs) or _READ_ALOUD_TARGET.search(source)
    ):
        return None

    percent = _PERCENT.search(source)
    if percent is None and not re.search(
        r"(?:音量|ボリューム|ヴォリューム|大き|小さ|上げ|下げ|ミュート|無音|聞こえ)", verbs,
    ):
        return None
    if percent:
        return TTSVolumeCommand("set", max(0, min(200, int(percent.group(1)))) / 100.0)
    if re.search(r"(?:ミュート|無音|音を消)", source):
        return TTSVolumeCommand("set", 0.0)
    if re.search(r"(?:元に戻|標準|普通の音量|デフォルト)", source):
        return TTSVolumeCommand("set", 1.0)
    if re.search(r"(?:最大|マックス|全開)", source):
        return TTSVolumeCommand("set", 2.0)
    if re.search(r"(?:上げ|大きく|大きめ|聞こえない|聞こえにく)", source):
        return TTSVolumeCommand("adjust", 0.1)
    if re.search(r"(?:下げ|小さく|小さめ|うるさ|大きすぎ)", source):
        return TTSVolumeCommand("adjust", -0.1)
    return None
