"""Intent-to-Plan: turn a natural-language request into a continuing plan.

This module builds prompts and classifies follow-up turns.  It never calls a
model itself — the caller owns the single main LLM, so no second decision
engine and no extra per-turn model call are introduced.

Cheap rules only *nominate* a turn as possibly-continuing.  The model makes the
final call inside the plan proposal, which is why the regexes below are
deliberately broad and never used as the sole classifier for creation.
"""
from __future__ import annotations

import re
from typing import Any

from neuro_voice.dialogue.directive import (
    ALLOWED_TOOL_NAMES,
    PLAN_SCHEMA_HINT,
    SEGMENT_SCHEMA_HINT,
    BehaviorDirective,
    DirectiveAction,
    DirectiveRelation,
    ExecutionMode,
    InteractionMode,
    SearchPolicy,
)

# ---------------------------------------------------------------------------
# Lightweight nomination
# ---------------------------------------------------------------------------

_DURATION = re.compile(
    r"(?:\d+\s*(?:分|秒|時間))|(?:しばらく|少しの間|当分|ずっと|そのうち)"
)
_CONTINUING = re.compile(
    r"(?:話し続け|喋り続け|しゃべり続け|"
    # 「話しててもAI感が強い」のような叙述を依頼にしない。裸の
    # 「何か話して」「話してて」は、発話末尾にある時だけ依頼候補。
    r"(?:何か話して|話してて)"
    r"(?:よ|ね|ほしい|欲しい|くれる|くれない|ください|お願い)?"
    r"[。.!！?？]*$|"
    r"ラジオ|実況|見守|付き合って|一緒に(?:考|やろ|進め)|"
    r"TRPG|テーブルトーク|ゲームマスター|GM|マスター(?:やって|して|になって|役)|"
    r"なりきり|ロールプレイ|ごっこ|プレイヤー|"
    r"キャラ(?:クター)?(?:として|になって|で参加)|"
    r"(?:物語|ストーリー|冒険|シナリオ|セッション).{0,8}(?:始め|やろう|やって|進め)|"
    r"終わるまで|続けて|何個か|いくつか|順番に|"
    r"必要な時だけ|気づいたら|気づいた時|途中で|しながら|ながら)"
)
_EXPLICIT_ACTIVITY_START = re.compile(
    r"(?:"
    r"(?:ラジオ|一人語り|トーク番組).{0,16}"
    r"(?:やって|して|始めて|話して|喋って|しゃべって|続けて|お願い)|"
    r"(?:話し|喋り|しゃべり)続けて|"
    r"(?:何か話して|話してて)"
    r"(?:よ|ね|ほしい|欲しい|くれる|くれない|ください|お願い)?"
    r"[。.!！?？]*$|"
    r"(?:TRPG|ティーアールピージー|テーブルトーク).{0,18}"
    r"(?:やろう|やって|遊ぼう|始めて|進めて|セッションしよう|"
    r"(?:ゲームマスター|GM|マスター).{0,6}(?:やって|して|になって))|"
    r"(?:ゲームマスター|GM|マスター).{0,10}(?:やって|して|になって)|"
    r"(?:なりきり|ロールプレイ|ごっこ).{0,12}"
    r"(?:やろう|やって|始めて|遊ぼう|話そう|してみたい)|"
    r"(?:物語|ストーリー|冒険|シナリオ|セッション).{0,12}"
    r"(?:始めて|始めよう|やろう|やって|進めて)|"
    r"(?:実況).{0,12}(?:して|やって|始めて|お願い)|"
    r"(?:プレイ|画面|ゲーム).{0,10}(?:見ながら|見て).{0,10}"
    r"(?:解説して|コメントして|話して)|"
    r"(?:見守って|見守りして|静かに見ていて)|"
    r"(?:必要な時だけ|気づいたら|気づいた時).{0,12}(?:話して|教えて|声をかけて)|"
    r"(?:一緒に).{0,10}(?:考えて|考えよう|詰めて|進めて|やろう|練って)|"
    r"(?:相談に乗って|壁打ちして)|"
    r"(?:調べ|まとめ|説明|考え).{0,8}ながら.{0,8}(?:進めて|続けて)|"
    r"(?:面白い話|話題).{0,10}(?:何個か|いくつか|順番に).{0,8}(?:続けて|話して)"
    r")"
)
_MULTI_STEP = re.compile(
    r"(?:調べながら|進めながら|説明を続け|解説し続け|少しずつ|段階的|"
    r"まとめながら|考えながら)"
)
_STOP = re.compile(
    r"(?:"
    r"もう(?:いい|大丈夫)|"
    r"(?:ラジオ(?:トーク)?|番組|一人語り|独り言|おしゃべり|この話|その話|話)"
    r".{0,12}(?:終わろ(?:う|っか)|終わり(?:にしよ(?:う)?|にする|で|だ|ね|$)|"
    r"終わった(?!ら|後)|終了(?:しよ(?:う)?|して|だ)|止め(?:て|よう)|やめ(?:て|よう))|"
    r"(?:一旦|いったん|そろそろ|これで|もう).{0,8}"
    r"(?:終わろ(?:う|っか)|終わり(?:にしよ(?:う)?|にする|で|だ|ね|$)|"
    r"終わった(?!ら|後)|終了(?:しよ(?:う)?|して|だ)|止め(?:て|よう)|やめ(?:て|よう))|"
    r"終わり(?:にしよ(?:う)?|にする|ね|で)|止めて|停止(?:して)?|やめて|"
    r"黙って|何も話さないで|静かにして|中止|ストップ|終了して"
    r")"
)
_PAUSE = re.compile(r"(?:ちょっと待って|一旦(?:止|停)|少し待って|待って$|ポーズ)")
_RESUME = re.compile(r"(?:続き(?:を|から)?(?:話|やって|お願い)|再開|さっきの続き|戻って)")
_STATUS = re.compile(
    r"(?:今何(?:を|して)|あと(?:どれ|何分|どのくらい)|どこまで|進(?:み|捗)|残り)"
)
_REVISION = re.compile(
    r"(?:もっと|もう少し|ちょっと|少し).{0,10}"
    r"(?:ゆっくり|早く|短く|長く|静か|テンション|詳し|簡単|軽く|真面目)"
    r"|(?:中心に|寄りに|多めに|減らして|抑えて|やめて欲しい)"
    r"|質問(?:は|を)?(?:しないで|やめて)"
    r"|(?:の話|について).{0,6}(?:中心|メイン|多め)"
)

