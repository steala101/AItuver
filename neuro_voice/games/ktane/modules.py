"""各モジュールの解法。**判断はここで決める。**

ポッポは12Bのローカルモデルで動いている。「赤が2本以上あって、かつシリアルの
末尾が奇数なら最後の赤」のような多条件の分岐をモデルに解かせると、そこそこの
確率で間違える。爆弾の場合、間違いは爆発である。

だから条件分岐はすべてこのモジュールが決め、モデルには**確定した答えを
自分の言葉で伝える**役だけを任せる（第2条）。

## 実装していないモジュールについて

自信のない規則は**書かない**。`Solution.unknown` を返して「手元に規則がない」と
言わせる。間違った指示は「分からない」よりはるかに悪い。

- キーパッド（記号）: 記号の見た目を音声で特定できない
- 心理戦（Who's on First）: 対応表が大きく、記憶だけでは正確性を保証できない
- 迷路: 格子の絵が要る

これらは埋めるなら、公式マニュアルを見ながらデータとして起こすこと。
**推測で足さないこと。**
"""
from __future__ import annotations

from dataclasses import dataclass, field

from neuro_voice.games.ktane.edgework import Edgework


@dataclass(slots=True)
class Solution:
    """一つの判断の結果。答えか、足りないものか、規則が無いかのどれか。"""

    #: 確定した指示。空なら未確定。
    answer: str = ""
    #: どの規則でそうなったか。ユーザーが検算できるように短く残す。
    because: str = ""
    #: 答えるのに足りない情報。埋まっていれば、これを**一つだけ**尋ねる。
    needs: str = ""
    #: 規則自体が手元に無い。
    unknown: bool = False
    #: 段階のあるモジュールで、次に必要な操作。
    follow_up: str = ""

    @property
    def solved(self) -> bool:
        return bool(self.answer)


def _need(what: str) -> Solution:
    return Solution(needs=what)


def unknown_module(name: str) -> Solution:
    return Solution(
        unknown=True,
        because=f"{name}の規則は手元のマニュアルに入っていない",
    )


# ---------------------------------------------------------------------------
# 配線（3〜6本）
# ---------------------------------------------------------------------------

_ORDINAL = ("1本目", "2本目", "3本目", "4本目", "5本目", "6本目")


def _ordinal(index: int) -> str:
    """1始まりの位置を日本語にする。"""
    return _ORDINAL[index - 1] if 1 <= index <= len(_ORDINAL) else f"{index}本目"


def solve_wires(colors: list[str], edge: Edgework) -> Solution:
    """上から順に並べた色から、切る線を決める。

    色は "red" "blue" "yellow" "white" "black" の英小文字で受け取る。
    日本語からの変換はパーサ側の仕事。
    """
    count = len(colors)
    if not 3 <= count <= 6:
        return _need("配線が何本あるか（3〜6本のはず）")

    def n(color: str) -> int:
        return colors.count(color)

    def last_index_of(color: str) -> int:
        return len(colors) - 1 - colors[::-1].index(color) + 1

    odd = edge.serial_last_digit_is_odd

    if count == 3:
        if n("red") == 0:
            return Solution(f"{_ordinal(2)}を切って", "3本で赤が無いとき")
        if colors[-1] == "white":
            return Solution(f"{_ordinal(3)}を切って", "3本で最後が白のとき")
        if n("blue") > 1:
            return Solution(f"{_ordinal(last_index_of('blue'))}を切って", "3本で青が2本以上のとき、最後の青")
        return Solution(f"{_ordinal(3)}を切って", "3本で、どの条件にも当たらないとき")

    if count == 4:
        if n("red") > 1:
            if odd is None:
                return _need("シリアル番号（末尾の数字が奇数か偶数かで変わる）")
            if odd:
                return Solution(
                    f"{_ordinal(last_index_of('red'))}を切って",
                    "4本で赤が2本以上、シリアル末尾が奇数のとき、最後の赤",
                )
        if colors[-1] == "yellow" and n("red") == 0:
            return Solution(f"{_ordinal(1)}を切って", "4本で最後が黄、赤が無いとき")
        if n("blue") == 1:
            return Solution(f"{_ordinal(1)}を切って", "4本で青がちょうど1本のとき")
        if n("yellow") > 1:
            return Solution(f"{_ordinal(4)}を切って", "4本で黄が2本以上のとき")
        return Solution(f"{_ordinal(2)}を切って", "4本で、どの条件にも当たらないとき")

    if count == 5:
        if colors[-1] == "black":
            if odd is None:
                return _need("シリアル番号（末尾の数字が奇数か偶数かで変わる）")
            if odd:
                return Solution(f"{_ordinal(4)}を切って", "5本で最後が黒、シリアル末尾が奇数のとき")
        if n("red") == 1 and n("yellow") > 1:
            return Solution(f"{_ordinal(1)}を切って", "5本で赤が1本、黄が2本以上のとき")
        if n("black") == 0:
            return Solution(f"{_ordinal(2)}を切って", "5本で黒が無いとき")
        return Solution(f"{_ordinal(1)}を切って", "5本で、どの条件にも当たらないとき")

    if n("yellow") == 0:
        if odd is None:
            return _need("シリアル番号（末尾の数字が奇数か偶数かで変わる）")
        if odd:
            return Solution(f"{_ordinal(3)}を切って", "6本で黄が無く、シリアル末尾が奇数のとき")
    if n("yellow") == 1 and n("white") > 1:
        return Solution(f"{_ordinal(4)}を切って", "6本で黄が1本、白が2本以上のとき")
    if n("red") == 0:
        return Solution(f"{_ordinal(6)}を切って", "6本で赤が無いとき")
    return Solution(f"{_ordinal(4)}を切って", "6本で、どの条件にも当たらないとき")


