"""ポッポの答えと、コードの答えを突き合わせる。

ポッポは規則を読んで自分で考える。その方が会話として生きているが、12Bの
モデルは多条件の分岐をそこそこの割合で外す。爆弾では外すと爆発するので、
**発話の前に検算する**。

判定できるものだけ判定する。「切る/切らない」「何本目」のように結論が
短い語で表れるものは比べられるが、言い回しによっては取り出せない。
**取り出せない時に否定しない。** 読めなかっただけで正しいかもしれないのに
差し替えるのは、それ自体が誤りである。

戻り値は3値:

* ``AGREE``     — 一致した。ポッポの言葉をそのまま話す
* ``DISAGREE``  — 食い違った。コードの答えへ差し替える
* ``UNCLEAR``   — 結論を取り出せない。触らない（記録だけ残す）
"""
from __future__ import annotations

import re

from neuro_voice.games.ktane.modules import Solution

AGREE = "agree"
DISAGREE = "disagree"
UNCLEAR = "unclear"

_ORDINAL = re.compile(r"(\d)\s*本目")
_POSITION = re.compile(r"左から\s*(\d)\s*番目")
_LABEL = re.compile(r"[「『]\s*(\d)\s*[」』]")
_FREQUENCY = re.compile(r"3\.\d{3}")
_WORD = re.compile(r"[「『]\s*([a-zA-Z]{3,10})\s*[」』]")
_COLOR = re.compile(r"赤|青|緑|黄")
_DIGIT = re.compile(r"(\d)\s*が出た")

_CUT = re.compile(r"切って|切る(?!な)|切っちゃ(?!だめ|ダメ)")
_KEEP = re.compile(r"切らない|切らず|切っちゃだめ|切っちゃダメ|残して")
_HOLD = re.compile(r"押しっぱなし|長押し|押し続け")
_TAP = re.compile(r"すぐ離|すぐに離|押してすぐ|短く押")


def _only(pattern: re.Pattern, text: str) -> str | None:
    """その種類の値がちょうど1つだけ出てくるなら、それを返す。

    2つ以上あると、どれが結論なのか決められない。決められないものを
    「一致した」とも「食い違った」とも言わない。
    """
    found = {match.group(1) if match.groups() else match.group(0)
             for match in pattern.finditer(text)}
    return next(iter(found)) if len(found) == 1 else None


def _both(pattern_a: re.Pattern, pattern_b: re.Pattern, text: str) -> str | None:
    """相反する2つのうち、片方だけが出ているなら 'a' か 'b'。"""
    a, b = bool(pattern_a.search(text)), bool(pattern_b.search(text))
    if a and not b:
        return "a"
    if b and not a:
        return "b"
    return None


def check(reply: str, expected: Solution) -> str:
    """ポッポの発話が、確定した答えと同じことを言っているか。"""
    said = str(reply or "")
    answer = str(expected.answer or "")
    if not said.strip() or not answer:
        return UNCLEAR

    # 何本目を切るか（配線）
    wanted = _ORDINAL.search(answer)
    if wanted:
        got = _only(_ORDINAL, said)
        if got is None:
            return UNCLEAR
        return AGREE if got == wanted.group(1) else DISAGREE

    # 切る / 切らない（複雑な配線・順番に配線）
    if _CUT.search(answer) or _KEEP.search(answer):
        wanted_side = "a" if _CUT.search(answer) else "b"
        got_side = _both(_CUT, _KEEP, said)
        if got_side is None:
            return UNCLEAR
        return AGREE if got_side == wanted_side else DISAGREE

    # 押しっぱなし / すぐ離す（ボタン）
    if _HOLD.search(answer) or _TAP.search(answer):
        wanted_side = "a" if _HOLD.search(answer) else "b"
        got_side = _both(_HOLD, _TAP, said)
        if got_side is None:
            return UNCLEAR
        return AGREE if got_side == wanted_side else DISAGREE

    # 離す数字（ボタンの帯）
    wanted_digit = _DIGIT.search(answer)
    if wanted_digit:
        got = _only(_DIGIT, said)
        if got is None:
            return UNCLEAR
        return AGREE if got == wanted_digit.group(1) else DISAGREE

    # 押す色の並び（サイモン）
    if "の順に押して" in answer:
        wanted_colors = _COLOR.findall(answer)
        got_colors = _COLOR.findall(said)
        if not got_colors:
            return UNCLEAR
        return AGREE if got_colors == wanted_colors else DISAGREE

    # 左から何番目（記憶）
    wanted_position = _POSITION.search(answer)
    if wanted_position:
        got = _only(_POSITION, said)
        if got is None:
            return UNCLEAR
        return AGREE if got == wanted_position.group(1) else DISAGREE

    # 書かれた数字（記憶）
    wanted_label = _LABEL.search(answer)
    if wanted_label:
        got = _only(_LABEL, said)
        if got is None:
            return UNCLEAR
        return AGREE if got == wanted_label.group(1) else DISAGREE

    # 周波数（モールス）
    wanted_frequency = _FREQUENCY.search(answer)
    if wanted_frequency:
        got = _only(_FREQUENCY, said)
        if got is None:
            return UNCLEAR
        return AGREE if got == wanted_frequency.group(0) else DISAGREE

    # 送信する単語（パスワード）
    wanted_word = _WORD.search(answer)
    if wanted_word:
        got = _only(_WORD, said)
        if got is None:
            return UNCLEAR
        return AGREE if got.lower() == wanted_word.group(1).lower() else DISAGREE

    return UNCLEAR


#: 「操作しろ」と言っている表現。これが出たら**取り返しがつかない**。
#:
#: 実機で、表が空のキーパッドに対して「一番上の『プサイ』のボタンを押して」と
#: 言い、爆発した。コードは何も答えていない。ポッポが自分で作った。
#: 検算は「コードの答えがある時」しか働かないので、**答えが無い時に
#: 指示させない**ための網が別に要る。
_INSTRUCTION = re.compile(
    r"切って|切る(?!な)|カットして|"
    r"押して|押しっぱなし|離して|"
    r"周波数を\s*3\.\d{3}|"
    r"[「『][a-zA-Z]{3,10}[」』]\s*で?送信|"
    r"の順に押"
)
#: 仮定や説明として操作の語が出るのは止めない。「切ったらどうなる？」など。
_HYPOTHETICAL = re.compile(
    r"(?:たら|れば|なら|かも|と思う|だっけ|かな[？?]|でいい[？?]|"
    r"どっち|どれ|わからな|分からな|教えて|確認)"
)


def contains_instruction(reply: str) -> bool:
    """操作を指示しているか。仮定・質問はここでは拾わない。"""
    text = str(reply or "")
    if not _INSTRUCTION.search(text):
        return False
    # 「切ってもいいのかな？」のように、尋ねている文は指示ではない。
    return not _HYPOTHETICAL.search(text)


def ungrounded_refusal(missing: str = "") -> str:
    """根拠の無い指示を差し替える一文。**推測で操作させない。**"""
    text = "ちょっと待って、まだ判断できてない。適当に言うと爆発しちゃう"
    if missing:
        text += f"。{missing}"
    return text + "。"


def correction(expected: Solution) -> str:
    """食い違った時に代わりに話す一文。

    ポッポの誤った指示は**読み上げない**。全文を保持してから話す作りなので、
    ここで差し替えれば耳には届かない。自分で気づいて言い直した形にする。
    """
    text = f"んー……ちょっと待って。ここは{expected.answer}"
    if expected.because:
        text += f"。{expected.because}だから"
    if expected.follow_up:
        text += f"。{expected.follow_up}"
    return text + "。"