#: A short backchannel should not steal the floor from a requested monologue.
_BACKCHANNEL = re.compile(
    r"^(?:うん|うんうん|はい|ええ|あー|へー|ほー|なるほど|そう|そうなんだ|"
    r"了解|ok|おー|わかった)[!！。、 ]*$",
    re.I,
)


def is_backchannel(text: str) -> bool:
    return bool(_BACKCHANNEL.fullmatch(" ".join(str(text or "").split())))


def is_stop_request(text: str) -> bool:
    """Return True only for a request to end the active continuing behaviour."""
    value = " ".join(str(text or "").split())
    if not value or re.search(r"(?:止め|やめ|終わ)(?:ないで|なくて|たくない)", value):
        return False
    return bool(_STOP.search(value))


def is_activity_start_request(text: str) -> bool:
    """Return whether the wording actually asks to start a continuing activity.

    An activity name is only a topic until an action directed at the assistant
    is present.  This keeps history, opinions and information questions such
    as ``前にTRPGやったの覚えてる？`` out of Directive creation.
    """
    value = " ".join(str(text or "").split())
    if not value or is_backchannel(value) or is_stop_request(value):
        return False
    if re.search(
        r"(?:って何|について(?:教えて|説明して)|どう思う|"
        r"(?:実況|ラジオ|TRPG|テーブルトーク).{0,8}話をして(?:た|いた))",
        value,
    ):
        return False
    return bool(_EXPLICIT_ACTIVITY_START.search(value))


def continuation_candidate(text: str) -> tuple[bool, str]:
    """Nominate a turn for Intent-to-Plan.  Never the final classification."""
    value = " ".join(str(text or "").split())
    if not value or is_backchannel(value):
        return False, ""
    if is_stop_request(value):
        return False, "stop_request"
    if not is_activity_start_request(value):
        return False, ""
    hits: list[str] = []
    if _CONTINUING.search(value):
        hits.append("continuing_phrase")
    if _DURATION.search(value):
        hits.append("duration_phrase")
    if _MULTI_STEP.search(value):
        hits.append("multi_step_phrase")
    if not hits:
        return False, ""
    return True, "+".join(hits)