# ---------------------------------------------------------------------------
# ボタン
# ---------------------------------------------------------------------------

#: 押し続けた時、帯の色ごとに「離してよいタイマーの数字」。
_STRIP_RELEASE = {
    "blue": "4", "white": "1", "yellow": "5",
}


def solve_button(color: str, label: str, edge: Edgework) -> Solution:
    """色と文字から、押して離すか、押し続けるかを決める。"""
    color = (color or "").lower()
    label = (label or "").lower()
    if not color or not label:
        return _need("ボタンの色と、書いてある文字")

    hold = Solution(
        "押しっぱなしにして。帯の色を教えて",
        "",
        follow_up="帯の色で離すタイミングが変わる（青=4、白=1、黄=5、それ以外=1）",
    )

    if color == "blue" and label == "abort":
        return Solution(hold.answer, "青で『Abort』のとき", follow_up=hold.follow_up)
    if label == "detonate":
        if edge.batteries is None:
            return _need("電池の数（1個より多いかどうかで変わる）")
        if edge.batteries > 1:
            return Solution("すぐ押して、すぐ離して", "『Detonate』で電池が2個以上のとき")
    if color == "white" and edge.lit("CAR"):
        return Solution(hold.answer, "白でCARが点灯しているとき", follow_up=hold.follow_up)
    if edge.batteries is not None and edge.batteries > 2 and edge.lit("FRK"):
        return Solution("すぐ押して、すぐ離して", "電池が3個以上でFRKが点灯しているとき")
    if color == "red" and label == "hold":
        return Solution("すぐ押して、すぐ離して", "赤で『Hold』のとき")
    if color == "white" and not edge.known("indicators"):
        return _need("点灯しているインジケーター（CARが点いているかで変わる）")
    if edge.batteries is None:
        return _need("電池の数")
    return Solution(hold.answer, "どの条件にも当たらないとき", follow_up=hold.follow_up)


def release_on(strip_color: str) -> Solution:
    """押し続けている間に、帯の色から離す数字を決める。"""
    color = (strip_color or "").lower()
    if not color:
        return _need("光っている帯の色")
    digit = _STRIP_RELEASE.get(color, "1")
    reason = {
        "blue": "帯が青のとき", "white": "帯が白のとき", "yellow": "帯が黄のとき",
    }.get(color, "帯がそれ以外の色のとき")
    return Solution(f"タイマーのどこかに{digit}が出た瞬間に離して", reason)


# ---------------------------------------------------------------------------
# サイモン（色の押し替え）
# ---------------------------------------------------------------------------

_SIMON_WITH_VOWEL = (
    {"red": "blue", "blue": "red", "green": "yellow", "yellow": "green"},
    {"red": "yellow", "blue": "green", "green": "blue", "yellow": "red"},
    {"red": "green", "blue": "red", "green": "yellow", "yellow": "blue"},
)
_SIMON_NO_VOWEL = (
    {"red": "blue", "blue": "yellow", "green": "green", "yellow": "red"},
    {"red": "red", "blue": "blue", "green": "yellow", "yellow": "green"},
    {"red": "yellow", "blue": "green", "green": "blue", "yellow": "red"},
)
_COLOR_JA = {
    "red": "赤", "blue": "青", "green": "緑", "yellow": "黄",
    "white": "白", "black": "黒",
}


