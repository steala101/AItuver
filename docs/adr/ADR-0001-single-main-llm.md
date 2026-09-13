# ADR-0001: 単一メインLLMを維持する

- Status: Accepted as project invariant; implementation has bounded auxiliary calls
- Date: 2026-07-26

## Context

音声会話では低遅延とGPU予算が重要である。partial STT、相槌、自律判断、検索計画、視覚応答ごとに別の常駐モデルを持つと、VRAM競合、文脈不一致、人格分裂、キャンセル困難が生じる。現行`build_llm()`は一つの選択backendを生成し、`VoicePipeline`と`DiscordBridge`は共有されたLLMを利用する。一方、検索計画や行動計画など、同じbackendへの補助呼び出しは存在する。

## Decision

一つのメインLLMを人格と会話の主体とする。補助推論は同じ主体の限定的処理として、目的、優先度、予算、timeout、cancel ownerを明示する。partial STTとbackchannel専用の常駐LLMは追加しない。前景応答を常に優先する。

## Consequences

- 人格と文脈の一貫性を保ちやすい。
- VRAM使用量とモデルlifecycleを管理しやすい。
- 補助呼び出し間のGPU schedulingが必要になる。
- 単一モデルの能力・速度が全体品質へ強く影響する。

## Alternatives

- 複数の専門LLMを常駐: 現行ハードウェアと低遅延要件では不採用。
- 規則だけで補助判断: 決定論的検証には使うが、会話意味の判断を全面代替しない。

## Related Files

- `neuro_voice/llm/factory.py`
- `neuro_voice/llm/openai_compat.py`
- `neuro_voice/pipeline.py`
- `neuro_voice/discord_bridge/bot.py`
- `config/config.yaml`
