"""Deterministic response-shape guidance for less mechanical voice dialogue.

This is not hidden chain-of-thought.  It converts observable conversational
signals into compact instructions that the response model can follow quickly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ResponseDirection:
    intent: str
    moves: tuple[str, ...]
    allow_follow_up: bool

    def prompt(self) -> str:
        lines = [
            "【今回の応答設計】",
            f"意図={self.intent}。",
            "本文では次を自然な会話として行う: " + " / ".join(self.moves),
        ]
        if self.allow_follow_up:
            lines.append(
                "最後に質問を付けるなら、発話中の具体的な内容に答えやすくつながる質問を一つだけにする。"
            )
        else:
            lines.append("質問を足して会話を無理に引き延ばさない。")
        lines.append(
            "定型の『なるほど』『そうなんだ』だけで終わらせず、ユーザーの発話中の具体的な一点に触れる。"
        )
        lines.append(
            "Agreement Gate: 前提を未検証で肯定しない。未知語や不明な主張には"
            "正式名称や事実を作らず、確認する。違う内容は理由を添えて訂正する。"
        )
        lines.append(
            "Independent Social Stance: 温かさと同意を分け、好意・称賛・親密さを"
            "実際の関係状態より盛らない。関係や感情にも根拠の範囲で率直に答え、"
            "急な『大好き』などへ飛躍しない。"
        )
        return "\n".join(lines)


class ResponseDirector:
    """Choose a conversational response shape from the current utterance."""

    _QUESTION = re.compile(r"[?？]|(?:教えて|どう|なぜ|なんで|いつ|どこ|誰|どれ|どうやって)")
    _CORRECTION = re.compile(r"(?:違う|それじゃない|修正|訂正|いや、|いや )")
    _SUPPORT = re.compile(r"(?:つら|疲れ|しんど|困っ|不安|悲し|むかつ|最悪|失敗|できなかった)")
    _GOOD_NEWS = re.compile(r"(?:できた|成功|受か|勝っ|嬉し|楽しかった|最高|やった|達成)")
    _PREFERENCE = re.compile(r"(?:好き|苦手|ハマっ|興味|欲し|やりたい|作って|開発して|始めた)")
    _STORY = re.compile(r"(?:今日|さっき|この前|昨日|行っ|見た|食べた|会った|遊んだ|起きた)")
    _RELATION_CHECK = re.compile(
        r"(?:私|僕|俺|うち|自分|きみ).{0,14}"
        r"(?:好き|興味|どう思|どう感じ|仲良|関係|距離感|知りたい)"
    )

    def decide(self, text: str, *, allow_follow_up: bool, momentum: float) -> ResponseDirection:
        normalized = (text or "").strip().lower()
        if self._CORRECTION.search(normalized):
            return ResponseDirection(
                "訂正・軌道修正",
                ("まず誤解を短く認める", "何をどう理解し直したかを具体的に示す", "修正後の内容に答える"),
                False,
            )
        if self._SUPPORT.search(normalized):
            return ResponseDirection(
                "気持ちの共有と支援",
                ("状況を決めつけず気持ちに寄り添う", "負担を減らす小さく具体的な選択肢を一つ示す"),
                allow_follow_up and momentum >= 0.55,
            )
        if self._RELATION_CHECK.search(normalized):
            return ResponseDirection(
                "関係性・感情の確認",
                (
                    "期待されていそうな好意を演じず、現在の関係と実際のやり取りに合う範囲で直接答える",
                    "関心・信頼・親しさを混同せず、自分の見方を一つ具体的に示す",
                ),
                False,
            )
        if self._QUESTION.search(normalized):
            return ResponseDirection(
                "質問への回答",
                ("知りたいことに先に直接答える", "必要な理由・例外・自分の短い見解を一つ補う"),
                False,
            )
        if self._GOOD_NEWS.search(normalized):
            return ResponseDirection(
                "喜び・達成の共有",
                ("何が良かったのかを具体的に喜ぶ", "その出来事が持つ意味や次に楽しめそうな点へ自然につなぐ"),
                allow_follow_up,
            )
        if self._PREFERENCE.search(normalized):
            return ResponseDirection(
                "関心・希望の受け取り",
                ("好みや目標の中身を一段具体的に受け取る", "関連する自分の見解か、役立つ次の一歩を添える"),
                allow_follow_up,
            )
        if self._STORY.search(normalized):
            return ResponseDirection(
                "出来事への反応",
                ("出来事の中で印象的な一点に反応する", "そこから自然に連想できる感想や関連話題を一つ添える"),
                allow_follow_up,
            )
        return ResponseDirection(
            "雑談・意見の受け取り",
            ("発話の具体的な一点に自分の見解か感情を返す", "会話の流れを少しだけ前へ進める関連の視点を添える"),
            allow_follow_up and momentum >= 0.65,
        )