def solve_simon(flashes: list[str], edge: Edgework) -> Solution:
    """光った順から、押す順を出す。シリアルに母音があるかで表が変わる。"""
    if not flashes:
        return _need("光った色の順番")
    vowel = edge.serial_has_vowel
    if vowel is None:
        return _need("シリアル番号（母音が入っているかで表が変わる）")
    table = (_SIMON_WITH_VOWEL if vowel else _SIMON_NO_VOWEL)[min(edge.strikes, 2)]
    pressed = [table.get(color, color) for color in flashes]
    order = "、".join(_COLOR_JA.get(color, color) for color in pressed)
    return Solution(
        f"{order} の順に押して",
        f"シリアルに母音が{'ある' if vowel else 'ない'}、ミス{min(edge.strikes, 2)}回のときの対応",
    )


# ---------------------------------------------------------------------------
# 記憶
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MemoryPress:
    position: int
    label: int


def solve_memory(stage: int, display: int, history: list[MemoryPress]) -> Solution:
    """段階ごとに「位置で押す」か「数字で押す」かが変わる。

    履歴が要る唯一のモジュール。前の段階で**どこを押して何が書いてあったか**を
    両方覚えていないと4段目以降が解けない。
    """
    if not 1 <= stage <= 5:
        return _need("今が何段目か")
    if not 1 <= display <= 4:
        return _need("画面に出ている数字")

    def by_position(index: int, why: str) -> Solution:
        return Solution(f"左から{index}番目のボタンを押して", why)

    def by_label(value: int, why: str) -> Solution:
        return Solution(f"「{value}」と書いてあるボタンを押して", why)

    def past(index: int) -> MemoryPress | None:
        return history[index - 1] if len(history) >= index else None

    if stage == 1:
        return by_position(
            {1: 2, 2: 2, 3: 3, 4: 4}[display],
            f"1段目で画面が{display}のとき",
        )
    if stage == 2:
        if display == 1:
            return by_label(4, "2段目で画面が1のとき")
        if display in (2, 4):
            first = past(1)
            if first is None:
                return _need("1段目で押した位置")
            return by_position(first.position, f"2段目で画面が{display}のとき、1段目と同じ位置")
        return by_position(1, "2段目で画面が3のとき")
    if stage == 3:
        if display == 1:
            second = past(2)
            if second is None:
                return _need("2段目で押したボタンの数字")
            return by_label(second.label, "3段目で画面が1のとき、2段目で押した数字")
        if display == 2:
            first = past(1)
            if first is None:
                return _need("1段目で押したボタンの数字")
            return by_label(first.label, "3段目で画面が2のとき、1段目で押した数字")
        if display == 3:
            return by_position(3, "3段目で画面が3のとき")
        return by_label(4, "3段目で画面が4のとき")
    if stage == 4:
        if display == 1:
            first = past(1)
            if first is None:
                return _need("1段目で押した位置")
            return by_position(first.position, "4段目で画面が1のとき、1段目と同じ位置")
        if display == 2:
            return by_position(1, "4段目で画面が2のとき")
        second = past(2)
        if second is None:
            return _need("2段目で押した位置")
        return by_position(second.position, f"4段目で画面が{display}のとき、2段目と同じ位置")

    wanted = {1: 1, 2: 2, 3: 4, 4: 3}[display]
    press = past(wanted)
    if press is None:
        return _need(f"{wanted}段目で押したボタンの数字")
    return by_label(press.label, f"5段目で画面が{display}のとき、{wanted}段目で押した数字")


# ---------------------------------------------------------------------------
# モールス信号
# ---------------------------------------------------------------------------

MORSE_WORDS = {
    "shell": "3.505", "halls": "3.515", "slick": "3.522", "trick": "3.532",
    "boxes": "3.535", "leaks": "3.542", "strobe": "3.545", "bistro": "3.552",
    "flick": "3.555", "bombs": "3.565", "break": "3.572", "brick": "3.575",
    "steak": "3.582", "sting": "3.592", "vector": "3.595", "beats": "3.600",
}
_MORSE_LETTERS = {
    ".-": "a", "-...": "b", "-.-.": "c", "-..": "d", ".": "e", "..-.": "f",
    "--.": "g", "....": "h", "..": "i", ".---": "j", "-.-": "k", ".-..": "l",
    "--": "m", "-.": "n", "---": "o", ".--.": "p", "--.-": "q", ".-.": "r",
    "...": "s", "-": "t", "..-": "u", "...-": "v", ".--": "w", "-..-": "x",
    "-.--": "y", "--..": "z",
}


