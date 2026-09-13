# AI作業Handoff

- 担当AI: Codex
- Role: design / documentation
- Task: ポッポ開発憲法の趣旨明確化とversion 1.1.0への改正
- Finished At: 2026-07-26 JST
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: 実働ルート直接（Gitリポジトリとして認識されない）
- User Approval: 2026-07-26「では憲法を改正して下さい」

## 目的

GPT Liveのように意図・文脈・会話の流れを考える音声AIと、
ネウロ様・Evilのように人間らしく固有で成長するキャラクターを目指すという
プロジェクト所有者の前提を、最上位の開発基準として誤解なく読める形にする。

## 変更

| File | Change | Why |
|---|---|---|
| `docs/PROJECT_CONSTITUTION.md` | 1.1.0 / ACTIVEへ更新。第1条へ会話理解、固有人格、参照品質、思考と表現を追加 | 目標が既存条項へ分散し、実装評価で一問一答へ戻る余地があったため |
| `docs/DECISION_LOG.md` | D-013をACCEPTED化しD-018を追加 | 所有者承認と改正理由を追跡可能にするため |
| `docs/SHARED_CHANGELOG.md` | 改正要約を追加 | CodexとClaude Codeの双方へ改正を引き継ぐため |

## 主要な設計判断

1. GPT Liveは低遅延、ターンテイキング、割り込み、文脈理解の品質参照であり、同一APIや非公開実装の再現を意味しない。
2. ネウロ様・Evilは固有反応、存在感、経験による変化の品質参照であり、人格・声・外見・学習データの複製を意味しない。
3. 人間らしさのために知覚、記憶、感情、経験を捏造しない既存不変条件を維持する。
4. 内部計画は応答品質へ利用するが、生のchain-of-thoughtや内部tagをsurface/TTSへ出さない。

## テスト

- `PROJECT_CONSTITUTION.md`のversion、status、改正履歴を確認。
- 第1条に「意図」「指示対象」「会話状態」「一問一答」「経験」「GPT Live」「ネウロ様」「Evil」「内部thought」が存在することを確認。
- `DECISION_LOG.md`のD-013とD-018、`SHARED_CHANGELOG.md`、本handoffの相互参照を確認。
- 実行コード、設定、DBは変更していないためruntime testは対象外。

## 残課題

- 現行実装が憲法1.1.0を満たしていることを意味しない。
- 今後、会話評価用replay corpusと、参照解決・話題継続・人格の経験依存を測る受け入れテストが必要。

## Rollback

憲法を1.0.0-draftへ戻す場合は、所有者の新たな承認を得て、
第1条の追加、改正履歴、D-018をrevertし、共通変更台帳へ記録する。

## 次の担当への注意

会話品質を「質問へ正しく答えたか」だけで評価しない。
ユーザーの意図、指示対象、話題状態、保留内容、経験による判断変化まで確認する。

