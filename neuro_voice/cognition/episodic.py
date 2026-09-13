"""経験を「覚えるかどうか」から決める層。

**新しい記憶基盤は作らない。** 保存先は既存の `mind/store.py` の `memories`
テーブルのまま。ここが足すのは、これまで無かった2つだけ。

1. **出典** — ユーザーが言ったのか、こちらが推測したのか。この区別が無いと、
   推測がいつの間にか「本人が言ったこと」として使われる
2. **保存判断** — これまでは内省LLMが「覚えるべき」と言ったものを、
   重要度だけ見て入れていた。重複も訂正も見ていなかった

保存判断を1箇所へ集約しているのは、散らばると「なぜ覚えたか／覚えなかったか」
が誰にも説明できなくなるため（第20条）。

**過去の記憶を無条件に物理削除しない。** 訂正は上書きではなく、
`superseded_by` で繋いだ別のレコードとして残す。何を信じ直したのかを
後から読めないと、間違った訂正に気づけない。
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from neuro_voice.cognition.types import InformationType

# ---------------------------------------------------------------------------
# 状態
# ---------------------------------------------------------------------------


class MemoryStatus(StrEnum):
    """忘却は**即座の物理削除ではない**。

    検索順位を下げる／棚上げする／訂正済みとして残す、を区別する。
    区別が無いと「消したのか、出てこないだけなのか」が分からない。
    """

    ACTIVE = "active"
    LOW_PRIORITY = "low_priority"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    CONTRADICTED = "contradicted"
    DELETED = "deleted"


#: 検索に出てよい状態。訂正されたものは既定では出さない。
RETRIEVABLE = frozenset({MemoryStatus.ACTIVE, MemoryStatus.LOW_PRIORITY})


#: 長期記憶へ入れない機微度。`privacy.Sensitivity` の値に合わせてある。
#:
#: 既存の `PrivacyManager` を再利用する——判定器を作り直すと、片方だけ
#: 更新されて緩い方が通ってしまう。
PRIVACY_NEVER_STORE = frozenset({"restricted", "secret", "high"})


class WriteDecision(StrEnum):
    """保存判断の結果。**SKIP と HOLD は違う。**

    SKIP は「要らない」。HOLD は「まだ確信が無いので仮説として置く」。
    低い確信の推論を恒久記憶にしてしまうと、後から本人が否定しても
    同じ強さで競合してしまう。
    """

    STORE = "store"
    SKIP = "skip"
    HOLD = "hold"
    REINFORCE = "reinforce"
    UPDATE = "update"
    CONTRADICT = "contradict"
    SUPERSEDE = "supersede"


# ---------------------------------------------------------------------------
# 型
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EpisodicMemory:
    """1つの経験。**既存レコードの上に載るメタデータ**として設計してある。

    `memory_id` は SQLite の rowid をそのまま使う（新しいID体系を作らない）。
    """

    memory_id: int = 0
    summary: str = ""
    information_type: InformationType | str = InformationType.OBSERVATION
    event_type: str = "conversation"
    occurred_at: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    session_id: str = ""
    participant_ids: tuple[str, ...] = ()
    topic_ids: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    confidence: float = .5
    importance: float = .5
    emotional_salience: float = .0
    relationship_salience: float = .0
    goal_relevance: float = .0
    outcome: str = ""
    retrieval_keys: tuple[str, ...] = ()
    status: MemoryStatus | str = MemoryStatus.ACTIVE
    superseded_by: int = 0
    support_count: int = 1
    contradiction_count: int = 0
    access_count: int = 0
    last_accessed_at: float = .0

    @property
    def is_statement(self) -> bool:
        """本人が言ったことか。推測より常に強い。"""
        return str(self.information_type) == str(InformationType.USER_STATEMENT)

    def planner_view(self, relevance: str = "") -> dict[str, Any]:
        """Conversation Planner へ渡す形。**短く、出典付きで。**

        生の会話履歴を大量に投入しない。渡すのは「何を」「どれくらい確かに」
        「何のために」の3つだけ。
        """
        return {
            "memory_id": self.memory_id,
            "type": str(self.information_type).upper(),
            "summary": self.summary[:90],
            "confidence": round(float(self.confidence), 2),
            "relevance": relevance,
        }

    def snapshot(self) -> dict[str, Any]:
        """トレース用。**本文は要約のみ、全文は入れない**（第12条）。"""
        return {
            "memory_id": self.memory_id,
            "type": str(self.information_type),
            "status": str(self.status),
            "confidence": round(float(self.confidence), 2),
            "importance": round(float(self.importance), 2),
            "chars": len(self.summary),
        }


@dataclass(slots=True)
class MemoryCandidate:
    """まだ保存していない、保存**候補**。

    候補づくりと保存判断を分けているのは、規則やLLMが「覚えたい」と言うのと、
    実際に覚えてよいのかが別の話だから。最終判断はコード側（第2条）。
    """

    proposed_summary: str
    information_type: InformationType | str = InformationType.OBSERVATION
    event_type: str = "conversation"
    source_event_ids: tuple[str, ...] = ()
    topic_ids: tuple[str, ...] = ()
    importance: float = .5
    novelty: float = .5
    emotional_salience: float = .0
    relationship_salience: float = .0
    goal_relevance: float = .0
    expected_future_utility: float = .5
    confidence: float = .5
    privacy_level: str = "normal"
    participant_ids: tuple[str, ...] = ()
    session_id: str = ""
    reasons: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)
    candidate_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def value(self) -> float:
        """保存する価値。**重要度だけでは決めない。**

        一度きりの強い感情も、地味だが後で効く情報も、どちらも残したい。
        """
        return round(
            .40 * float(self.importance)
            + .25 * float(self.expected_future_utility)
            + .15 * float(self.emotional_salience)
            + .10 * float(self.relationship_salience)
            + .10 * float(self.goal_relevance),
            4,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "type": str(self.information_type),
            "event": self.event_type,
            "value": self.value,
            "confidence": round(float(self.confidence), 2),
            "novelty": round(float(self.novelty), 2),
            "reasons": list(self.reasons),
            "chars": len(self.proposed_summary),
        }


@dataclass(frozen=True, slots=True)
class WriteVerdict:
    """保存判断の結果と、その理由。**理由が無い判断は残さない。**"""

    decision: WriteDecision
    reasons: tuple[str, ...] = ()
    target_memory_id: int = 0
    stored_status: MemoryStatus | str = MemoryStatus.ACTIVE
    confidence: float = .5

    @property
    def writes(self) -> bool:
        """新しいレコードを作るか。REINFORCE は既存を触るだけ。"""
        return self.decision in {
            WriteDecision.STORE, WriteDecision.HOLD,
            WriteDecision.CONTRADICT, WriteDecision.SUPERSEDE,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "reasons": list(self.reasons),
            "target": self.target_memory_id,
            "status": str(self.stored_status),
        }


# ---------------------------------------------------------------------------
# 候補づくり
#
# ここは**規則で拾える分だけ**。LLMの内省（`Mind._reflect`）が出す候補も
# 同じ `MemoryCandidate` にして、同じゲートを通す。入口を2つに分けると、
# 片方だけ重複チェックが漏れる。
# ---------------------------------------------------------------------------

#: 保存しない発話。挨拶・相づち・意味の薄い断片。
#:
#: **全ターンを長期保存しない**の実体はここ。落とす側を明示しておかないと、
#: 「なんとなく重要そう」で全部通ってしまう。
_GREETING = re.compile(
    r"^(?:おはよ|こんにちは|こんばんは|やあ|よお|ハロー|hi|hello|"
    r"ただいま|おかえり|おやすみ|またね|バイバイ|お疲れ)[ぅーっ！!。、\s]*$"
)
_BACKCHANNEL = re.compile(
    r"^(?:うん|ええ|はい|そう|そっか|なるほど|へえ|ふーん|おお|あー|まあ|"
    r"だね|ですね|オッケー|ok|わかった|了解)[ぇーっ！!。、？?\s]*$",
    re.IGNORECASE,
)

#: 明示された好み。**これは推測ではなく本人の申告**。
_PREFERENCE = re.compile(
    r"(?:が|は|って)\s*(?:好き|嫌い|苦手|得意)|"
    r"(?:して|やって|話して|聞いて)(?:ほしい|ください|くれ)|"
    r"(?:しないで|やめて|やめとい)|"
    r"(?:いつも|毎回|なるべく|できれば|基本的に).{0,20}(?:して|でいい|がいい|にして)|"
    r"(?:方がいい|ほうがいい|嫌だ|嫌い)"
)
#: 訂正。**過去に覚えたことを疑う合図**。
_CORRECTION = re.compile(
    r"(?:そうじゃなく|そうではなく|じゃなくて|ではなくて|違うって|"
    r"わけじゃない|わけでもない|とは限らない|そうとも限らない|"
    r"前に言った|さっき言った)(?![んの]?です?か)"
)
#: 約束・未完了。**忘れると信頼を失う側**。
_PROMISE = re.compile(
    r"(?:後で|あとで|次|今度|明日|later).{0,12}(?:確認|調べ|やっと|見と|やる|する)|"
    r"(?:覚えて|忘れないで|メモしと)"
)
#: 強い感情。
_STRONG_AFFECT = re.compile(
    r"(?:めっちゃ|すごく|すっごく|本当に|ほんとに|超)\s*(?:嬉し|楽し|悲し|悔し|腹立|怖|つら)|"
    r"(?:最高|最悪|感動|ショック|びっくり)"
)


def _keys(text: str, limit: int = 6) -> tuple[str, ...]:
    """検索用の手がかり語。**1文字クラスで拾わない。**

    日本語は分かち書きされないので、素朴に文字種で切ると「文まるごと=1語」に
    なる。ここは2文字以上の漢字/カタカナ塊と英数字だけを拾う。
    """
    value = str(text or "")
    found = re.findall(r"[一-龥々]{2,}|[ァ-ヴー]{2,}|[A-Za-z][A-Za-z0-9_]{1,}", value)
    seen: list[str] = []
    for item in found:
        if item not in seen:
            seen.append(item)
    return tuple(seen[:limit])


def is_trivial(text: str) -> bool:
    """挨拶と相づちだけのターンか。"""
    value = str(text or "").strip()
    if len(value) <= 2:
        return True
    return bool(_GREETING.match(value) or _BACKCHANNEL.match(value))


def is_question(text: str) -> bool:
    """Questions are evidence requests, never preference assertions."""
    value = str(text or "").strip()
    return bool(
        "?" in value or "？" in value
        or re.search(r"(?:ですか|ますか|でしょうか|だっけ|なの|か)$", value)
    )


def propose_candidates(
    user_text: str, reply: str = "", *,
    speaker_key: str = "", session_id: str = "",
    event_ids: tuple[str, ...] = (), privacy_level: str = "normal",
    input_confidence: float = 1.0, provenance: dict[str, Any] | None = None,
) -> list[MemoryCandidate]:
    """1ターンから保存候補を作る。**作れないターンの方が多くて正常。**

    `input_confidence` が低いターンは、そもそも何を言われたか怪しいので
    候補にしない（一時的なASR誤認識を覚えない）。
    """
    text = str(user_text or "").strip()
    if not text or is_trivial(text) or float(input_confidence) < .55:
        return []
    out: list[MemoryCandidate] = []
    common = {
        "participant_ids": (speaker_key,) if speaker_key else (),
        "session_id": session_id,
        "source_event_ids": tuple(event_ids),
        "topic_ids": _keys(text),
        "privacy_level": privacy_level,
        "provenance": dict(provenance or {}),
    }
    if _CORRECTION.search(text):
        # 訂正が一番強い。過去の推論を疑わせるので、他より先に置く。
        out.append(MemoryCandidate(
            proposed_summary=text[:180],
            information_type=InformationType.USER_STATEMENT,
            event_type="correction",
            importance=.85, novelty=.8, expected_future_utility=.9,
            relationship_salience=.4, confidence=.95,
            reasons=("explicit_correction",), **common,
        ))
    elif _PREFERENCE.search(text) and not is_question(text):
        out.append(MemoryCandidate(
            proposed_summary=text[:180],
            information_type=InformationType.USER_STATEMENT,
            event_type="preference",
            importance=.8, novelty=.7, expected_future_utility=.85,
            relationship_salience=.3, confidence=.95,
            reasons=("explicit_preference",), **common,
        ))
    if _PROMISE.search(text) or _PROMISE.search(str(reply or "")):
        out.append(MemoryCandidate(
            proposed_summary=(text if _PROMISE.search(text) else str(reply))[:180],
            information_type=InformationType.USER_STATEMENT,
            event_type="promise",
            importance=.85, novelty=.75, expected_future_utility=.95,
            goal_relevance=.8, confidence=.9,
            reasons=("unfinished_obligation",), **common,
        ))
    if _STRONG_AFFECT.search(text):
        out.append(MemoryCandidate(
            proposed_summary=text[:180],
            information_type=InformationType.OBSERVATION,
            event_type="strong_affect",
            importance=.7, novelty=.6, emotional_salience=.8,
            relationship_salience=.5, expected_future_utility=.6, confidence=.8,
            reasons=("strong_emotion",), **common,
        ))
    return out


def self_failure_candidate(
    pattern: str, *, detail: str = "", speaker_key: str = "",
    session_id: str = "", event_ids: tuple[str, ...] = (),
) -> MemoryCandidate:
    """**自分の失敗も覚える。** ユーザーの分析だけでは片手落ち。

    `pattern` は `kept_talking_after_end_signal` のような短い識別子。
    自由文にすると同じ失敗が別物として溜まる。
    """
    return MemoryCandidate(
        proposed_summary=(detail or pattern)[:180],
        information_type=InformationType.OBSERVATION,
        event_type=f"self_failure:{pattern}",
        importance=.75, novelty=.5, expected_future_utility=.9,
        relationship_salience=.5, confidence=.9,
        participant_ids=(speaker_key,) if speaker_key else (),
        session_id=session_id, source_event_ids=tuple(event_ids),
        topic_ids=(pattern,), reasons=("self_failure",),
    )


# ---------------------------------------------------------------------------
# 重複と訂正
# ---------------------------------------------------------------------------


def _bigrams(text: str) -> set[str]:
    value = re.sub(r"[\s　。、！!？?・,.]", "", str(text or "").lower())
    if len(value) < 2:
        return {value} if value else set()
    return {value[i:i + 2] for i in range(len(value) - 1)}


def similarity(left: str, right: str) -> float:
    """文字の重なり。**意味の一致ではない**ので、これだけで訂正を判断しない。"""
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return .0
    return len(a & b) / len(a | b)


def duplicate_score(left: str, right: str) -> float:
    """同じことを言い直しただけか。

    重なり率（Jaccard）だけでは足りない。「一問一答は嫌い」と
    「一問一答は嫌いなんだよね」は**同じことを言っている**のに、語尾が5文字
    増えるだけで .64 まで落ちる。片方がもう片方をほぼ含んでいるかも見る。

    包含だけで判定すると、短い断片が長文へ吸い込まれる（「嫌い」が何にでも
    含まれる）。**十分な長さがある時だけ**包含を使う。
    """
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return .0
    jaccard = len(a & b) / len(a | b)
    shorter = min(len(a), len(b))
    if shorter < 6:
        return jaccard
    return max(jaccard, len(a & b) / shorter)


#: **同じ軸の上で反対を向いた言明は両立しない。**
#:
#: 「短い方がいい」と「詳しい方がいい」は、文字としてはほとんど重ならないので
#: 文字列の類似度では絶対に検出できない。だから軸を明示的に持つ。
#:
#: ここに無い話題は矛盾判定に掛からない——**分からないものを矛盾と決めつけない**
#: 方が安全側。増やすときは行を足す。
AXES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "response_length": (
        ("短い", "短め", "短く", "簡潔", "手短", "一言", "端的"),
        ("詳しい", "詳しく", "詳細", "長め", "丁寧", "くわしく", "細かく"),
    ),
    "conversation_style": (
        ("一問一答", "単調", "質問ばかり", "聞き返", "淡々"),
        ("話を広げ", "自分から", "雑談", "掘り下げ", "続けて"),
    ),
    "humor": (("冗談", "ボケ", "ふざけ", "ジョーク", "からかい"), ()),
    "proactivity": (("勝手に", "余計な", "口を挟", "割り込"), ("提案", "気づいたら言", "先回り")),
}
#: 好意的な言い回し。
#:
#: 「好き」だけでは足りない。観測を書いた文（「〜を好む」）と、本人の発言
#: （「〜が好き」）は言い回しが違う。**両方が同じ軸へ乗らないと突き合わせられない。**
_POSITIVE = ("好き", "好む", "好ん", "嬉しい", "助かる", "がいい", "方がいい",
             "ほうがいい", "してほしい", "歓迎", "ありがた", "楽しい", "いい感じ",
             "望む", "求め")
#: 否定的な言い回し。**否定形（〜わけじゃない）と、結果の悪化もここ。**
#:
#: 「反応が悪くなる」は否定語を含まないが、**向きとしては否定側**。
#: これを拾えないと、自分の失敗から作った仮説が本人の発言と突き合わない。
_NEGATIVE = ("嫌い", "嫌", "やめて", "苦手", "いらない", "うんざり", "しないで",
             "わけじゃない", "わけでもない", "ではない", "じゃない", "つまらな",
             "困る", "やめとい", "望んでいない", "とは限らない",
             "悪くな", "悪化", "嫌がら", "避け")


def _side_polarity(text: str, words: tuple[str, ...]) -> int:
    """その語の**周辺**を見て、好意か否定かを決める。

    語だけ見ても意味は決まらない。「短いのが好き」と「短ければいいわけじゃない」は
    どちらも「短い」を含む。**続く十数文字**を読む。
    """
    score = 0
    for word in words:
        start = 0
        while True:
            index = text.find(word, start)
            if index < 0:
                break
            start = index + len(word)
            window = text[index:index + len(word) + 16]
            negative = any(mark in window for mark in _NEGATIVE)
            positive = any(mark in window for mark in _POSITIVE)
            if negative:
                score -= 1
            elif positive:
                score += 1
    return score


def stance(text: str) -> tuple[str, int]:
    """(軸, どちら側を望んでいるか)。分からなければ ("", 0)。

    +1 は前者の側、-1 は後者の側。片側しか語彙が無い軸（humor）は、
    好意なら +1、否定なら -1 になる。
    """
    value = str(text or "")
    best: tuple[str, int] = ("", 0)
    best_strength = 0
    for axis, (first, second) in AXES.items():
        left = _side_polarity(value, first)
        right = _side_polarity(value, second)
        signed = left - right
        if signed and abs(signed) > best_strength:
            best_strength = abs(signed)
            best = (axis, 1 if signed > 0 else -1)
    return best


def contradicts(new_text: str, old_text: str) -> bool:
    """同じ軸の上で反対を向いているか。"""
    new_axis, new_side = stance(new_text)
    old_axis, old_side = stance(old_text)
    if not new_axis or new_axis != old_axis:
        return False
    return new_side != 0 and old_side != 0 and new_side != old_side


# ---------------------------------------------------------------------------
# 保存判断
# ---------------------------------------------------------------------------


class MemoryWriteGate:
    """**覚えるかどうかを決める唯一の場所。**

    候補が規則から来ようとLLMから来ようと、ここを通る。通らない経路を
    作った時点で、重複も訂正も privacy も保証できなくなる。
    """

    def __init__(
        self, *, min_value: float = .45, duplicate_similarity: float = .78,
        hypothesis_confidence: float = .60, storage_budget: int = 2000,
    ) -> None:
        self.min_value = float(min_value)
        self.duplicate_similarity = float(duplicate_similarity)
        #: これ未満の推論は恒久記憶にしない。**仮説のまま置く。**
        self.hypothesis_confidence = float(hypothesis_confidence)
        self.storage_budget = int(storage_budget)

    def evaluate(
        self, candidate: MemoryCandidate,
        existing: list[EpisodicMemory] | tuple[EpisodicMemory, ...] = (),
        *, stored_count: int = 0,
    ) -> WriteVerdict:
        summary = str(candidate.proposed_summary or "").strip()
        if not summary:
            return WriteVerdict(WriteDecision.SKIP, ("empty",))
        # 秘密情報と、機微な個人情報は長期記憶へ入れない（第12条）。
        #
        # `high` まで落とすのは、住所・本名・病歴・年収・借金がここに入るため。
        # 会話の役には立つが、**長期に持ち続ける必要が無い**。必要なら
        # そのセッションの会話履歴から読める。
        if str(candidate.privacy_level) in PRIVACY_NEVER_STORE:
            return WriteVerdict(WriteDecision.SKIP, ("privacy_restricted",))
        if is_trivial(summary):
            return WriteVerdict(WriteDecision.SKIP, ("trivial_turn",))

        related = [
            item for item in existing
            if str(item.status) not in {
                str(MemoryStatus.DELETED), str(MemoryStatus.SUPERSEDED),
            }
        ]
        # --- 訂正 ---------------------------------------------------------
        # **物理削除しない。** 古い方に印を付けて、新しい方から繋ぐ。
        for item in related:
            if not contradicts(summary, item.summary):
                continue
            if candidate.information_type == InformationType.USER_STATEMENT and not item.is_statement:
                # 本人の明言が、こちらの推論を置き換える。これは訂正として扱う。
                return WriteVerdict(
                    WriteDecision.SUPERSEDE,
                    ("explicit_statement_over_inference", f"axis={stance(summary)[0]}"),
                    target_memory_id=item.memory_id,
                    confidence=max(float(candidate.confidence), .9),
                )
            return WriteVerdict(
                WriteDecision.CONTRADICT,
                ("conflicting_stance", f"axis={stance(summary)[0]}"),
                target_memory_id=item.memory_id,
                confidence=float(candidate.confidence),
            )

        # --- 重複 ---------------------------------------------------------
        for item in related:
            score = duplicate_score(summary, item.summary)
            if score < self.duplicate_similarity:
                continue
            if (candidate.information_type == InformationType.USER_STATEMENT
                    and not item.is_statement):
                # 同じ内容でも「推測」から「本人の申告」へ格上げする価値がある。
                return WriteVerdict(
                    WriteDecision.UPDATE, ("upgraded_to_statement",),
                    target_memory_id=item.memory_id,
                    confidence=max(float(candidate.confidence), float(item.confidence)),
                )
            return WriteVerdict(
                WriteDecision.REINFORCE, ("already_known", f"similarity={score:.2f}"),
                target_memory_id=item.memory_id,
                confidence=min(1.0, float(item.confidence) + .03),
            )

        # --- 価値 ---------------------------------------------------------
        if candidate.value < self.min_value:
            return WriteVerdict(
                WriteDecision.SKIP, ("below_value_threshold", f"value={candidate.value:.2f}"),
            )
        if stored_count >= self.storage_budget and candidate.value < .75:
            # 予算を超えたら、よほど価値が高いものしか入れない。
            return WriteVerdict(WriteDecision.SKIP, ("storage_budget",))

        # --- 確からしさ ---------------------------------------------------
        if (candidate.information_type in {InformationType.INFERENCE, InformationType.REFLECTION}
                and float(candidate.confidence) < self.hypothesis_confidence):
            # **推論を事実として置かない。** 仮説として低優先で保持する。
            return WriteVerdict(
                WriteDecision.HOLD, ("low_confidence_inference",),
                stored_status=MemoryStatus.LOW_PRIORITY,
                confidence=float(candidate.confidence),
            )
        return WriteVerdict(
            WriteDecision.STORE, candidate.reasons or ("worth_keeping",),
            confidence=float(candidate.confidence),
        )


def to_memory(candidate: MemoryCandidate, verdict: WriteVerdict) -> EpisodicMemory:
    """判断が通った候補を、保存する形へ。"""
    return EpisodicMemory(
        summary=candidate.proposed_summary[:400],
        information_type=candidate.information_type,
        event_type=candidate.event_type,
        session_id=candidate.session_id,
        participant_ids=candidate.participant_ids,
        topic_ids=candidate.topic_ids,
        source_event_ids=candidate.source_event_ids,
        confidence=float(verdict.confidence),
        importance=float(candidate.importance),
        emotional_salience=float(candidate.emotional_salience),
        relationship_salience=float(candidate.relationship_salience),
        goal_relevance=float(candidate.goal_relevance),
        retrieval_keys=candidate.topic_ids,
        status=verdict.stored_status,
    )