def decode_morse(signal: str) -> str:
    """トン・ツーの並びを文字へ。読めない符号は無視せず空を返す。"""
    letters = []
    for token in str(signal or "").replace("　", " ").split():
        letter = _MORSE_LETTERS.get(token)
        if letter is None:
            return ""
        letters.append(letter)
    return "".join(letters)


def solve_morse(letters: str) -> Solution:
    """読み取れた文字から周波数を出す。途中まででも一つに絞れれば答える。"""
    value = "".join(char for char in str(letters or "").lower() if char.isalpha())
    if not value:
        return _need("読み取れた文字（最初の何文字かでよい）")
    matches = [word for word in MORSE_WORDS if word.startswith(value)]
    if not matches:
        matches = [word for word in MORSE_WORDS if value in word]
    if not matches:
        return Solution(
            because=f"「{value}」に当たる単語がマニュアルの一覧に無い",
            needs="もう一周見て、読み取れた文字をもう一度",
        )
    if len(matches) > 1:
        return Solution(
            because=f"候補が{len(matches)}件（{'、'.join(matches)}）",
            needs="次の1文字",
        )
    word = matches[0]
    return Solution(
        f"周波数を {MORSE_WORDS[word]} に合わせて送信して",
        f"「{word}」に対応する周波数",
    )


# ---------------------------------------------------------------------------
# 複雑な配線
# ---------------------------------------------------------------------------

#: (赤, 青, 星, LED) → 判定記号。
#: C=切る / D=切らない / S=シリアル末尾が偶数なら切る /
#: P=並列ポートがあれば切る / B=電池2個以上なら切る
_COMPLICATED = {
    (False, False, False, False): "C",
    (True, False, False, False): "S",
    (True, False, True, False): "C",
    (True, False, False, True): "B",
    (True, False, True, True): "B",
    (False, True, False, False): "S",
    (False, True, True, False): "D",
    (False, True, False, True): "P",
    (False, True, True, True): "D",
    (True, True, False, False): "S",
    (True, True, True, False): "P",
    (True, True, False, True): "S",
    (True, True, True, True): "D",
    (False, False, True, False): "C",
    (False, False, False, True): "D",
    (False, False, True, True): "B",
}


def solve_complicated_wire(
    *, red: bool, blue: bool, star: bool, led: bool, edge: Edgework,
) -> Solution:
    """1本ぶんの判定。赤/青の有無、星印、LEDの点灯で決まる。"""
    code = _COMPLICATED[(bool(red), bool(blue), bool(star), bool(led))]
    described = "、".join(filter(None, [
        "赤" if red else "", "青" if blue else "", "星印" if star else "", "LED点灯" if led else "",
    ])) or "何も無い"
    if code == "C":
        return Solution("切って", f"{described}の組み合わせ")
    if code == "D":
        return Solution("切らないで", f"{described}の組み合わせ")
    if code == "S":
        odd = edge.serial_last_digit_is_odd
        if odd is None:
            return _need("シリアル番号（末尾が偶数かどうかで変わる）")
        return Solution(
            "切って" if not odd else "切らないで",
            f"{described}なのでシリアル末尾が偶数なら切る。末尾は{'奇数' if odd else '偶数'}",
        )
    if code == "P":
        if not edge.known("ports"):
            return _need("ポートの種類（並列ポートがあるかどうか）")
        has = edge.has_port("Parallel")
        return Solution(
            "切って" if has else "切らないで",
            f"{described}なので並列ポートがあれば切る。ポートは{'ある' if has else 'ない'}",
        )
    if edge.batteries is None:
        return _need("電池の数（2個以上かどうかで変わる）")
    enough = edge.batteries >= 2
    return Solution(
        "切って" if enough else "切らないで",
        f"{described}なので電池2個以上なら切る。電池は{edge.batteries}個",
    )


# ---------------------------------------------------------------------------
# 順番に配線
# ---------------------------------------------------------------------------

