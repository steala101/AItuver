# AI開発プロトコル

- Version: 1.0
- Applies To: Codex、Claude Opus、人間、将来の開発エージェント

本書は「誰が優秀か」ではなく、「現在の担当、範囲、書き込み権限、根拠、検証」を揃えるための実務手順である。すべての変更は [PROJECT_CONSTITUTION.md](PROJECT_CONSTITUTION.md) に従う。

## 1. 同時編集禁止

一つの作業treeへ同時に書き込むagentは一つだけとする。別agentはread-only reviewに限定する。

推奨順:

1. taskごとのGit worktree。
2. taskごとのbranch。
3. 上記を使えない暫定期間のみ、所有者承認済み`/.ai-work-lock.json`。

lock案:

```json
{
  "agent": "codex",
  "task": "short-task-id",
  "started_at": "ISO-8601",
  "status": "active",
  "planned_files": ["path/to/file.py"]
}
```

調査時点では実働ルートが有効なGit repoではないため、lockを勝手に導入しない。まず正本とGit運用を所有者に確認する。

## 2. 作業開始

必ず次を行う。

1. `PROJECT_CONSTITUTION.md`を読む。
2. `SHARED_CHANGELOG.md`の最新項目と、関連するhandoffを読む。
3. `CURRENT_ARCHITECTURE.md`を読む。
4. `CODEMAP.md`で関連ownerと横断影響を確認する。
5. `DECISION_LOG.md`と関連ADRを読む。
6. 最新handoffを読む。
7. project rootと実行entry pointを確認する。
8. Git branch、status、直近commit、nested repoを確認する。
9. 既存変更、backup、lock、別processの起動を確認する。
10. `.env`、data、audio、image、log、modelを出力対象から除外する。
11. 作業範囲、予定ファイル、変更しないもの、riskを宣言する。

Gitが壊れている、正本が不明、別agentが書き込み中、scope外の変更が重なる場合は実装を開始せず、事実を報告する。ユーザーの既存変更をreset、checkout、削除しない。

## 3. 調査

ファイル名やコメントだけで断定せず、entry pointから呼び出しを追う。

最低限確認:

- factoryがどのbackendを選ぶか。
- feature flagのdefaultと実値。
- ownerが誰で、誰が直接更新するか。
- task、queue、executorを誰が作り、誰が停止するか。
- timeout、cancel、stale result判定。
- localとDiscordの両経路。
- persona、group privacy、data migration。
- GUI、Remote Launcher、status APIへの影響。
- テストで使われる経路と本番entry pointから到達する経路の違い。

調査結果は`CONFIRMED`、`LIKELY`、`UNCERTAIN`、`OUTDATED`、`CONFLICTING`で表す。推測を実装済みと報告しない。

## 4. 実装前報告

次の形式で短く報告する。

```text
現状:
再現条件:
根本原因の仮説:
根拠ファイル/関数:
変更案:
変更しないもの:
影響するstate owner:
async/cancelへの影響:
privacy/data migration:
性能影響:
テスト計画:
rollback:
```

根本原因が未確定でも、安全なinstrumentationで切り分けできる場合は進めてよい。ただしログ追加だけを修正完了としない。

## 5. 実装原則

- 一つのdiffへ一つの目的。
- 無関係なformat、rename、dependency updateを混ぜない。
- stateはowner API経由で更新する。
- local/Discordの共通意味論は共有層へ置く。
- LLMへdeterministic validationを丸投げしない。
- prompt変更でrace、ACL、state corruptionを隠さない。
- feature flagを使い、旧経路との同時稼働を防ぐ。
- task ownerがcancel/await/exception retrievalを行う。
- response/session/state versionをasync境界で検査する。
- queueに上限とoverflow policyを持たせる。
- secret、raw audio、frame、private transcriptを通常ログへ出さない。
- DB schema変更はversion、migration、backup、rollbackを揃える。
- optional dependency失敗は機能単位で隔離し、会話全体を沈黙させない。
- ユーザーへ見せない内部tag、thought、planner、criticをsurface/TTSへ流さない。

