"""Surface realization instructions: turn a plan into natural spoken Japanese."""
from __future__ import annotations

import re

from neuro_voice.dialogue.conversation_planner import ConversationPlan


class SurfaceRealizer:
    """Keep wording separate from the planner without another slow LLM call."""

    def __init__(self) -> None:
        self._extensions: list = []

    def register_extension(self, extension) -> None:
        """Register a callable returning extra bounded realization guidance."""
        if extension not in self._extensions:
            self._extensions.append(extension)

    @staticmethod
    def _reply_opening(text: str, *, max_chars: int = 48) -> str:
        """Return one concrete, spoken opening without copying a whole reply.

        The planner already stores recent replies for the critic.  Reusing only
        the first sentence here gives the model a concrete collision target
        without creating another transcript store or flooding the prompt.
        """
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value:
            return ""
        first = re.split(r"(?<=[。！？!?])|\n", value, maxsplit=1)[0].strip()
        return first[:max(12, int(max_chars))]

    @classmethod
    def recent_reply_context(
        cls, recent_replies: list[str] | tuple[str, ...] | None, *,
        count: int = 4, max_chars: int = 48, max_total_chars: int = 280,
    ) -> str:
        """Build turn-local concrete anti-repetition context.

        This is intentionally dynamic evidence rather than an always-on list
        of forbidden phrases.  It adds no LLM call and is never written to the
        metrics log.
        """
        openings: list[str] = []
        for reply in list(recent_replies or ())[-max(1, int(count)):]:
            opening = cls._reply_opening(reply, max_chars=max_chars)
            if opening and opening not in openings:
                openings.append(opening)
        if not openings:
            return ""
        header = "【直近の自分の発話・今回だけの重複回避材料】\n"
        footer = (
            "\n上と同じ入り方や文の運びをなぞらず、今回の内容に合う別の自然な入り方を選ぶ。"
        )
        budget = max(80, int(max_total_chars)) - len(header) - len(footer)
        lines: list[str] = []
        used = 0
        for opening in openings:
            line = f"- {opening}"
            if lines and used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1
        return header + "\n".join(lines) + footer if lines else ""

    @staticmethod
    def _persona_expression_rule(plan: ConversationPlan) -> str:
        """Convert personality axes into bounded, context-aware wording guidance."""
        traits = plan.persona_traits or {}
        serious = plan.intent in {"support", "correction"} or plan.temperature in {"quiet", "serious"}
        if serious:
            return (
                "今回は遊び心・生意気さ・大げさなリアクションを抑え、相手の具体的な内容を"
                "まっすぐ受け止める。普段との落差で人格を見せる。"
            )

        rules: list[str] = []
        if float(traits.get("spontaneity", .5)) >= .65:
            rules.append(
                "面白い、意外、悔しい等の反応が本当にある時は、説明より先に短く素直な反応を出してよい"
            )
        if float(traits.get("playfulness", .5)) >= .65:
            rules.append(
                "発話中の具体的な一語か状況を拾い、一度だけ遊びに変える。幼児語や固定のかわいい語尾にはしない"
            )
        if float(traits.get("cheekiness", .5)) >= .55:
            if plan.intimacy_level >= .45:
                rules.append(
                    "安全な雑談では、一度だけ軽く張り合う、得意げになる、やさしくからかう反応を許可する。侮辱や見下しにはしない"
                )
            else:
                rules.append(
                    "生意気さは相手でなく自分や状況へ向け、馴れ馴しいからかいは避ける"
                )
        if float(traits.get("humor", .5)) >= .65:
            rules.append(
                "冗談は今の具体的な文脈から作り、一発で本題へ戻る。定番の自称や同じ小ボケを反復しない"
            )
        if not rules:
            return "人格を演じるための口癖を足さず、今回の内容に即した自然な反応を優先する。"
        return (
            "子供っぽさは、無知を装うことではなく、好奇心・遊びへの乗りの良さ・感情の素直さとして表す。"
            + " / ".join(rules)
            + "。ただし全項目を毎回実行せず、今回もっとも自然な一つだけを選ぶ。"
        )

    def prompt(
        self, plan: ConversationPlan, critic_feedback: str = "", *,
        recent_replies: list[str] | tuple[str, ...] | None = None,
        recent_reply_count: int = 4,
        recent_reply_max_chars: int = 48,
        recent_reply_max_total_chars: int = 280,
    ) -> str:
        move_rule = {
            "DIRECT_ANSWER": "今回は直接答える。答えた後に別の質問を付け足さない。",
            "BRIEF_REACTION": "今回は短い反応だけで十分。説明や新しい話題を無理に足さない。",
            "SHARE_OPINION": "今回は自分の見解を一つ、理由とともに示す。",
            "PLAYFUL_REACTION": "今回は今の具体的な内容への軽い遊び心を一度だけ使う。",
            "EXTEND_TOPIC": "今回は今の話から自然な一歩だけ広げる。",
            "ASK_QUESTION": "今回は直接の反応を返した後、意味のある質問を一つだけする。",
            "REPAIR": "今回は誤りを認め、正しい理解へ短く修復する。",
            "SUPPORT": "今回は具体的に受け止め、助言を押しつけず余白を残す。",
            "HONEST_RECALL": "今回は取得できた記録を根拠に、具体的に思い出して答える。",
            "SAY_NOT_REMEMBERED": "今回は記録がないことを率直に伝える。曖昧な過去を作らず、Web検索もしない。",
        }.get(plan.conversation_move, "今回は選ばれた会話行為を一つだけ明確に実現する。")
        question_rule = (
            "質問するなら一つだけ。先に答え・予想・考察を返し、質問だけで終わらない。"
            if plan.ask_follow_up else
            "今回は質問を義務的に足さない。見解、感情、余韻のどれかで自然に閉じてよい。"
        )
        correction_rule = (
            "『あれ、いや待って』のような軽い言い直しは、考え直す場面だけ低頻度で許可する。"
            "事実をわざと間違えたり、存在しない経験を作ったりしない。"
        )
        continuity_rule = (
            "一問一答で閉じず、直接の答えに自分の見方・小さな提案・自然な連想のいずれかを"
            "一つだけ添えて会話を前へ進める。ただし無理に質問を足さない。"
            if plan.target_length != "short" and plan.intent not in {"correction", "support"}
            else "短い返答が自然な場面では、無理に話を広げない。"
        )
        if plan.continuation_requested:
            continuity_rule = (
                "これは継続トークの最初の区間。返事だけで終了せず、一つの話題に自分の見方と"
                "近い連想を重ねる。ユーザーへの質問や、過去の未解決事項の確認には切り替えない。"
            )
        persona_rule = self._persona_expression_rule(plan)
        recent_reply_rule = self.recent_reply_context(
            recent_replies,
            count=recent_reply_count,
            max_chars=recent_reply_max_chars,
            max_total_chars=recent_reply_max_total_chars,
        )
        extension_rules: list[str] = []
        for extension in self._extensions:
            try:
                rule = str(extension(plan) or "").strip()
                if rule:
                    extension_rules.append(rule[:500])
            except Exception:
                continue
        return (
            "【Surface Realizer・最終発話規則】\n"
            "上のConversation Plannerが『何を話すか』を決めた。あなたは『どう話すか』だけを自然な日本語音声会話として実現する。\n"
            f"{move_rule}\n"
            f"{question_rule}\n"
            "質問を選んだターンでも、質問だけで終わらない。\n"
            # 「毎回『なるほど』『確かに』から始めない」は常時ONから外した。
            # ConversationCritic が canned_opening / repeated_opening を実際に
            # 検出した時だけ、critic_feedback が同じことを言う。起きてもいない
            # 違反を毎ターン先回りで禁じるのをやめ、起きた時にだけ現れる形にする。
            "短文と少し長い文を混ぜ、感情は説明せず言葉選びとリズムににじませる。笑いやツッコミは空気に合う時だけ。\n"
            "確定した記憶だけを使う。読んだ・見た・遊んだ・行った等の過去経験を、根拠なしに自分の経験として語らない。"
            "知らない事実は知らないと言い、ユーザーの誤った前提へ迎合しない。\n"
            f"{correction_rule}\n"
            f"{continuity_rule}\n"
            f"{persona_rule}\n"
            + ("継続中の会話の焦点を踏まえ、前の話を忘れたような返答にしない。\n"
               if plan.continuity_focus else "")
            # 「内部のものを出力するな」は persona の最終出力ルールが一箇所で担う。
            # ここにも同じ趣旨が2文あり、『The user is asking』…の列挙に至っては
            # persona と一字一句同じものが重複していた。
            + (critic_feedback + "\n" if critic_feedback else "")
            + (recent_reply_rule + "\n" if recent_reply_rule else "")
            + (("【拡張表現ルール】" + " / ".join(extension_rules) + "\n") if extension_rules else "")
            + "これは回答文の後編集ではなく最終発話そのもの。会話相手として、自分の具体的な反応を含める。"
        )
