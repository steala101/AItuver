# AI作業Handoff

- 担当AI: Codex
- Role: design / implementation
- Task: Codex・Claude共通変更台帳の導入
- Finished At: 2026-07-26 JST
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: Git管理外のため該当なし

## 目的

CodexとClaude Codeのどちらが修正しても、変更内容を一つの共通資料から確認できるようにする。

## 変更

| File | Change | Why |
|---|---|---|
| `AGENTS.md` | 共通の読込順序と同期手順を追加 | Codexのプロジェクト指示入口にするため |
| `CLAUDE.md` | `AGENTS.md`のimportと同期手順を追加 | Claude Codeにも同じ規則を適用するため |
| `docs/SHARED_CHANGELOG.md` | 逆時系列の共通変更台帳を追加 | 両AI間の変更把握を一本化するため |
| `docs/AI_DEVELOPMENT_PROTOCOL.md` | 開始時の読込と終了時の追記を必須化 | 運用上の記録漏れを防ぐため |

## テスト

- `AGENTS.md`、`CLAUDE.md`、共通変更台帳、handoffの相互参照を確認。
- 実行コード、設定、DBには変更なし。

## 残課題

- 新しいCodexタスクで `AGENTS.md` と共通台帳が読み込まれることを確認する。
- 新しいClaude Codeセッションで `/memory` を実行し、`CLAUDE.md` と `AGENTS.md` の読込を確認する。
- 現在の実働rootはGitリポジトリとして認識されないため、Git運用は別途整理が必要。

## Rollback

今回追加した `AGENTS.md`、`CLAUDE.md`、`docs/SHARED_CHANGELOG.md`、
handoffを削除し、`docs/AI_DEVELOPMENT_PROTOCOL.md`の追加部分を戻す。

## 次の担当への注意

今後ファイルを変更した作業は、詳細handoffだけでなく
`docs/SHARED_CHANGELOG.md`の先頭にも必ず要約を追加する。

