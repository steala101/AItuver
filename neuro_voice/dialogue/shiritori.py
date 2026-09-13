"""しりとり進行の追跡 (プレイするのはLLM本体、ここはルール状況の補助のみ)。

以前は専用エンジンがLLMを迂回して定型文を返していたが、それだと人格や
過去の記憶が遊びに一切反映されない。現在この module は「審判メモ」として
状態 (直前の言葉・次の文字・使用済みの言葉) を追跡し、system コンテキスト
としてLLMへ渡すだけ。応答そのものは常にLLM自身が生成する。
"""
from __future__ import annotations

import re

_START_RE = re.compile(r"(?:しりとり|尻取り).*(?:しよう|やろう|しよ|開始|始め)|(?:しりとり|尻取り)$")
_STOP_RE = re.compile(r"(?:しりとり|尻取り).*(?:やめ|終わ)|(?:もう)?(?:やめよう|終わりにしよう)")
_WORD_RE = re.compile(r"[一-龥々〆ヶぁ-んァ-ヶー]{1,24}")
_KANA_WORD_RE = re.compile(r"[ぁ-んァ-ヶー]{2,16}")
_QUOTED_RE = re.compile(r"[「『]([^」』]{1,16})[」』]")
_SMALL = str.maketrans("ぁぃぅぇぉゃゅょゎっ", "あいうえおやゆよわつ")


def _katakana_to_hiragana(text: str) -> str:
    return "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in text)


def _kana(text: str) -> str:
    return _katakana_to_hiragana(text).translate(_SMALL)


_LONG_VOWEL = {
    **{ch: "あ" for ch in "かがさざただなはばぱまやらわ"},
    **{ch: "い" for ch in "きぎしじちぢにひびぴみり"},
    **{ch: "う" for ch in "くぐすずつづぬふぶぷむゆる"},
    **{ch: "え" for ch in "けげせぜてでねへべぺめれ"},
    **{ch: "お" for ch in "こごそぞとどのほぼぽもよろを"},
}

_ALIASES = {
    "林檎": "りんご", "蜜柑": "みかん", "達磨": "だるま",
    "苺": "いちご", "兎": "うさぎ", "狐": "きつね", "車": "くるま",
    "桜": "さくら", "西瓜": "すいか", "空": "そら", "象": "ぞう",
    "狸": "たぬき", "猫": "ねこ", "蛇": "へび", "葡萄": "ぶどう",
}

# 小型ローカルLLM向けのヒント用語彙。「す」で始まる言葉の実例を2〜3個
# プロンプトに添えるとルール遵守率が大きく上がる (選ぶかは LLM の自由)。
_HINT_WORDS = (
    "あさがお", "いちご", "うさぎ", "えのぐ", "おにぎり",
    "かめ", "きつね", "くるま", "けむし", "こあら",
    "がっこう", "ぎんこう", "ぐみ", "げた", "ごりら",
    "さくら", "しろくま", "すいか", "せみ", "そら",
    "ざりがに", "じてんしゃ", "ずこう", "ぜっけん", "ぞう",
    "たぬき", "ちくわ", "つみき", "てぶくろ", "とまと",
    "だるま", "でんわ", "どらやき",
    "なす", "にわとり", "ぬりえ", "ねこ", "のりまき",
    "はさみ", "ひまわり", "ふくろう", "へび", "ほたる",
    "ばなな", "びわ", "ぶどう", "べんち", "ぼうし",
    "ぱんだ", "ぴあの", "ぷりん", "ぺんぎん", "ぽけっと",
    "まくら", "みずうみ", "むぎちゃ", "めがね", "もも",
    "やさい", "ゆき", "ようふく",
    "らくだ", "りす", "るびー", "れもん", "ろうそく", "わに",
)

# しりとりの言葉ではなく「ゲームについての発言」を示す語
_META_MARKERS = (
    "違う", "ちがう", "じゃない", "だめ", "ダメ", "ルール", "間違",
    "まちがって", "待って", "まって", "おかしい", "できない", "出来ない",
    "さっき", "なんで", "どうして", "もう一回", "もういっかい", "ずるい",
)


