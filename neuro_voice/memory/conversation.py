"""会話履歴と人格 (System Prompt) の管理。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DeferredTopic:
    """割り込みでいったん保留した会話のスナップショット。"""

    summary: str
    messages: list[dict[str, str]]
    interrupted_turn: "InterruptedTurn | None" = None


@dataclass
class InterruptedTurn:
    """Assistant turn that was stopped after the user had heard only part."""

    response_id: str
    original_user_text: str
    full_assistant_text: str
    played_text: str
    unplayed_text: str
    interrupted_at: float
    interruption_text: str | None = None
    resume_recommended: bool = False


class ConversationManager:
    """履歴とシステムプロンプトを管理する。将来 Memory Engine へ拡張予定。"""

    def __init__(self, system_prompt: str, max_turns: int = 20):
        self._system = system_prompt
        self._max_turns = max_turns
        self._history: list[dict[str, str]] = []
        self._deferred_topics: list[DeferredTopic] = []
        # A cancelled response is not ordinary conversation history, but it is
        # still evidence for preventing the model from saying the same rejected
        # answer again on the correction turn.
        self._recent_interrupted_assistant: list[str] = []
        #: いまの人格。**切替の取りこぼしを見つけるための印**（Phase 7C）。
        self._persona_id: str = ""

    def add_user(self, text: str) -> None:
        self._history.append({"role": "user", "content": text})
        self._trim()

    def add_assistant(self, text: str) -> None:
        self._history.append({"role": "assistant", "content": text})
        self._trim()

    def messages(self) -> list[dict[str, str]]:
        """System Prompt + 履歴を LLM に渡す形式で返す。"""
        return [{"role": "system", "content": self._system}, *self._history]

    def messages_for_autonomous_turn(self, instruction: str) -> list[dict[str, str]]:
        """Build one initiative turn without fabricating a human utterance.

        The internal instruction is visible only to the current generation.
        The spoken result must be committed separately with ``add_assistant``;
        then the next real user turn follows the words the user actually heard.
        """
        return [
            *self.messages(),
            {"role": "user", "content": str(instruction or "")},
        ]

    def messages_for_turn(self, user_text: str) -> tuple[list[dict[str, str]], bool]:
        """保留話題を含めた、そのターン専用のメッセージを返す。"""
        messages = self.messages()
        if not self._deferred_topics:
            return messages, False
        from neuro_voice.dialogue.repair import analyze_conversation_repair

        repair = analyze_conversation_repair(user_text)
        if repair.supersedes_previous:
            # The interrupted output was based on the interpretation the user
            # is correcting.  Re-injecting it as a "topic to resume" makes that
            # invalid interpretation more salient than the correction.
            self.discard_deferred_topic()
            return messages, False
        topic = self._deferred_topics[-1]
        resume = self._is_resume_request(user_text)
        instruction = (
            "会話制御メモ: 直前のAIの説明は割り込みで保留されています。\n"
            f"保留話題: {topic.summary}\n"
            f"保留時点の会話: {self._format_topic(topic)}\n"
        )
        if topic.interrupted_turn is not None:
            interrupted = topic.interrupted_turn
            instruction += (
                "\n直前のあなたの発話はユーザーに途中で遮られました。\n\n"
                f"実際にユーザーへ伝わった内容:\n{interrupted.played_text}\n\n"
                f"生成済みだが、まだ伝えていない内容:\n{interrupted.unplayed_text}\n\n"
                "まずユーザーの新しい発話へ答えてください。未発話内容がまだ重要であれば、"
                "その後に自然に再開してください。すでに伝えた内容を最初から繰り返さないでください。\n"
            )
        if resume:
            instruction += "今回の発話は保留話題の続きを求めています。自然に続きを話してください。"
        else:
            instruction += (
                "今回のユーザー発話を最優先で答えてください。答えた後、自然な場合だけ"
                "保留話題へ戻れることを短く示してください。"
            )
        messages.insert(1, {"role": "system", "content": instruction})
        return messages, resume

    def hold_current_topic(self, interrupted_turn: InterruptedTurn | None = None) -> DeferredTopic | None:
        """現在の会話を割り込み前の話題として保存する。"""
        if not self._history:
            return None
        snapshot = [dict(item) for item in self._history[-4:]]
        last_user = next((item["content"] for item in reversed(snapshot)
                          if item["role"] == "user"), "直前の話題")
        summary = last_user.replace("\n", " ").strip()[:80] or "直前の話題"
        topic = DeferredTopic(summary=summary, messages=snapshot, interrupted_turn=interrupted_turn)
        self._deferred_topics.append(topic)
        del self._deferred_topics[:-3]  # 古い保留は最大3件まで
        return topic

    def complete_deferred_topic(self) -> None:
        if self._deferred_topics:
            self._deferred_topics.pop()

    def discard_deferred_topic(self) -> None:
        """Invalidate the latest interrupted answer while retaining echo evidence."""
        if not self._deferred_topics:
            return
        topic = self._deferred_topics.pop()
        interrupted = topic.interrupted_turn
        if interrupted is not None and interrupted.full_assistant_text.strip():
            self._recent_interrupted_assistant.append(
                interrupted.full_assistant_text.strip(),
            )
            del self._recent_interrupted_assistant[:-3]

    def recent_assistant_texts(self, limit: int = 4) -> list[str]:
        """Return heard and interrupted assistant output for mechanical guards."""
        values = [
            str(item.get("content") or "").strip()
            for item in self._history
            if item.get("role") == "assistant" and str(item.get("content") or "").strip()
        ]
        for topic in self._deferred_topics:
            interrupted = topic.interrupted_turn
            if interrupted is not None and interrupted.full_assistant_text.strip():
                values.append(interrupted.full_assistant_text.strip())
        values.extend(self._recent_interrupted_assistant)
        deduped = list(dict.fromkeys(item for item in values if item))
        return deduped[-max(1, int(limit)):]

    @property
    def deferred_topic(self) -> DeferredTopic | None:
        return self._deferred_topics[-1] if self._deferred_topics else None

    def reset(self) -> None:
        self._history.clear()
        self._deferred_topics.clear()
        self._recent_interrupted_assistant.clear()

    def switch_persona(self, system_prompt: str, *,
                       persona_id: str = "") -> dict[str, int]:
        """ペルソナが変わった。**旧ペルソナの発話を引き継がない。**

        `set_system_prompt()` だけを呼んでいたのが、実機で見た漏れの
        直接の原因だった——system prompt は新しくなるのに、
        `_history` に旧ペルソナの AI 発話が残ったまま次のターンへ
        渡っていた。モデルから見れば「自分が直前にそう喋った」ので、
        口調も知識もそのまま続く。

        **履歴を捨てる。** 共有してよい情報は、明示的な共有スコープの
        記憶から引き直す（`persona_scope.SHARED_SCOPES`）。ここで
        「これは共通の話題だから残そう」と選り分けると、その判断の
        根拠が旧ペルソナの文脈になってしまう。
        """
        dropped = {
            "history": len(self._history),
            "deferred_topics": len(self._deferred_topics),
            "interrupted": len(self._recent_interrupted_assistant),
        }
        self._system = system_prompt
        self._history.clear()
        self._deferred_topics.clear()
        self._recent_interrupted_assistant.clear()
        self._persona_id = str(persona_id or "")
        return dropped

    @property
    def persona_id(self) -> str:
        return getattr(self, "_persona_id", "")

    def set_system_prompt(self, system_prompt: str) -> None:
        """System Prompt (ペルソナ) を実行時に差し替える。"""
        self._system = system_prompt

    def set_max_turns(self, max_turns: int) -> None:
        """履歴として保持する往復数を変更する。"""
        self._max_turns = max(1, int(max_turns))
        self._trim()

    def _trim(self) -> None:
        limit = self._max_turns * 2
        if len(self._history) > limit:
            del self._history[: len(self._history) - limit]

    @staticmethod
    def _is_resume_request(text: str) -> bool:
        compact = text.replace(" ", "").replace("　", "")
        return any(phrase in compact for phrase in (
            "続き", "さっきの話", "元の話", "戻って", "続きを聞かせ", "続けて",
        ))

    @staticmethod
    def _format_topic(topic: DeferredTopic) -> str:
        return " / ".join(
            f"{'ユーザー' if item['role'] == 'user' else 'AI'}: {item['content'][:160]}"
            for item in topic.messages
        )