## 6. ファイル編集

編集前に対象部分を読み、existing diffを確認する。ユーザー変更と重なる場合は最小hunkにする。生成物、models、data、logs、`.env`を編集しない。

大規模classを分割するときは、次の順を守る。

1. characterization testを追加。
2. interfaceを定義。
3. 挙動を変えずに一責任だけ抽出。
4. old call siteとnew call siteを比較。
5. local/Discordを一経路ずつ移す。
6. legacy flagを設ける。
7. long-runningとcancel test後に旧実装を停止。

一括rewriteは禁止する。

## 7. テスト

[TEST_AND_ACCEPTANCE_POLICY.md](TEST_AND_ACCEPTANCE_POLICY.md)から変更種別に必要なtest matrixを選ぶ。

推奨順:

1. syntax/import。
2. 変更moduleのunit。
3. state machine/cancellation。
4. affected integration。
5. full regression。
6. 実機manual。
7. latency/resource。
8. long-running。

環境不足で実行できないtestは、未実施理由と必要環境を明記する。「テストが存在する」を「通った」と書かない。

## 8. Review

別agentのreviewは、前agentの説明を鵜呑みにしない。

- actual diff。
- new/changed tests。
- owner violation。
- error path。
- stale/cancel path。
- group privacy。
- local/Discord parity。
- config migration。
- UI/status/rollback。
- logsにsecretがないこと。

問題を指摘するだけのreviewは外部stateを変更しない。修正を依頼された場合のみpatchする。

## 9. 作業終了

次を残す。

```text
担当:
目的:
変更ファイル:
変更概要:
設計判断:
実行テストと結果:
未実施テスト:
性能計測:
設定変更:
DB migration:
失敗した試行:
残課題:
rollback:
次のagentへの注意:
関連する憲法/ADR:
```

保存先は`docs/handoffs/YYYY-MM-DD_HHMM_<agent>_<task>.md`。テンプレートは [handoffs/TEMPLATE.md](handoffs/TEMPLATE.md)。

ファイルを一つでも変更した場合は、handoffに加えて
`docs/SHARED_CHANGELOG.md`の先頭へ必ず変更の要約を追記する。
共通台帳には変更ファイル、挙動への影響、設定・DB migration、実行したテスト、
残課題、handoffへのリンクを含める。CodexとClaude Codeのどちらが作業した場合も省略しない。
この追記が完了するまで、作業は完了扱いにしない。

Git管理下では、ユーザー承認なく既存変更をcommitしない。commitを作る場合はtask scopeだけをstageする。

## 10. 憲法・ADR・文書更新

- CURRENTが変わった: `CURRENT_ARCHITECTURE.md`と`CODEMAP.md`を更新。
- 不変条件または最上位方針を変えたい: `CONSTITUTION_AMENDMENT_PROPOSAL`を提出し、承認後に憲法更新。
- 重大で長期的な設計判断: ADRを追加。
- リスクを発見/解消: `KNOWN_RISKS_AND_DEBT.md`を更新。
- acceptance基準を変える: Test PolicyとDecision Logを更新。

過去文書を黙って書き換えて「昔からそうだった」状態を作らない。

## 11. 推奨交互サイクル

```text
設計review
→ 実装計画
→ 一agentが実装
→ 別agentがdiff review
→ 修正
→ test
→ architecture/risk/ADR更新
→ handoff
```

CodexとClaude Opusのどちらも全役割を担当できる。モデル名ではなく、現在のroleとwrite authorityを宣言する。

## 12. 緊急修正

会話不能、data corruption、secret exposure、無限再生、process終了不能などは緊急修正とする。ただし緊急でも以下は省略しない。

- reproducerまたはerror evidence。
- blast radius。
- smallest safe patch。
- targeted regression。
- rollback。
- handoff。

緊急修正で大規模refactorを始めない。暫定guardを置いた場合はrisk台帳へ恒久対応を記録する。