#: 色ごとに、その色が何本目に出てきたかで「切ってよい接続先」が決まる。
_SEQUENCE = {
    "red": ("C", "B", "A", "AC", "B", "AC", "ABC", "AB", "B"),
    "blue": ("B", "AC", "B", "A", "B", "BC", "C", "AC", "A"),
    "black": ("ABC", "AC", "B", "AC", "B", "BC", "AB", "C", "C"),
}


def solve_wire_sequence(color: str, occurrence: int, target: str) -> Solution:
    """その色の何本目か、どこへ繋がっているかで切るかを決める。"""
    key = (color or "").lower()
    letters = _SEQUENCE.get(key)
    if letters is None:
        return _need("線の色（赤・青・黒のどれか）")
    if not 1 <= occurrence <= 9:
        return _need(f"{_COLOR_JA.get(key, key)}の線がこれで何本目か")
    letter = (target or "").upper()
    if letter not in {"A", "B", "C"}:
        return _need("繋がっている先（A・B・Cのどれか）")
    allowed = letters[occurrence - 1]
    ok = letter in allowed
    return Solution(
        "切って" if ok else "切らないで",
        f"{_COLOR_JA.get(key, key)}の{occurrence}本目で切ってよいのは{'・'.join(allowed)}",
    )


# ---------------------------------------------------------------------------
# パスワード
# ---------------------------------------------------------------------------

#: 出題されうる単語。列ごとの文字表を覚え違えるより、単語を絞る方が確実で、
#: 実際の解き方（見えた文字で候補を削る）とも一致する。
PASSWORD_WORDS = (
    "about", "after", "again", "below", "could", "every", "first", "found",
    "great", "house", "large", "learn", "never", "other", "place", "plant",
    "point", "right", "small", "sound", "spell", "still", "study", "their",
    "there", "these", "thing", "think", "three", "water", "where", "which",
    "world", "would", "write",
)


def solve_password(columns: list[list[str]]) -> Solution:
    """各列で見えている文字から単語を絞る。列は左から、途中まででよい。"""
    if not columns or not any(columns):
        return _need("1列目に出ている文字（全部でなくてよい）")
    candidates = []
    for word in PASSWORD_WORDS:
        ok = True
        for index, seen in enumerate(columns):
            if index >= len(word) or not seen:
                continue
            if word[index] not in {char.lower() for char in seen}:
                ok = False
                break
        if ok:
            candidates.append(word)
    if not candidates:
        return Solution(
            because="その組み合わせに当たる単語が一覧に無い",
            needs="もう一度、列ごとの文字を確認",
        )
    if len(candidates) > 1:
        return Solution(
            because=f"候補が{len(candidates)}件（{'、'.join(candidates[:6])}）",
            needs=f"{len(columns) + 1}列目の文字",
        )
    return Solution(f"「{candidates[0]}」で送信して", "他の候補が消えた")


# ---------------------------------------------------------------------------
# 表が要る3モジュール
#
# 解法は単純だが表が大きい。記憶で書き起こすと一箇所の誤りが静かに残るので、
# 表は `manual/tables/*.json` から読む（`tables.py`）。未取り込みなら答えない。
# ---------------------------------------------------------------------------


def _not_loaded(name: str, file: str) -> Solution:
    return Solution(
        unknown=True,
        because=(
            f"{name}の表をまだ取り込んでいない"
            f"（{file} が空。docs/KTANE_TABLES.md の手順で入れられる）"
        ),
    )


def solve_keypad(symbols: list[str], columns: list[list[str]]) -> Solution:
    """4つの記号を全部含む列を探し、その列の並び順に押す。"""
    if not columns:
        # 表が無くても口で教えられる。突き放さずに手順を言う。
        return Solution(
            because="キーパッドの表がまだ空",
            needs=(
                "マニュアルを見ながら『1列目は 丸に線、稲妻、…』のように"
                "6列ぶん読み上げてくれれば覚える"
            ),
        )
    seen = [str(symbol).strip() for symbol in symbols if str(symbol).strip()]
    if not seen:
        return _need("4つの記号の呼び名")
    matches = [column for column in columns if all(symbol in column for symbol in seen)]
    if not matches:
        return Solution(
            because="その4つを全部含む列が無い。どれかの読み取りが違うかもしれない",
            needs="もう一度、記号の特徴",
        )
    if len(matches) > 1:
        return Solution(
            because=f"候補の列が{len(matches)}本ある",
            needs="残りの記号",
        )
    column = matches[0]
    order = sorted(seen, key=column.index)
    return Solution(
        "、".join(order) + " の順に押して",
        "その4つが全部そろう列の並び順",
    )