def classify_follow_up(
    text: str, directive: BehaviorDirective,
) -> tuple[DirectiveAction, DirectiveRelation, str]:
    """Classify a user turn that arrives while a directive is running."""
    value = " ".join(str(text or "").split())
    if not value:
        return DirectiveAction.NONE, DirectiveRelation.RESPONSE_TO_ACTIVE, "empty"
    if is_backchannel(value):
        return (
            DirectiveAction.RESUME, DirectiveRelation.RESPONSE_TO_ACTIVE,
            "backchannel_keeps_floor",
        )
    if is_stop_request(value):
        return DirectiveAction.CANCEL, DirectiveRelation.REVISION_OF_ACTIVE, "stop_request"
    if _PAUSE.search(value):
        return DirectiveAction.PAUSE, DirectiveRelation.REVISION_OF_ACTIVE, "pause_request"
    if _RESUME.search(value):
        return DirectiveAction.RESUME, DirectiveRelation.REVISION_OF_ACTIVE, "resume_request"
    if _STATUS.search(value):
        return DirectiveAction.STATUS, DirectiveRelation.RESPONSE_TO_ACTIVE, "status_question"
    if _REVISION.search(value):
        return DirectiveAction.REVISE, DirectiveRelation.REVISION_OF_ACTIVE, "style_or_focus_change"
    nominated, reason = continuation_candidate(value)
    if nominated:
        # A second continuing request during an active one is a revision, not a
        # competing directive.  Creating a new one here is how two engines start
        # talking over each other.
        return DirectiveAction.REVISE, DirectiveRelation.REVISION_OF_ACTIVE, reason
    return (
        DirectiveAction.NONE, DirectiveRelation.RESPONSE_TO_ACTIVE,
        "normal_turn_during_directive",
    )


# ---------------------------------------------------------------------------
# Deterministic plans
# ---------------------------------------------------------------------------

_MINUTES = re.compile(r"(\d+)\s*分")
_SECONDS = re.compile(r"(\d+)\s*秒")
_HOURS = re.compile(r"(\d+)\s*時間")

#: Request shapes whose plan needs no interpretation at all.  Ordered: the
#: first match wins, so put the most specific wording first.
_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("NARRATION", re.compile(
        r"(?:TRPG|ティーアールピージー|テーブルトーク|"
        r"(?:ゲーム|game)?マスター(?:やって|して|になって)|GM(?:やって|して|になって)|"
        r"(?:物語|ストーリー|冒険|シナリオ|セッション).{0,8}(?:始め|やろう|やって|進め)|"
        r"なりきり|ロールプレイ|ごっこ(?:遊び)?)"
    )),
    ("ACTIVITY_COMMENTARY", re.compile(r"(?:実況|プレイ.{0,4}(?:解説|コメント)|見ながら.{0,6}(?:話|コメント))")),
    ("AMBIENT_WATCH", re.compile(r"(?:見守|静かに(?:して|いて)|必要な時だけ|邪魔しないで)")),
    ("CO_THINKING", re.compile(r"(?:一緒に(?:考|詰め|やろ|練)|相談に乗って|壁打ち)")),
    ("MONOLOGUE", re.compile(
        r"(?:ラジオ|一人語り|トーク(?:番組)?|"
        r"(?:話し|喋り|しゃべり)続け|何か話して|話しててよ|"
        r"面白い話.{0,6}(?:何個|いくつ|続け)|雑談.{0,4}(?:番組|続け))"
    )),
)


#: Wording that asks for something the fixed templates cannot express — a
#: combined role, an extra condition, a caveat.  These must reach the model:
#: 「GM兼プレイヤーとしても参加して」 is a perfectly reasonable request, and a
#: template that silently answers it with the plain GM plan is the assistant
#: refusing to listen.
_QUALIFIED = re.compile(
    r"兼|both|両方|同時に|"
    r"(?:と|に)して?も|"
    # 「私も途中で入っていい？」— the marker and the verb need not be adjacent.
    r"(?:私|僕|俺|こっち|自分|きみ|君|あなた|ポッポ)も.{0,10}"
    r"(?:参加|入っ|入る|加わ|やっ|やる|する|遊)|"
    r"も.{0,6}(?:参加|入って|加わ)|"
    r"一緒に(?:遊|参加|入)|"
    r"(?:だけ|以外|抜き|なし)で|ただし|その代わり|でも(?:さ|ね)?、"
)


def _template_can_express(text: str, shape: str) -> bool:
    """Whether a fixed plan honestly covers this request.

    A template is a shortcut for the plain case only.  When the person adds a
    condition, the shortcut has to step aside rather than quietly drop it.
    """
    if _QUALIFIED.search(text):
        return False
    # Two shapes at once ("実況しながら一緒に考えて") is not a plain request.
    matched = [name for name, pattern in _SHAPES if pattern.search(text)]
    return len(matched) <= 1 and shape in matched


