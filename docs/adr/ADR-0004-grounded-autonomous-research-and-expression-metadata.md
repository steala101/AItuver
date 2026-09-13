# ADR-0004: 自律研究をgrounded producerへ限定し、表現metadataを本文外に置く

- Status: Accepted
- Date: 2026-07-30

## Context

自律研究には永続queue、gate、worker、Evidence/Knowledge/Reflectionが存在したが、
課題を作る通常経路は明示的な後調べ依頼だけだった。Heartbeatはqueue保守しかしないため、
「自分の興味から裏で調べる」という外部挙動は発生しなかった。履歴APIもGUIへ接続されていなかった。

また、voice/avatar共通の`UnifiedExpressionPlan`を導入した後も、persona promptは
LLM本文へ感情・styleタグを出すよう求めていた。未知labelは旧parserを抜け、UIとTTSへ漏れた。

## Decision

自律研究の課題producerを次に限定する。

1. active personaへ明記された公開の興味
2. 一対一会話の事実質問へ実際に「分からない」と答えた知識gap
3. ユーザーが明示した後調べ依頼

Heartbeat/LLMに自由なtopicを発明させない。候補は起動遅延、cooldown、quota、重複、
privacy、risk gateを通し、reason code付きでpersona別Research storeへ保存する。
group transcript、関係・感情・知覚・能力質問は自動知識gapへしない。GUIにはサニタイズ済み履歴だけを表示する。
自発発話ON/OFFは音声発話policyとして扱い、Heartbeat schedulerの生存とは分離する。OFF時も研究queue保守とgrounded producerは動かし、発話評価だけを見送る。

voice/avatar表現metadataの正本は`UnifiedExpressionPlan`とする。LLM本文へtagを要求しない。
legacyモデルが型付きtagを返した場合は値が未知でも本文/TTSから除去する。

## Consequences

- 自律研究が実際に起動する入口と、ユーザーが確認できる履歴を持つ。
- 自発発話を望まない設定でも、裏の研究機能は失われない。
- 沈黙を理由にtopicを無制限生成せず、検索理由を監査できる。
- 単一sourceは引き続き暫定Knowledgeで、EvidenceとFactを混ぜない。
- persona興味が広すぎる場合は結果も一般的になるため、実会話と実検索で調整が必要。
- 表現制御と発話本文が競合せず、tag漏出をlegacy互換guardで止められる。

## Related Files

- `neuro_voice/research/service.py`
- `neuro_voice/research/store.py`
- `neuro_voice/mind/mind.py`
- `neuro_voice/pipeline.py`
- `neuro_voice/discord_bridge/bot.py`
- `neuro_voice/ui/assets/index.html`
- `neuro_voice/memory/persona.py`
- `neuro_voice/dialogue/response_director.py`
- `neuro_voice/dialogue/expression_plan.py`
- `neuro_voice/utils/emotion.py`
- `neuro_voice/utils/textseg.py`
