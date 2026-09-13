"""話し言葉から、爆弾の状態とモジュールの入力を読み取る。

音声で伝えられるので、書式は期待できない。「赤、青、赤の3本」「上から白黒白黄」
「シリアルはA7B3C1」「電池2個」——どれも同じ意味を持つ。

**読み取れなかったものを埋めない。** 分からない色を勝手に赤とみなすより、
一つ聞き返す方がよい。Whisperの誤変換もあるので、確信の持てない断片は捨てる。
"""
from __future__ import annotations

import re

from neuro_voice.games.ktane.edgework import INDICATOR_LABELS, Edgework

#: 色の呼び方。ひらがな・カタカナ・漢字・英語のどれで来ても拾う。
_COLORS: tuple[tuple[str, str], ...] = (
    ("red", r"赤|あか|アカ|レッド|red"),
    ("blue", r"青|あお|アオ|ブルー|blue"),
    ("yellow", r"黄色|黄|きいろ|キイロ|イエロー|yellow"),
    ("white", r"白|しろ|シロ|ホワイト|white"),
    ("black", r"黒|くろ|クロ|ブラック|black"),
    ("green", r"緑|みどり|ミドリ|グリーン|green"),
)
_COLOR_TOKEN = re.compile("|".join(f"(?P<{name}>{pattern})" for name, pattern in _COLORS))

#: 「3本」「4ぼん」「五本」
_KANJI_DIGITS = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                 "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_COUNT = re.compile(r"(\d+|[〇一二三四五六七八九十])\s*(?:本|個|つ|回)")
_SERIAL = re.compile(
    r"(?:シリアル|しりある|serial|通し番号)[^0-9A-Za-z]{0,8}([0-9A-Za-z\s]{4,20})",
    re.IGNORECASE,
)
_BATTERY = re.compile(r"(?:電池|バッテリー|battery)[^0-9〇一二三四五六七八九十]{0,6}(\d+|[〇一二三四五六七八九十])")
_STRIKES = re.compile(r"(?:ミス|失敗|ストライク|strike)[^0-9〇一二三四五六七八九十]{0,6}(\d+|[〇一二三四五六七八九十])")
_PORTS: tuple[tuple[str, str], ...] = (
    ("Parallel", r"パラレル|並列|parallel"),
    ("Serial", r"シリアルポート|serial\s*port"),
    ("DVI-D", r"dvi"),
    ("PS/2", r"ps\s*/?\s*2|ピーエスツー"),
    ("RJ-45", r"rj\s*-?\s*45|アールジェイ"),
    ("Stereo RCA", r"rca|ステレオ"),
)


