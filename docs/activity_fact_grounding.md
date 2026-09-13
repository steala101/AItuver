# 活動の事実固定基盤

会話履歴は表現用であり、遊びや共同作業の正本ではありません。`fact_grounding` を有効にすると、最終確定した発話だけを活動イベント台帳へ append-only で保存し、そこから現在状態を再構築します。

## 処理の順序

```text
STT final + 話者確定
  -> ActivityEventLedger
  -> Canonical Activity State
  -> LLM（状態を参照して提案）
  -> Validator
  -> Commit
  -> TTS
```

STT partial、LLMの解釈、感情、会話履歴は canonical fact を上書きできません。状態バージョンが変わった古い提案、不正な提案、読みが不明な提案は TTS に送られません。

## 初期Protocol: 日本語しりとり

`しりとりしよう` で開始します。手番、使用済み語、前の語、必要な仮名、ルールはペルソナ別の `data/activities_<persona>.json` に保存されます。ポッポの単語はLLMが選びますが、引用したひらがな一語を提案として検証し、重複・先頭不一致・`ん`終端・古い状態を拒否します。

AIの手は `Proposal → Normalize → Validate → Commit → Speak` の一つのトランザクションです。カタカナ、引用符、末尾の句読点を吸収した `comparison_key` で、ユーザーとポッポ共通の使用済み集合を照合します。不正候補は発話もcommitもせず、その手番の除外リストへ記録します。再提案でも同じ候補・不正候補なら活動を安全に停止します。

すでに誤ったAI手が旧データとしてcommitされていた場合は、重複訂正を受けて `AI_MOVE_INVALIDATED` をappendし、有効なイベントだけから直前の正常状態へreplayします。古いイベントを削除することはありません。

訂正らしい発話は即興で盤面を書き換えず、`CORRECTION_REQUESTED` を記録して台帳から再構築します。読みが曖昧な訂正は一語の確認を求めます。

## 設定

`config/config.yaml` の `fact_grounding.enabled` で無効化できます。無効時は、旧来の会話側しりとり補助へフォールバックします。現在は安全のため同時に有効な活動は1つです。

## 拡張方法

新しい遊びは任意コードを実行するのではなく、固定の protocol、許可アクション、状態スキーマ、不変条件、検証規則を追加します。複雑な盤面計算や外部情報が必要なゲームは、専用の検証ツールを追加して同じ Proposal → Validate → Commit 経路へ接続します。