def first_kana(reading: str) -> str:
    value = _kana(reading)
    return value[0] if value else ""


def last_kana(reading: str) -> str:
    value = _kana(reading).rstrip("。！？!?、, ")
    if not value:
        return ""
    if value[-1] == "ー" and len(value) >= 2:
        return _LONG_VOWEL.get(value[-2], value[-2])
    return value[-1]


class ShiritoriCoach:
    """しりとりの審判メモ。LLMの応答は生成せず、状況コンテキストだけ返す。"""

    def __init__(self):
        self.active = False
        self.expected = ""          # 次の言葉が始まるべきかな ("" = 未確定)
        self.used: list[str] = []   # これまでに出た言葉 (ひらがな読み、出た順)

    def reset(self) -> None:
        self.active = False
        self.expected = ""
        self.used = []

    # ---------- ユーザー発話の観測 ----------

    def observe_user(self, text: str) -> str | None:
        """ユーザー発話を観測し、LLMへ渡す system コンテキストを返す。

        しりとりに関係なければ None。返した文字列は応答生成の system に
        差し込むだけで、応答文は必ずLLMが自分で作る。
        """
        source = str(text or "").strip()
        if not self.active:
            if not _START_RE.search(source):
                return None
            self.active = True
            self.expected = ""
            self.used = []
            return (
                "【しりとり開始】いまユーザーとしりとりを始めることになった。"
                "応答はいつも通り自分の言葉と性格で話す。"
                "自分から最初の言葉を出すか、ユーザーに先攻を譲るかは流れで決めてよい。"
                "ルール: 前の言葉の最後の文字から始まる言葉を出す。"
                "「ん」で終わる言葉を出したら負け。同じ言葉は再利用しない。"
                "自分の言葉には聞き取りやすいよう、ひらがなの読みを添える。"
                "過去の会話や思い出にちなんだ言葉を選ぶと会話が弾む。"
            )

        if _STOP_RE.search(source):
            self.reset()
            return (
                "【しりとり終了】ユーザーがしりとりを終えたがっている。"
                "結果や印象を一言振り返って、自然に普段の会話へ戻る。"
            )

        notes: list[str] = []
        if self._is_meta_utterance(source):
            notes.append(
                "ユーザーの今の発言は言葉の提出ではなく、指摘・雑談のようだ。"
                "内容に普通に応じる。もし自分のルール違反を指摘されたら素直に認めて、"
                "下記の「次に出す言葉」から仕切り直す。"
            )
            return self._context(notes)
        surface, reading = self._extract_word(source)
        if reading:
            if self.expected and first_kana(reading) != self.expected:
                notes.append(
                    f"ユーザーの言葉『{surface}』は「{first_kana(reading)}」で始まっているように聞こえる。"
                    f"ルール上は「{self.expected}」始まりのはず。本当に違っていたら軽く指摘する"
                    "(聞き間違いの可能性もあるので決めつけない)。"
                )
            if reading in self.used:
                notes.append(f"『{surface}』はすでに出た言葉のはず。軽く指摘して別の言葉を促してよい。")
            else:
                self.used.append(reading)
            if last_kana(reading) == "ん":
                self.reset()
                return (
                    f"【しりとり決着】ユーザーの言葉『{surface}』は「ん」で終わっている。"
                    "ルール上ユーザーの負け。勝ち誇りすぎず楽しく決着を伝えて、"
                    "続けるか別の遊びにするかは会話の流れに任せる。"
                )
            self.expected = last_kana(reading)
        else:
            notes.append(
                "ユーザーの言葉の読みをこちらで確定できなかった。"
                "漢字などは自分で読みを判断してしりとりを続ける。"
            )
        return self._context(notes)

    # ---------- AI発話の観測 ----------

    def observe_assistant(self, text: str) -> None:
        """AI自身の応答から出した言葉を推定して状態を進める。"""
        if not self.active:
            return
        reading = self._extract_ai_word(str(text or ""))
        if not reading:
            return
        if reading not in self.used:
            self.used.append(reading)
        if last_kana(reading) == "ん":
            # 「ん」で終わる言葉を出したなら、LLM側が負けを認めて締めている
            # はず。状態だけ静かに畳む。
            self.reset()
            return
        self.expected = last_kana(reading)

    # ---------- 内部 ----------

    def _context(self, notes: list[str]) -> str:
        used_tail = "、".join(self.used[-12:]) if self.used else "まだなし"
        lines = [
            "【しりとり進行中・このルールを最優先で守る】いまユーザーとしりとりで遊んでいる。",
        ]
        if self.expected:
            line = f"- 次にあなたが出す言葉は、必ず「{self.expected}」で始まる言葉。"
            hints = self._hint_words(self.expected)
            if hints:
                line += f"例: {'、'.join(hints)}。この例以外でも「{self.expected}」で始まれば何でもよい。"
            lines.append(line)
        else:
            lines.append("- まだ言葉の連鎖が確定していない。好きな言葉から始めてよい。")
        lines.append("- 「ん」で終わる言葉は絶対に出さない (出したら自分の負け)。")
        lines.append(f"- 使用済みの言葉 (もう出せない): {used_tail}")
        lines.append(
            "- 出す言葉は一つだけ。「◯◯!次は『◇』だよ」のように、"
            "自分の言葉とその最後の文字をはっきり言う。"
        )
        lines.append("- ルールの説明を長々と話さない。短い一言+自分の言葉でテンポよく返す。")
        lines.append("- 過去の会話や記憶にちなんだ言葉を選ぶのは大歓迎。")
        for note in notes:
            lines.append(f"- {note}")
        return "\n".join(lines)

    def _hint_words(self, required: str) -> list[str]:
        """expected文字で始まる未使用の実例を最大3つ返す (LLMへのヒント)。"""
        out = []
        for word in _HINT_WORDS:
            if first_kana(word) == required and word not in self.used and last_kana(word) != "ん":
                out.append(word)
                if len(out) >= 3:
                    break
        return out

    @staticmethod
    def _is_meta_utterance(text: str) -> bool:
        """言葉の提出ではなく、ゲームについての指摘・雑談かを判定する。"""
        compact = re.sub(r"[。！？!?、,\s]", "", text)
        if "しりとり" in compact or "尻取り" in compact:
            return True
        if any(marker in compact for marker in _META_MARKERS):
            return True
        # 一語の提出は通常短い。長い文は会話として扱う (誤って連鎖を汚さない)
        return len(compact) > 12

    @staticmethod
    def _extract_word(text: str) -> tuple[str, str]:
        compact = re.sub(r"^(?:じゃあ|それじゃ|次は|はい|うん)[、,\s]*", "", text.strip())
        compact = re.sub(
            r"(?:だよ|です|でお願いします|でおねがいします|にする|かな)[。！？!?、,\s]*$",
            "", compact,
        )
        matches = _WORD_RE.findall(compact)
        if not matches:
            return "", ""
        surface = matches[0].strip("。！？!?、, ")
        if surface in _ALIASES:
            return surface, _ALIASES[surface]
        reading = _kana(surface)
        if re.search(r"[一-龥々〆ヶ]", reading):
            return surface, ""
        return surface, reading

    def _extract_ai_word(self, text: str) -> str:
        """AI応答から「出した言葉」を推定する。曖昧なら空を返す (審判メモなので許容)。"""
        quoted = [_kana(m) for m in _QUOTED_RE.findall(text) if _KANA_WORD_RE.fullmatch(m)]
        candidates = quoted + [_kana(m) for m in _KANA_WORD_RE.findall(text)]
        if not candidates:
            return ""
        if self.expected:
            for cand in candidates:
                if first_kana(cand) == self.expected:
                    return cand
            return ""
        return candidates[0]