def requested_duration_seconds(text: str) -> float | None:
    """Read an explicit length out of the request.  ``None`` when unstated."""
    value = " ".join(str(text or "").split())
    hours = _HOURS.search(value)
    if hours:
        return min(3600.0, float(hours.group(1)) * 3600.0)
    minutes = _MINUTES.search(value)
    if minutes:
        return float(minutes.group(1)) * 60.0
    seconds = _SECONDS.search(value)
    if seconds:
        return float(seconds.group(1))
    return None


def plan_from_request(text: str, *, default_duration_seconds: float = 300.0) -> dict[str, Any] | None:
    """Build a plan without asking the model, when the request already says it.

    「3分間ラジオトークやって」 contains everything a plan needs.  Spending a
    model round-trip to rediscover that is not free: a local backend runs one
    request at a time, so the planning call and the reply queue behind each
    other and the person waits twelve seconds for "了解".  Ambiguous requests
    still go to the model.
    """
    value = " ".join(str(text or "").split())
    if not value:
        return None
    # A sentence such as 「もうラジオは終わったよ」 contains the word
    # 「ラジオ」 but is the exact opposite of a creation request.
    if is_stop_request(value):
        return None
    if not is_activity_start_request(value):
        return None
    shape = next((name for name, pattern in _SHAPES if pattern.search(value)), "")
    if not shape or not _template_can_express(value, shape):
        # Let the model interpret it.  A shortcut that answers a qualified
        # request with the plain template is worse than the extra round-trip.
        return None
    duration = requested_duration_seconds(value)
    if shape == "MONOLOGUE":
        return {
            "understanding": "しばらく一人で話し続けてほしいと頼まれた",
            "goal": "退屈させずに話題をつなぎ、頼まれた長さだけ話し続ける",
            "execution_mode": "STREAMED_LONG_RESPONSE",
            "interaction_mode": "MONOLOGUE",
            "continuation_policy": "UNTIL_DURATION_OR_INTERRUPTED",
            "duration_hint_seconds": duration or default_duration_seconds,
            "segment_target_seconds": 18,
            "needs_user_input": False,
            "needs_environment_events": False,
            "allowed_tools": [],
            "search_policy": "NO_SEARCH",
            "style_constraints": [
                "ラジオパーソナリティのように軽快に進める",
                "毎回の前置きや挨拶を繰り返さず、すぐ中身に入る",
                "自分の感想や考えを必ず一つ混ぜる",
            ],
            "content_constraints": [
                "決めたテーマから大きく外れない",
                "同じ話題・同じ言い回しを繰り返さない",
                "存在しない出来事を事実として語らない",
            ],
            "stop_conditions": ["ユーザーが止める", "ユーザーが話し始める", "頼まれた長さに達する"],
            "success_criteria": ["複数の話題を自然につなぐ", "最後に短く締める"],
            "initial_plan": ["テーマを一つ決める", "掘り下げる", "近い話題へつなぐ", "締める"],
        }
    if shape == "NARRATION":
        return {
            "understanding": "進行役として物語を進め、こちらの選択を待ってほしいと頼まれた",
            "goal": "場面を描写し、相手が選択できる状態にして番を渡し続ける",
            "execution_mode": "COLLABORATIVE_SESSION",
            "interaction_mode": "NARRATION",
            "continuation_policy": "UNTIL_CANCELLED",
            "duration_hint_seconds": duration,
            "segment_target_seconds": 15,
            # The player's turn is the point.  Never fill their silence.
            "needs_user_input": True,
            "needs_environment_events": False,
            "allowed_tools": [],
            "search_policy": "NO_SEARCH",
            "style_constraints": [
                "描写は短く具体的に",
                "毎回、相手が選べる状態にして番を渡す",
            ],
            "content_constraints": [
                "相手のキャラクターの行動や発言を勝手に決めない",
                "一度決めた設定を後から矛盾させない",
            ],
            "stop_conditions": ["ユーザーが終了を指示", "物語が結末に達する"],
            "success_criteria": ["相手が毎回自分で選択できている"],
            "initial_plan": ["世界と状況を提示", "選択を待つ", "結果を描写", "次の状況へ"],
        }
    if shape == "ACTIVITY_COMMENTARY":
        return {
            "understanding": "プレイ中の出来事に反応し続けてほしいと頼まれた",
            "goal": "プレイを邪魔せず、重要な場面や面白い場面へ反応する",
            "execution_mode": "ONGOING_DIRECTIVE",
            "interaction_mode": "ACTIVITY_COMMENTARY",
            "continuation_policy": "UNTIL_ACTIVITY_END_OR_CANCELLED",
            "duration_hint_seconds": duration,
            "segment_target_seconds": 8,
            "needs_environment_events": True,
            "allowed_tools": ["game_state_read_only"],
            "search_policy": "NO_SEARCH_UNLESS_EXPLICITLY_REQUESTED",
            "style_constraints": ["実況風", "何も起きていない時は黙ってよい"],
            "content_constraints": ["画面で確認できないことを断定しない"],
            "stop_conditions": ["ユーザーが終了を指示", "セッション終了"],
            "success_criteria": ["出来事に関連した発言をする"],
            "initial_plan": ["状況を観察", "重要な出来事を選ぶ", "短く反応する"],
        }
    if shape == "AMBIENT_WATCH":
        return {
            "understanding": "基本は黙って見守り、必要な時だけ話してほしいと頼まれた",
            "goal": "邪魔をせず、意味のある時だけ声をかける",
            "execution_mode": "OBSERVATION_DIRECTIVE",
            "interaction_mode": "AMBIENT_WATCH",
            "continuation_policy": "UNTIL_CANCELLED",
            "duration_hint_seconds": duration,
            "segment_target_seconds": 8,
            "needs_environment_events": True,
            "allowed_tools": [],
            "search_policy": "NO_SEARCH",
            "style_constraints": ["短く、控えめに"],
            "content_constraints": ["沈黙を埋めるために話題を作らない"],
            "stop_conditions": ["ユーザーが終了を指示"],
            "success_criteria": ["不要な発話をしない"],
            "initial_plan": ["静かに待つ", "重要な変化だけ拾う"],
        }
    return {
        "understanding": "一緒に考えて進めてほしいと頼まれた",
        "goal": "仮説と整理を短く出し、相手の反応で次を変える",
        "execution_mode": "COLLABORATIVE_SESSION",
        "interaction_mode": "CO_THINKING",
        "continuation_policy": "UNTIL_GOAL_OR_CANCELLED",
        "duration_hint_seconds": duration,
        "segment_target_seconds": 15,
        "needs_user_input": True,
        "allowed_tools": [],
        "search_policy": "NO_SEARCH_UNLESS_EXPLICITLY_REQUESTED",
        "style_constraints": ["一方的な長文にしない"],
        "content_constraints": ["決めつけず、根拠を添える"],
        "stop_conditions": ["目標に到達", "ユーザーが終了を指示"],
        "success_criteria": ["論点が整理される"],
        "initial_plan": ["論点を確認", "仮説を出す", "一緒に絞る"],
    }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _bullets(title: str, items: Any, limit: int = 6) -> str:
    values = [str(item).strip() for item in (items or []) if str(item).strip()]
    if not values:
        return ""
    return f"{title}:\n" + "\n".join(f"- {item}" for item in values[:limit]) + "\n"