def _number(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _KANJI_DIGITS.get(token)


def parse_colors(text: str) -> list[str]:
    """出てきた順に色を並べる。数を数える語（「3本」）は色ではないので拾わない。"""
    return [match.lastgroup for match in _COLOR_TOKEN.finditer(str(text or ""))]


def parse_count(text: str) -> int | None:
    match = _COUNT.search(str(text or ""))
    return _number(match.group(1)) if match else None


def parse_serial(text: str) -> str:
    """シリアル番号。空白を詰め、英数字だけ残す。

    KTANEのシリアルは6桁なので、それより長い塊は読み違えとみなして捨てる。
    誤った末尾の数字は、そのまま誤った線を切らせる。
    """
    match = _SERIAL.search(str(text or ""))
    if not match:
        return ""
    value = re.sub(r"[^0-9A-Za-z]", "", match.group(1)).upper()
    return value if 4 <= len(value) <= 8 else ""


def parse_indicators(text: str) -> tuple[set[str], set[str]]:
    """点灯・消灯のインジケーター。ラベルが出ていないものは拾わない。"""
    value = str(text or "").upper()
    lit: set[str] = set()
    unlit: set[str] = set()
    for label in INDICATOR_LABELS:
        for match in re.finditer(re.escape(label), value):
            window = value[max(0, match.start() - 14):match.end() + 14]
            if re.search(r"消|きえ|消灯|UNLIT|点いてい?な|ついてな", window):
                unlit.add(label)
            else:
                lit.add(label)
    return lit, unlit


def parse_ports(text: str) -> set[str]:
    value = str(text or "")
    found = set()
    for kind, pattern in _PORTS:
        if re.search(pattern, value, re.IGNORECASE):
            found.add(kind)
    return found


def update_edgework(edge: Edgework, text: str) -> list[str]:
    """発話から拾えたものだけを反映する。何を更新したかを返す。"""
    changed: list[str] = []
    serial = parse_serial(text)
    if serial and serial != edge.serial:
        edge.serial = serial
        changed.append("シリアル")
    battery = _BATTERY.search(str(text or ""))
    if battery:
        count = _number(battery.group(1))
        if count is not None and count != edge.batteries:
            edge.batteries = count
            changed.append("電池")
    lit, unlit = parse_indicators(text)
    if lit - edge.lit_indicators or unlit - edge.unlit_indicators:
        edge.lit_indicators |= lit
        edge.unlit_indicators |= unlit
        changed.append("インジケーター")
    ports = parse_ports(text)
    if ports - edge.ports:
        edge.ports |= ports
        changed.append("ポート")
    strikes = _STRIKES.search(str(text or ""))
    if strikes:
        count = _number(strikes.group(1))
        if count is not None and count != edge.strikes:
            edge.strikes = count
            changed.append("ミス")
    return changed


#: モジュール名の呼び方。ここに無い言い方は「どのモジュール？」と聞き返す。
MODULE_ALIASES: tuple[tuple[str, str], ...] = (
    ("complicated_wires", r"複雑な配線|複雑なワイヤー|ふくざつな(?:はいせん|配線)|complicated"),
    ("wire_sequences", r"順番に配線|配線の順番|ワイヤーシーケンス|順番のワイヤー|sequence"),
    ("wires", r"配線|ワイヤー|線が|はいせん|wires?"),
    ("button", r"ボタン|でかいボタン|button"),
    ("keypad", r"キーパッド|記号|シンボル|keypad"),
    ("simon", r"サイモン|光る|色が光|simon"),
    ("whos_on_first", r"心理戦|フーズオン|who'?s on first"),
    ("memory", r"記憶|メモリ|memory"),
    ("morse", r"モールス|もーるす|morse"),
    ("maze", r"迷路|めいろ|maze"),
    ("password", r"パスワード|password"),
)


def parse_module(text: str) -> str:
    value = str(text or "")
    for name, pattern in MODULE_ALIASES:
        if re.search(pattern, value, re.IGNORECASE):
            return name
    return ""


# ---------------------------------------------------------------------------
# 表が要る3モジュールの入力
# ---------------------------------------------------------------------------

_SYMBOL_SPLIT = re.compile(r"[、,，\s]+と?|それから|あと")
#: 呼び名の**前後**だけを削る。
#:
#: 中の助詞まで落とすと「しっぽ付きの6」が「しっぽ付き6」になり、
#: `keypad.json` に書いた呼び名と一致しなくなる。呼び名は利用者が決めたもので、
#: こちらが整形してよいものではない。
_SYMBOL_HEAD = re.compile(r"^(?:記号|シンボル|マーク|キーパッド|モジュール|は|が|の|を|、)+")
_SYMBOL_TAIL = re.compile(r"(?:です|だよ|かな|みたいなやつ|みたいな|っぽいの|っぽい|[。．！!？?、])+$")


def parse_symbols(text: str, limit: int = 4) -> list[str]:
    """キーパッドの記号の呼び名。

    公式の記号に決まった読み方は無いので、**利用者が付けた呼び名をそのまま
    受け取る**。「しっぽ付きの6」「逆さのC」で構わない。同じ呼び名で
    `keypad.json` を書いてもらえば当たる。
    """
    value = str(text or "")
    if "記号" not in value and "シンボル" not in value and "キーパッド" not in value:
        return []
    body = re.split(r"記号|シンボル", value, maxsplit=1)[-1]
    parts = []
    for chunk in _SYMBOL_SPLIT.split(body):
        name = _SYMBOL_TAIL.sub("", _SYMBOL_HEAD.sub("", chunk.strip())).strip()
        if name:
            parts.append(name)
    # 既定4件は「盤面の記号は4つ」だから。表を教える時は1列7個あるので、
    # 呼び出し側が上限を上げる。ここで固定すると7個目が黙って消える。
    return parts[:max(1, int(limit))]


def parse_words(text: str) -> list[str]:
    """心理戦のボタンに書かれた語。英字の連なりだけを拾う。"""
    return re.findall(r"[A-Za-z][A-Za-z']{0,11}", str(text or ""))


# ---------------------------------------------------------------------------
# ボタンに書いてある文字
#
# 実機で「長押しとか押すとかが通じず、push / hold と英語で言わないと伝わらない
# 場面が結構あった」と言われた。原因は2つある。
#
# 1. こちらが「Abort / Detonate / Hold / Press」と英語で聞き返していた。
#    聞き方が答え方を決めてしまう
# 2. 「押す」を語彙から外していた。動詞と同じ形なので「ボタンを押すね」で
#    label=press と誤読するのを恐れたため
#
# 2番目の直し方を間違えていた。**衝突する語を捨てるのではなく、文字を
# 読み上げている場面かどうかで分ける。** 語を捨てると、いちばん自然な
# 言い方だけが通らないという歪んだ挙動になる。
# ---------------------------------------------------------------------------

#: 動詞と紛れない言い方。いつ出てきても文字として採ってよい。
_BUTTON_LABELS: tuple[tuple[str, str], ...] = (
    ("abort", "abort"), ("アボート", "abort"), ("中止", "abort"), ("中断", "abort"),
    ("detonate", "detonate"), ("デトネート", "detonate"),
    ("起爆", "detonate"), ("爆破", "detonate"),
    ("hold", "hold"), ("ホールド", "hold"), ("保持", "hold"),
    ("press", "press"), ("プレス", "press"), ("push", "press"), ("プッシュ", "press"),
)
#: **動詞としても普通に出る言い方。** 文字を読んでいる場面でだけ採る。
_SPOKEN_LABELS: tuple[tuple[str, str], ...] = (
    ("長押し", "hold"), ("ながおし", "hold"), ("押しっぱなし", "hold"), ("押し続け", "hold"),
    ("押す", "press"), ("押せ", "press"), ("おす", "press"),
)
#: 文字を読み上げている合図。
_READING_LABEL = re.compile(
    r"書いて|書かれ|表示|文字|ラベル|label|と読|って(?:ある|なってる|書)|と書"
)
#: 「押したよ」は文字ではなく**押した報告**。ここを文字として採ると、
#: 押し終わった後に別の指示が確定する。
_PRESSED_REPORT = re.compile(r"押しし?(?:た|ました|ちゃ|てみ|とい)")


def parse_button_label(text: str, *, expected: bool = False) -> str:
    """ボタンに書いてある文字。読み取れなければ空文字。

    `expected` は「直前にこちらが文字を尋ねた」かどうか。尋ねた直後なら
    発話まるごとが答えなので、「長押し」の一言でも文字として受け取る。
    尋ねていなければ、読み上げている合図がある時だけ採る。
    """
    value = str(text or "")
    lowered = value.lower()
    for word, canonical in _BUTTON_LABELS:
        if word in lowered:
            return canonical
    if _PRESSED_REPORT.search(value):
        return ""
    if not (expected or _READING_LABEL.search(value)):
        return ""
    for word, canonical in _SPOKEN_LABELS:
        if word in value:
            return canonical
    return ""


_COORDINATE = re.compile(
    r"(?:左|よこ|横|列)?\s*から?\s*(\d)\s*(?:番目|列目|つ目)?[^0-9]{0,8}?"
    r"(?:上|たて|縦|行)\s*から?\s*(\d)\s*(?:番目|行目|つ目)?"
)
_PAIR = re.compile(r"\(?\s*(\d)\s*[,、･・]\s*(\d)\s*\)?")


def parse_coordinates(text: str) -> list[tuple[int, int]]:
    """迷路の座標。「左から2、上から3」でも「2,3」でも受ける。

    どちらも (列, 行) の1始まり。6×6の外は捨てる。
    """
    value = str(text or "")
    found: list[tuple[int, int]] = []
    for column, row in _COORDINATE.findall(value):
        found.append((int(column), int(row)))
    if not found:
        for column, row in _PAIR.findall(value):
            found.append((int(column), int(row)))
    return [point for point in found if 1 <= point[0] <= 6 and 1 <= point[1] <= 6]
