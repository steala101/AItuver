# 会話品質再レビューとSol向け実装計画

- Date: 2026-09-07 08:57 JST
- 担当: Codex、設計・調査・文書のみ。次のコード担当はユーザー指定のSol。
- 指示: トークン消費を抑えて計画を練り直す。作業中の質問なし。後日確認事項は結果へまとめる。
- 正本: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`

## 成果物

- [Sol向け実装指示書](../superpowers/plans/2026-09-07-conversation-quality-sol.md): P0〜P7、初回P0〜P2、対象ファイル、producer/consumer、提案型、testコマンド、人工corpus、故障注入、品質/遅延gate、報告形式、後日確認7事項。
- [ADR-0005](../adr/ADR-0005-conversation-quality-recovery.md): PROPOSED。前回提案の訂正、確認した証拠、選択肢、owner、採用と保留の境界。
- SHARED_CHANGELOG / DECISION_LOG D-042 / KNOWN_RISKS R-048 / _NEXT_SESSION入口を同期。
- CURRENT/CODEMAPは実装が変わっていないため未変更。古い文書の実機状態を新しい合格証拠として使わない。

## 確認結果

ネットワーク・LLM・DBなしで `.venv-test\Scripts\python.exe -B -c` から既存純粋関数を実行。

```text
compound search request detected: false
compound should_search: false
simple search request detected: true
compound query correctly cleaned: false
synthetic body 300 chars + content 900 chars → selected excerpt 160 chars from body
```

既存コードから、一般queryへ攻略語を追加すること、queryを受け取らないPresenter、固定品質点、連続感情の既存実装も確認。これらの情報を根拠に計画化した。最新プロセスのASR・config・実機原因全体は今回未監査。

## 設計上の注意

1. 経路分散は負債だが、自然さ低下の主原因と未計測のまま断定しない。
2. 新しい感情/人格/Memoryシステムを足さず、既存計画から発話・TTSへの反映を確認する。
3. 検索判定、query、根拠選択、失敗時応答を第一便で直す。P2に最小限の一時Evidence保持を含め、follow-up受入との依存を揃えた。
4. 自然な検索LLM要約はD-041変更を伴うため、現行抽出経路と分離して承認待ち。source IDだけで真偽検証済みとしない。
5. 音楽・最新入力・退出のユーザー合格を回帰保護する。今回そこを大改造しない。
6. 全pytest過去2974/1 skipは履歴。今回の結果として報告しない。

## 検証と非変更

- 文書検査を実行済み（exit 0）: P0〜P7の8段階、corpus 27件、リンク切れ0、未記入placeholder 0、Markdown fence対応。参照した既存実装path 27件の存在を確認。
- 自己レビューでP2のfollow-up受入がP4のcache実装に依存していた点を修正し、最小Evidence保持をP2へ移した。評価器の提案型、各段階のtestコマンド、検索の全体deadline、関連判定の限界を補った。
- 作成したのは文書のみ。アプリコード・本番Memory・persona・config・過去Trace・実行用venvは編集なし。
- git statusは現在も「not a git repository」。Git操作なし。rollbackは今回の新規文書と追加段落だけを対象にし、旧本文を保持する。
- 全pytest、実モデル評価、外部検索、実機会話、Discord操作は実行なし。

## 次の担当

Solは指示書とADRを読み、実装開始の依頼が来たらP0〜P2から着手する。ユーザーの追加会話を求める前にfake統合と関連全suiteを実行する。確認事項の未回答はオフライン検証の妨げにせず、費用・新モデル・声変更・永続書込み・本番権限拡大だけを待ちにする。
