# AI作業Handoff

- 担当AI: Codex GPT-5.6
- Role: design / architecture audit / documentation
- Task: ポッポ開発憲法・共通設計基準
- Started At: 2026-07-26
- Finished At: 2026-07-26
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: 実働rootはGit repoとして認識されない
- Start Commit: UNKNOWN
- End Commit: N/A
- Work Lock: なし

## 目的

現行コードを調査し、複数AIと人間が同じ基準で変更するための憲法、architecture、codemap、protocol、test policy、risk、decision log、handoff templateを作成する。コード変更は行わない。

## 作業開始時の状態

- Git status: root `.git`は空。nested `AItuber/`のみ`main`、clean、`52469d4`。
- 既存変更: rootでGit diffを取得不能。
- 起動中process: 調査対象外。プロセス停止などの外部変更は実施していない。
- 読んだ文書: README、既存`docs/*.md`、添付指示書。

## 調査結果

- Root entry: `run.py → neuro_voice.app`。
- Local owner: `VoicePipeline`。Discord owner: `DiscordBridge`。意味状態は`Mind`配下。
- Local/Discordにresponse/autonomy/search/TTS制御の重複がある。
- response IDとPlayedTextTrackerによるstale音声guardは両経路に存在する。
- Direct DAVEがDiscord primary、loopbackはfallback。
- Activityはcanonical proposal/validate/commit。
- ResearchはEvidence/Knowledge/Reflectionを分離しsingle workerで処理。
- `config.yaml`の`privacy`は重複し、後のmappingが前を上書きする。
- Memory schemaはowner/audience/privacy metadataを完全には保持しない。
- global Pythonなし、`.venv`は存在しないPython 3.11を参照し、pytest不能。

## 変更

| File | Change | Why |
|---|---|---|
| `docs/PROJECT_CONSTITUTION.md` | 新規 | 最上位ルール |
| `docs/CURRENT_ARCHITECTURE.md` | 新規 | CURRENTの実態 |
| `docs/CODEMAP.md` | 新規 | owner/変更地図 |
| `docs/AI_DEVELOPMENT_PROTOCOL.md` | 新規 | 複数AIの作業手順 |
| `docs/TEST_AND_ACCEPTANCE_POLICY.md` | 新規 | 完了条件 |
| `docs/KNOWN_RISKS_AND_DEBT.md` | 新規 | P0-P2 risk |
| `docs/DECISION_LOG.md` | 新規 | 設計決定 |
| `docs/adr/ADR-0001-single-main-llm.md` | 新規 | 単一LLM |
| `docs/adr/ADR-0002-canonical-state-and-generation-guards.md` | 新規 | canonical state/stale guard |
| `docs/handoffs/TEMPLATE.md` | 新規 | 標準handoff |
| 本ファイル | 新規 | 今回のhandoff |
| `README.md` | 冒頭リンク追加 | 今後の開発者が憲法を発見できる入口 |

## 変更しなかったもの

- Python/JavaScript実装。
- config、dependencies、DB、data、logs、models、`.env`。
- Git初期化/commit。
- 既存README本文と旧設計文書（READMEは冒頭の参照リンクだけ追加）。

## 設計判断

- CURRENT/TARGET/INVARIANT/TRANSITIONを分離。
- ownerとgeneration guardを最上位不変条件にした。
- single main LLMは維持し、補助呼び出しをboundedに扱う。
- 一つのtreeへの同時書き込みを禁止する草案とした。
- root Gitの復旧とPython baselineをP0とした。

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| Markdown成果物存在 | PASS | `docs/` |
| Markdown相対link target | PASS | 11成果物、broken link 0 |
| 機密値非記載 | PASS | token/API key形式の静的検査で一致なし |
| pytest | NOT RUN | Python runtime unavailable |

- Manual: 文書の章・Mermaid sourceを目視確認。
- Performance: 文書作業のため対象外。

## 設定・Migration

- Config: 変更なし。
- Environment: 変更なし。
- DB schema: 変更なし。
- Data migration: なし。
- Backup: 文書は新規file。root GitがないためGit rollback不可。

## 失敗した試行

- root `git status`: repositoryとして認識されず。
- global Python/`.venv` pytest: interpreter unavailable。

## 残課題

1. 所有者が憲法草案を承認し1.0へする。
2. 正本Git/remoteを確定する。
3. Python環境を復旧しtest baselineを保存する。
4. `privacy` duplicateとMemory ACLをコード修正する。
5. Local/Discord共通Response RuntimeのADR/移行計画を作る。

## Rollback

今回追加した`docs/`成果物のみを削除すればよい。実装・設定・dataへの変更はない。削除は所有者承認後に行う。

## 次に触るべきファイル

- `config/config.yaml`
- `neuro_voice/utils/config.py`
- `neuro_voice/privacy.py`
- `neuro_voice/mind/store.py`
- `neuro_voice/pipeline.py`
- `neuro_voice/discord_bridge/bot.py`

## 触らない方がよい箇所

- `data/`のpersona DB/JSON。
- nested `AItuber/`を正本確認前にmerge/deleteしない。
- `.env`。

## 憲法・ADRへの影響

- Constitution amendment required: 所有者承認でdraft→1.0。
- ADR added: 0001、0002。
- Architecture/Codemap updated: 新規作成。

## 秘密・個人情報確認

- `.env`値を記録していない: YES
- raw audio/imageを添付していない: YES
- private transcriptを複製していない: YES

## 次の担当への一文

実装へ入る前に、root Gitの正本確認とPython環境復旧を優先し、`privacy`重複を独立した小さな変更として扱うこと。