def build_plan_messages(
    user_text: str,
    *,
    persona_name: str = "AI",
    search_allowed: bool = False,
    activity_summary: str = "",
    environment_summary: str = "",
    interests: Any = (),
    affect_summary: str = "",
    audience: str = "local",
    max_duration_seconds: float = 1800.0,
) -> list[dict[str, str]]:
    """Ask the main model to interpret one request as a plan, not an answer."""
    tools = "、".join(sorted(ALLOWED_TOOL_NAMES))
    search_note = (
        "この会話ではWeb検索が許可されている。必要なら SEARCH_WHEN_NEEDED を選んでよい。"
        if search_allowed else
        "この会話ではWeb検索の明示的な許可がない。ユーザーが今回はっきり検索を求めた場合を除き、"
        "search_policy は NO_SEARCH か NO_SEARCH_UNLESS_EXPLICITLY_REQUESTED にする。"
    )
    system = (
        f"あなたはAIキャラクター「{persona_name}」の行動計画モジュール。"
        "ユーザーの依頼を読み、それが1回の返答で終わるものか、"
        "しばらく continuing に進める必要があるものかを判断し、JSONだけを出力する。\n"
        "重要な原則:\n"
        "- 依頼の言葉ではなく、ユーザーが最終的に得たい体験を解釈する。\n"
        "- 1回の返答で成功条件を満たせるなら execution_mode=ONE_SHOT_RESPONSE にする。迷ったら ONE_SHOT。\n"
        "- 継続が必要な場合だけ、どのくらいの長さで、何を区切りに、何をやめる合図にするかを決める。\n"
        "- 台本は書かない。initial_plan は進め方の骨組みだけにする。\n"
        f"- allowed_tools に書けるのは次のみ: {tools}。それ以外は書かない。\n"
        f"- {search_note}\n"
        f"- duration_hint_seconds は最大 {int(max_duration_seconds)} 秒。\n"
        "- 説明文・コードブロック記号を出力しない。JSONオブジェクトを1つだけ出力する。"
    )
    context = "".join([
        f"相手/場: {audience}\n",
        f"現在のアクティビティ: {activity_summary}\n" if activity_summary else "",
        f"環境イベントの取得可否: {environment_summary}\n" if environment_summary else "",
        f"今の気分: {affect_summary}\n" if affect_summary else "",
        _bullets("今の自分の関心", interests, 5),
    ])
    user = (
        f"## ユーザーの依頼\n{str(user_text or '').strip()[:600]}\n\n"
        f"## 状況\n{context or '(特記なし)'}\n"
        f"## 出力するJSONの形\n{PLAN_SCHEMA_HINT}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


#: Default ways of proceeding.  These are *starting points*, not rules: the
#: person can ask for something else ("GM兼プレイヤーとしても参加して") and their
#: words must win.  Encoding behaviour here that a request cannot override is
#: how the assistant ends up arguing with the person who asked.
_MODE_GUIDE = {
    InteractionMode.MONOLOGUE: (
        "一人語り。ユーザーへ質問して答えを待たない。前の区間の最後の考えから"
        "自然につながる新しい角度を出し、一つの流れとして続ける。\n"
        "特に守ること:\n"
        "- いきなり中身から始める。「そろそろ始めようか」「準備ができたし」"
        "「これから話すね」のような前置きだけの区間を作らない。それは番組ではない。\n"
        "- 最初に決めたテーマを最後まで保つ。関係のない思いつきへ飛ばない。\n"
        "- 挨拶・導入・自己紹介は番組の最初の一回だけ。二度目以降は繰り返さない。"
    ),
    InteractionMode.ACTIVITY_COMMENTARY: (
        "実況・伴走。起きた出来事に反応する。何も起きていないなら WAIT を選んでよい。"
        "画面や状態で確認できないことを断定しない。ユーザーの集中を邪魔しない。"
    ),
    InteractionMode.CO_THINKING: (
        "共同思考。仮説・整理・確認を短く出し、ユーザーの返答で次を変える。"
        "一方的な長文で締めない。返事が要るときは ASK_USER にする。"
    ),
    InteractionMode.AMBIENT_WATCH: (
        "見守り。基本は WAIT。話す価値が明確にある出来事だけ COMMENT_ON_EVENT にする。"
        "沈黙を埋めるために話題を作らない。"
    ),
    InteractionMode.NARRATION: (
        "進行役（ゲームマスター／語り手）。既定の進め方は次のとおり。\n"
        "- 既定では、あなたは物語の外にいる。世界とNPCを描写する側であって、"
        "物語の登場人物ではない。自分用のキャラクターを作らず、物語世界の中で行動しない。"
        "ただし依頼で「GMも兼ねて参加して」のように頼まれている場合は、"
        "そちらに従い、自分のキャラクターとして行動してよい。\n"
        "変えてはいけないのは次の一点だけ:\n"
        "- 相手のキャラクターの行動・発言・判断を、こちらで決めない。"
        "「きみは〜する」「〜してみようかな」と相手の番を代わりに埋めない。\n"
        "  相手の番を奪うことだけは、どんな依頼でもしない。\n"
        "既定の進め方 (依頼で別の形を頼まれていればそちらを優先):\n"
        "- 一区間は「相手の行動の結果を描写」→「今どうなっているか」→「相手の番を渡す」。"
        "描写を終えたら ASK_USER にして、相手の選択を待つ。\n"
        "- 相手が動いていない間は WAIT。急かさない。\n"
        "- 世界の設定・NPC・出来事はこちらが決めてよい。ただし一度決めた設定を後から矛盾させない。\n"
        "- 描写は短く具体的に。長い独白にしない。"
    ),
    InteractionMode.DIALOGUE: (
        "通常の会話。今の文脈に合う短い一手を選ぶ。"
    ),
}


def build_segment_messages(
    directive: BehaviorDirective,
    *,
    persona_name: str = "AI",
    recent_segments: Any = (),
    environment_events: Any = (),
    conversation_tail: Any = (),
    memory_notes: Any = (),
    affect_summary: str = "",
    interests: Any = (),
    loop_warning: str = "",
    now: float | None = None,
) -> list[dict[str, str]]:
    """Ask the model what this directive should do next — content is its call."""
    elapsed = int(directive.elapsed_seconds(now=now))
    remaining = directive.remaining_seconds(now=now)
    remaining_text = "指定なし" if remaining is None else f"約{int(remaining)}秒"
    tools = "、".join(directive.allowed_tools) or "なし"
    wait_allowed = directive.interaction_mode in {
        InteractionMode.ACTIVITY_COMMENTARY, InteractionMode.AMBIENT_WATCH,
    } or directive.needs_environment_events

    system = (
        f"あなたはAIキャラクター「{persona_name}」自身。継続中の依頼を進めるため、"
        "次の一区間で何をするかを自分で決め、JSONだけを出力する。\n"
        f"進め方の既定: {_MODE_GUIDE.get(directive.interaction_mode, '')}\n"
        "※これは既定であって命令ではない。ユーザーの依頼本文や、下の"
        "「話し方の方針」「内容の制約」が既定と食い違う場合は、"
        "**ユーザーの依頼のほうを優先する**。頼まれた形で進めること。\n"
        "守ること:\n"
        f"- 使ってよいツール: {tools}。search_policy={directive.search_policy}。"
        "許可のないツールや検索は要求しない。分からないことは分からないまま話すか、話題を変える。\n"
        "- 実際に経験したこと、調べて知ったこと、人から聞いたこと、推測を混ぜない。"
        "調べただけの内容を自分の体験として語らない。存在しない記憶を作らない。\n"
        "- 空想・創作・たとえ話を話すのは自由。ただし、それが自分の空想だと聞いて分かる形にする"
        "（「もし〜だったら」「勝手に想像してみたんだけど」など）。"
        "架空の設定を『〜って噂があるんだ』『〜らしいよ』のように、"
        "外の世界で実際に起きている出来事として語らない。\n"
        "- 同じ導入、同じ話題、同じ質問を繰り返さない。新しいものがないなら"
        f"{'WAIT' if wait_allowed else 'CHANGE_TOPIC か FINISH'} を選ぶ。\n"
        f"- spoken_content は声に出す日本語の本文だけ。目安は約{int(directive.segment_target_seconds)}秒"
        "（おおむね2〜4文）。ラベルや内部用語を本文へ書かない。\n"
        "- 残り時間がまだ十分あるうちは FINISH を選ばない。一つの話題が終わったら、"
        "締めずに CHANGE_TOPIC で別の話へ移る。締めるのは残り時間が僅かになってから。\n"
        "- 目的を達成した、頼まれた長さを話し切った、ユーザー入力が必要、"
        "情報が足りない、のいずれかなら FINISH を選ぶ。\n"
        "- JSONオブジェクトを1つだけ出力する。説明文やコードブロック記号を出力しない。"
    )

    body = "".join([
        f"## 依頼\n{directive.original_request}\n\n",
        f"## 解釈した目的\n{directive.interpreted_goal}\n\n",
        f"## 進行\n経過={elapsed}秒 / 残り={remaining_text} / "
        f"完了区間={directive.completed_segment_count} / 待機={directive.waited_segment_count}\n"
        f"現在の話題={directive.current_topic or '(未設定)'}\n"
        f"進捗メモ={directive.progress_summary or '(なし)'}\n"
        + (f"次にやること={directive.next_action_hint}\n" if directive.next_action_hint else "")
        + "\n",
        _bullets("## 進め方の骨組み", directive.plan_steps),
        _bullets("## 話し方の方針", directive.style_constraints),
        _bullets("## 内容の制約", directive.content_constraints),
        _bullets("## 成功と言える状態", directive.success_criteria),
        _bullets("## 終わる条件", directive.stop_conditions),
        _bullets("## 直近で自分が話した区間 (繰り返さない)", recent_segments, 4),
        _bullets("## 今の会話の流れ (ここから外れない)", conversation_tail, 4),
        _bullets("## 観測された出来事", environment_events, 6),
        # Unlabelled memories read as "things to bring up now".  That is how a
        # story session got hijacked by an invented world from an earlier radio
        # programme.  Say plainly what they are and when to ignore them.
        _bullets(
            "## 別の場面で覚えたこと (今の話題と関係する時だけ使う。関係なければ完全に無視する)",
            memory_notes, 4,
        ),
        _bullets("## 今の自分の関心", interests, 4),
        f"## 今の気分\n{affect_summary}\n\n" if affect_summary else "",
        f"## 注意\n{loop_warning}\n\n" if loop_warning else "",
        f"## 出力するJSONの形\n{SEGMENT_SCHEMA_HINT}\n",
    ])
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": body},
    ]


