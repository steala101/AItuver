# ポッポ開発エージェント共通指示

このファイルは、CodexとClaude Codeが共有するプロジェクト指示の入口です。

## 作業開始時に必ず読む

調査・設計・実装・修正・レビューを始める前に、次の順で確認してください。

1. `docs/PROJECT_CONSTITUTION.md`
2. `docs/SHARED_CHANGELOG.md` の最新項目
3. `docs/CURRENT_ARCHITECTURE.md`
4. `docs/CODEMAP.md`
5. `docs/AI_DEVELOPMENT_PROTOCOL.md`
6. `docs/TEST_AND_ACCEPTANCE_POLICY.md`
7. `docs/DECISION_LOG.md`
8. 関連ADRと最新の `docs/handoffs/*.md`

`docs/PROJECT_CONSTITUTION.md` は、ユーザーの最新の明示指示に次ぐ、リポジトリ内の最上位基準です。

## 変更時の必須同期

ファイルを一つでも変更した場合、作業を完了する前に必ず次を行ってください。

1. `docs/SHARED_CHANGELOG.md` の先頭へ変更記録を追記する。
2. `docs/handoffs/YYYY-MM-DD_HHMM_<agent>_<task>.md` に詳細な引継ぎを残す。
3. CURRENTが変わった場合は `docs/CURRENT_ARCHITECTURE.md` と `docs/CODEMAP.md` を更新する。
4. 長期的な設計判断は `docs/DECISION_LOG.md` と関連ADRへ反映する。
5. 未解決リスクは `docs/KNOWN_RISKS_AND_DEBT.md` へ反映する。

変更履歴の追記を忘れた状態を「作業完了」と報告してはいけません。
別のAIが行った変更を引き継ぐ際は、最初に共通変更台帳の最新項目とリンク先handoffを読んでください。

## 作業上の不変条件

- 憲法と現行コードが矛盾する場合は、CURRENT / TARGET / INVARIANT / TRANSITIONを分ける。
- 憲法を変える必要がある場合は `CONSTITUTION_AMENDMENT_PROPOSAL` を提示し、承認前に変更しない。
- 同じ作業ツリーを複数AIで同時編集しない。
- ユーザーの既存変更を勝手に破棄しない。
- 勝手にGit初期化、reset、commit、pushしない。
- LocalとDiscordの共通意味論を片側だけ変更して完了としない。
- 状態問題をpromptだけで隠さず、owner、世代ID、cancel、validationを確認する。
- 秘密情報、個人情報、raw audio、raw imageを文書やログへ複製しない。

