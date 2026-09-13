# ADR-0003: 単一生成前の会話Move競争と弱い適応を採用する

- Status: Accepted
- Date: 2026-07-30

## Context

人間らしい会話には、直接答える、短く反応する、意見を言う、軽く遊ぶ、
話題を広げる、質問する、という複数の選択肢が必要である。一方、ローカルOllamaで
候補本文を複数生成すると初回音声が遅れ、単一メインLLMとリアルタイム性の不変条件を損なう。
また、会話の成功・失敗を次の選択へ戻す経路と、音声・将来avatarを同じ応答へ結び付ける
型が不足していた。

## Decision

返答本文ではなくConversation MoveをPythonで複数生成し、関連性、継続性、social fit、
novelty、直近反復、boundedな学習重みで一つ選ぶ。訂正、支援、履歴想起はhard obligationとする。
本文は選択後にメインLLMで一度だけ生成する。

次のユーザー発話は構造化Reaction signalへ変換し、直前Move重みを小幅・有界に更新する。
割り込みと沈黙だけでは更新しない。短い意味連想graphは事実正本から分離し、group由来edgeは
現在runtime sessionに限定する。

voiceと将来avatarは同じresponse IDの`UnifiedExpressionPlan`へまとめる。現状のavatar sinkは
No-opであり、配信commentもprotocolだけを用意してruntimeへ接続しない。

## Consequences

- 追加LLM呼び出しなしで返し方候補の競争を観測・テストできる。
- LocalとDiscordが同じ選択・学習・表現契約を使う。
- implicit reactionと共起edgeは弱い証拠なので、事実確定や大きな人格変更には利用できない。
- 将来avatar接続時はresponse ID、cancel、generation guardをsinkにも伝播する必要がある。

## Related Files

- `neuro_voice/dialogue/selection_kernel.py`
- `neuro_voice/dialogue/reaction_learning.py`
- `neuro_voice/dialogue/semantic_graph.py`
- `neuro_voice/dialogue/expression_plan.py`
- `neuro_voice/dialogue/audience.py`
- `neuro_voice/dialogue/conversation_planner.py`
- `neuro_voice/dialogue/intelligence.py`
- `neuro_voice/mind/mind.py`
- `neuro_voice/pipeline.py`
- `neuro_voice/discord_bridge/bot.py`