def solve_whos_on_first(
    display: str, buttons: list[str],
    positions: dict[str, int], order: dict[str, list[str]],
) -> Solution:
    """画面の語で読む位置を決め、そのボタンの語で押す語を決める。

    2段構えなので、1段目（読む位置）だけでも先に答えられる。
    ボタンの一覧がまだ聞けていない時は、そこで止めて位置だけ伝える。
    """
    if not positions or not order:
        return _not_loaded("心理戦", "manual/tables/whos_on_first.json")
    key = _normalise_word(display)
    if not key and display != "":
        return _need("画面に出ている語")
    if key not in positions:
        return Solution(
            because=f"画面の語「{display}」が表に無い",
            needs="画面の語をもう一度",
        )
    place = positions[key]
    if not buttons:
        return Solution(
            f"左上から数えて{place}番目のボタンを見て",
            f"画面が「{display}」のとき",
            follow_up="そこに書いてある語と、6つのボタンの語を教えて",
        )
    label = _normalise_word(buttons[place - 1]) if len(buttons) >= place else ""
    if not label:
        return _need(f"{place}番目のボタンに書いてある語")
    if label not in order:
        return Solution(
            because=f"ボタンの語「{buttons[place - 1]}」が表に無い",
            needs="その語をもう一度",
        )
    available = {_normalise_word(word): word for word in buttons}
    for candidate in order[label]:
        if candidate in available:
            return Solution(
                f"「{available[candidate]}」を押して",
                f"{place}番目が「{buttons[place - 1]}」のときの優先順",
            )
    return Solution(
        because="押せる語が盤面に見つからない",
        needs="6つのボタンの語をもう一度",
    )


def _normalise_word(word: str) -> str:
    return "".join(char for char in str(word or "").upper() if char.isalnum())


def solve_maze(
    circles: list[tuple[int, int]],
    start: tuple[int, int] | None,
    goal: tuple[int, int] | None,
    mazes: list[dict],
) -> Solution:
    """丸の位置で迷路を選び、最短経路を方角の列で返す。

    座標は (列, 行) の1始まり。迷路は6×6。
    """
    if not mazes:
        return _not_loaded("迷路", "manual/tables/maze.json")
    if len(circles) < 2:
        return _need("丸2つの位置（左から何番目、上から何番目）")
    if start is None:
        return _need("白い四角（今いる場所）の位置")
    if goal is None:
        return _need("赤い三角（出口）の位置")
    wanted = {tuple(circle) for circle in circles}
    chosen = None
    for maze in mazes:
        marks = {tuple(mark) for mark in maze.get("circles", [])}
        if marks == wanted:
            chosen = maze
            break
    if chosen is None:
        return Solution(
            because="その丸の配置に合う迷路が表に無い",
            needs="丸2つの位置をもう一度",
        )
    passages = {
        (tuple(edge[0]), tuple(edge[1])) for edge in chosen["passages"]
    }
    passages |= {(b, a) for a, b in passages}
    path = _shortest_path(tuple(start), tuple(goal), passages)
    if path is None:
        return Solution(
            because="その位置から出口へ繋がる道が表の中に無い",
            needs="今いる場所と出口の位置をもう一度",
        )
    if not path:
        return Solution("もう出口にいるよ", "今いる場所と出口が同じ")
    return Solution("、".join(path) + " の順に動かして", f"{chosen.get('name', '該当の迷路')}の最短経路")


_DIRECTIONS = (((0, -1), "上"), ((0, 1), "下"), ((-1, 0), "左"), ((1, 0), "右"))


def _shortest_path(start, goal, passages) -> list[str] | None:
    """幅優先探索。壁ではなく「通れる辺」で持っているので単純に辿れる。"""
    from collections import deque

    if start == goal:
        return []
    queue = deque([(start, [])])
    seen = {start}
    while queue:
        node, moves = queue.popleft()
        for (dx, dy), name in _DIRECTIONS:
            nxt = (node[0] + dx, node[1] + dy)
            if not (1 <= nxt[0] <= 6 and 1 <= nxt[1] <= 6):
                continue
            if nxt in seen or (node, nxt) not in passages:
                continue
            if nxt == goal:
                return [*moves, name]
            seen.add(nxt)
            queue.append((nxt, [*moves, name]))
    return None
