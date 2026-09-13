"""キーパッドの記号を、声で言える名前で扱う。

これまで「表が無い」とだけ返していたが、足りなかったのは表だけではない。
**記号を言葉で受け取る仕組みが無かった。**

キーパッドの記号には公式の読み方が無い。○や△と違って名前を持たないので、
プレイヤーは「しっぽの生えた6」「逆さのC」のように、その場で呼び名を作る。
だから

* 呼び名は**利用者が決める**。こちらが正解の名前を持たない
* 同じ記号を毎回まったく同じ言い方で言うとは限らない
* 「6にしっぽ」「しっぽ付きの6」「6みたいなやつ」は同じものを指す

この3つを踏まえた照合をする。ゆるく当てすぎると別の記号を掴むので、
**曖昧なら聞き返す**（第2条: 分からないものを分かったふりで確定させない）。

表そのものは `manual/tables/keypad.json` から読む。ここは名前の解決だけ。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: 呼び名を比べる時に落とす言葉。意味を持たない飾り。
_NOISE = re.compile(
    r"(?:みたいな(?:やつ|の)?|っぽい(?:の|やつ)?|のような(?:やつ|もの)?|"
    r"に見える|感じの?|記号|シンボル|マーク|文字|やつ|もの|の)"
)
_SPACE = re.compile(r"[\s　・,、]+")


def normalize(name: str) -> str:
    """呼び名を照合できる形にする。**中身の語順は保つ。**

    「しっぽ付きの6」→「しっぽ付き6」。全角半角と飾り語だけを吸収する。
    語そのものを削ると別の記号と衝突するので、そこまではやらない。
    """
    value = unicodedata.normalize("NFKC", str(name or "")).strip().lower()
    value = _NOISE.sub("", value)
    return _SPACE.sub("", value)


def _tokens(name: str) -> set[str]:
    """文字の並び。言い回しが近い時の保険。"""
    value = normalize(name)
    # 日本語は分かち書きされないので、2文字の並びで見る。
    return {value[i:i + 2] for i in range(max(1, len(value) - 1))} | {value}


#: **形の特徴**。言い方が違っても、同じ形なら同じ語が立つ。
#:
#: 呼び名の一致を求めると、人も場面も変われば必ず外れる。「丸に線」と
#: 「円の中に横棒」は文字列としては別物だが、**形としては同じ**。
#: そこを見る。
#:
#: ここに無い言葉は無視される（勝手に近い形へ寄せない）。足りなければ
#: 行を増やす——それだけで新しい言い方に対応できる。
FEATURES: dict[str, tuple[str, ...]] = {
    "丸": ("丸", "まる", "マル", "円", "えん", "オー", "o", "0", "球", "輪"),
    "線": ("線", "せん", "棒", "ぼう", "バー", "横棒", "縦棒", "ライン", "スラッシュ"),
    "三本": ("三本", "3本", "さんぼん", "3つの線", "三つ", "みっつ"),
    "二本": ("二本", "2本", "にほん", "двe", "ふたつ", "二つ"),
    "点": ("点", "てん", "ドット", "・", "粒"),
    "星": ("星", "ほし", "スター", "☆", "★"),
    "稲妻": ("稲妻", "いなずま", "雷", "かみなり", "ジグザグ", "ぎざぎざ", "z"),
    "しっぽ": ("しっぽ", "尻尾", "尾", "ひげ", "とんがり", "ツノ", "角", "つの"),
    "逆": ("逆", "さかさ", "反対", "裏返", "ひっくり", "鏡", "ミラー"),
    "曲": ("曲", "カーブ", "うずまき", "渦", "らせん", "くるっ", "丸まっ", "巻"),
    "四角": ("四角", "しかく", "箱", "はこ", "□", "長方形"),
    "三角": ("三角", "さんかく", "▲", "△", "とがっ"),
    "十字": ("十字", "クロス", "×", "ばつ", "バツ", "プラス", "＋"),
    "はてな": ("はてな", "疑問", "クエスチョン", "？", "?"),
    "六": ("6", "六", "ろく", "シックス"),
    "四": ("4", "四", "よん", "フォー"),
    "文字": ("アルファベット", "英字", "文字", "字"),
    "c": ("c", "シー", "ｃ"),
    "a": ("a", "エー", "ａ"),
    "e": ("e", "イー", "ｅ"),
    "h": ("h", "エイチ", "ｈ"),
    "k": ("k", "ケー", "ｋ"),
    "n": ("n", "エヌ", "ｎ"),
    "psi": ("プサイ", "サイ", "psi", "熊手", "フォーク", "三又", "三叉"),
    "omega": ("オメガ", "omega", "ω", "馬蹄", "ヘッドホン"),
    "水滴": ("水滴", "しずく", "雫", "涙", "ドロップ", "たまご", "卵"),
    "煙": ("煙", "けむり", "もくもく", "雲", "くも"),
    "顔": ("顔", "スマイル", "笑", "にこ"),
}
_FEATURE_INDEX: tuple[tuple[str, str], ...] = tuple(
    (word.lower(), key) for key, words in FEATURES.items() for word in words
)


def features(text: str) -> set[str]:
    """言われた説明から、形の特徴を拾う。**知らない言葉は捨てる。**"""
    value = normalize(text)
    return {key for word, key in _FEATURE_INDEX if word and word in value}


@dataclass(frozen=True, slots=True)
class SymbolMatch:
    name: str
    score: float
    ambiguous: bool = False


def _similarity(spoken: str, candidate: str, aliases: tuple[str, ...] = ()) -> float:
    """言われた説明と、登録済みの記号の近さ。

    **形の特徴を先に見る。** 文字列の一致は保険。「円の中に横棒」と
    「丸に線」は文字としては別物だが、特徴は {丸, 線} で同じ。
    決まった呼び名を毎回言わせる作りだと、人が変われば必ず外れる。
    """
    value = normalize(spoken)
    best = .0
    for target in (candidate, *aliases):
        normalized = normalize(target)
        if not normalized:
            continue
        if value == normalized:
            return 1.0
        spoken_features = features(spoken)
        target_features = features(target)
        if spoken_features and target_features:
            overlap = spoken_features & target_features
            union = spoken_features | target_features
            # 言われた特徴が**全部**含まれているなら、説明が短くても当たり。
            score = len(overlap) / len(union)
            if overlap == spoken_features:
                score = max(score, .8)
            best = max(best, score)
        letters = _tokens(value) & _tokens(target)
        union_letters = _tokens(value) | _tokens(target)
        if union_letters:
            best = max(best, len(letters) / len(union_letters))
        if len(value) >= 2 and (value in normalized or normalized in value):
            best = max(best, .75)
    return best


def match_symbol(
    spoken: str, known: list[str] | tuple[str, ...], *,
    accept: float = .5, margin: float = .12,
    aliases: dict[str, tuple[str, ...]] | None = None,
) -> SymbolMatch | None:
    """言われた説明を、登録済みの記号へ寄せる。

    `margin` は1位と2位の差。**近すぎるものが2つあれば曖昧**として返し、
    呼び出し側が聞き返せるようにする。適当に1位を採ると、隣の記号を
    押させて即ミスになる。
    """
    if not normalize(spoken) or not known:
        return None
    table = aliases or {}
    scored = [
        (_similarity(spoken, candidate, table.get(candidate, ())), candidate)
        for candidate in known
    ]
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, best_name = scored[0]
    if best_score >= 1.0:
        return SymbolMatch(best_name, 1.0)
    if best_score < accept:
        return None
    if len(scored) > 1 and best_score - scored[1][0] < margin:
        return SymbolMatch(best_name, best_score, ambiguous=True)
    return SymbolMatch(best_name, best_score)


class SymbolVocabulary:
    """**言われ方を覚えていく。** 一度通じた言い方は次から通る。

    人によって、その日によって言い方は変わる。毎回同じ呼び名を要求するのは
    こちらの都合でしかない。聞き返して分かったら、その言い方を紐づけておく。
    """

    def __init__(self, aliases: dict[str, list[str]] | None = None) -> None:
        self._aliases: dict[str, list[str]] = {
            name: list(values) for name, values in (aliases or {}).items()
        }

    def learn(self, symbol: str, spoken: str) -> bool:
        """「さっきのは丸に線のことだよ」で覚える。"""
        value = str(spoken or "").strip()
        if not symbol or not value or normalize(value) == normalize(symbol):
            return False
        # 「稲妻」と「いなずま」のように、**特徴が同じなら覚える必要が無い**。
        # 既に照合できるものを溜めても、表が膨らむだけ。
        spoken_features, symbol_features = features(value), features(symbol)
        if spoken_features and spoken_features == symbol_features:
            return False
        known = self._aliases.setdefault(symbol, [])
        if value in known:
            return False
        known.append(value)
        del known[:-8]          # 際限なく増やさない
        return True

    def for_symbol(self, symbol: str) -> tuple[str, ...]:
        return tuple(self._aliases.get(symbol, ()))

    def table(self) -> dict[str, tuple[str, ...]]:
        return {name: tuple(values) for name, values in self._aliases.items()}

    def snapshot(self) -> dict[str, list[str]]:
        return {name: list(values) for name, values in self._aliases.items() if values}


def resolve_symbols(
    spoken: list[str], known: list[str] | tuple[str, ...],
    vocabulary: "SymbolVocabulary | None" = None,
) -> tuple[list[str], list[str], list[str]]:
    """(確定した記号, 分からなかった呼び名, 紛らわしかった呼び名)。

    3つに分けるのは、返す言葉が変わるため。知らない呼び名なら
    「どんな形？」、紛らわしいなら「どっち？」と聞くのが自然で、
    どちらも「分からない」で片づけるとやり取りが進まない。
    """
    resolved: list[str] = []
    unknown: list[str] = []
    ambiguous: list[str] = []
    table = vocabulary.table() if vocabulary is not None else None
    for name in spoken:
        match = match_symbol(name, known, aliases=table)
        if match is None:
            unknown.append(name)
        elif match.ambiguous:
            ambiguous.append(name)
        else:
            resolved.append(match.name)
    return resolved, unknown, ambiguous


def known_symbols(columns: list[list[str]]) -> list[str]:
    """表に出てくる記号名の一覧。重複は畳む。"""
    seen: list[str] = []
    for column in columns:
        for symbol in column:
            if symbol not in seen:
                seen.append(symbol)
    return seen