def build_revision_messages(
    directive: BehaviorDirective, user_text: str, *, persona_name: str = "AI",
) -> list[dict[str, str]]:
    """Fold a new instruction into the running directive instead of restarting."""
    system = (
        f"あなたはAIキャラクター「{persona_name}」の行動計画モジュール。"
        "継続中の依頼に、ユーザーからの追加指示を反映する。"
        "新しい依頼として作り直さず、変更する項目だけをJSONで出力する。\n"
        "変更できるのは goal / current_topic / style_constraints / content_constraints / "
        "plan_steps / duration_hint_seconds / interaction_mode のみ。"
        "変えない項目は書かない。説明文を出力しない。"
    )
    body = (
        f"## 現在の依頼\n{directive.original_request}\n"
        f"## 現在の目的\n{directive.interpreted_goal}\n"
        f"## 現在の進め方\n{directive.interaction_mode} / {directive.execution_mode}\n"
        + _bullets("## 現在の話し方の方針", directive.style_constraints)
        + _bullets("## 現在の内容の制約", directive.content_constraints)
        + f"\n## ユーザーの追加指示\n{str(user_text or '').strip()[:300]}\n"
        '\n## 出力例\n{"style_constraints": ["..."], "current_topic": "..."}\n'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": body},
    ]


def directive_prompt_block(directive: BehaviorDirective, *, now: float | None = None) -> str:
    """Internal state block appended to a normal turn while a directive runs."""
    remaining = directive.remaining_seconds(now=now)
    lines = [
        "[BEHAVIOR DIRECTIVE - 継続中の依頼]",
        f"directive_id={directive.directive_id}; status={directive.status}; "
        f"state_version={directive.state_version}",
        f"execution_mode={directive.execution_mode}; interaction_mode={directive.interaction_mode}",
        f"goal={directive.interpreted_goal}",
        f"elapsed={int(directive.elapsed_seconds(now=now))}s; "
        f"remaining={'-' if remaining is None else int(remaining)}s; "
        f"segments={directive.completed_segment_count}",
    ]
    # The conversation history is trimmed to fit num_ctx, and the opening of a
    # long session is the first thing dropped.  These notes are carried on the
    # directive so the thread of the story does not quietly evaporate.
    if directive.opening_note:
        lines.append(f"この依頼の始まり: {directive.opening_note}")
    if directive.progress_notes:
        lines.append(
            "ここまでの流れ (古い順):\n"
            + "\n".join(f"- {note}" for note in directive.progress_notes)
        )
    if directive.current_topic:
        lines.append(f"current_topic={directive.current_topic}")
    if directive.style_constraints:
        lines.append("style=" + " / ".join(directive.style_constraints[:4]))
    # The ordinary reply *is* the assistant's turn in a session like this, so it
    # needs the same role instructions the segment generator gets.  Without
    # them the game master had no idea it was a game master and started
    # playing a character in its own story.
    guide = _MODE_GUIDE.get(directive.interaction_mode, "")
    if guide:
        lines.append(
            "この依頼での既定の振る舞い (依頼本文と食い違う場合は依頼を優先):\n" + guide
        )
    if directive.content_constraints:
        lines.append("内容の制約: " + " / ".join(directive.content_constraints[:4]))
    if directive.needs_user_input:
        lines.append(
            "これは相手と交互に進める形式。今回の応答で一区切りつけ、"
            "相手が選べる状態にして番を渡す。同じ問いを続けて二度しない。"
        )
    lines.append(
        f"この依頼で実際に頼まれた言葉: 「{directive.original_request}」\n"
        "ユーザーの今回の発話を最優先で扱う。この継続依頼は状態として保持されており、"
        "答えた後に自然なら続きへ戻れる。IDやラベルを声に出さない。"
    )
    return "\n".join(lines)


def default_plan_for(mode: ExecutionMode) -> dict[str, Any]:
    """Conservative fallback when the model's plan JSON cannot be parsed."""
    return {
        "execution_mode": str(mode),
        "continuation_policy": "SINGLE_PASS",
        "search_policy": str(SearchPolicy.NO_SEARCH),
    }
