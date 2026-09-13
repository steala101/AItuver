# AI作業Handoff

- 担当AI: Codex
- Role: investigation / implementation / test
- Task: 実GUI 8文受入で判明した継続誤検知・相槌長文化・過去回答自己模倣の修正
- Started At: 2026-07-29
- Finished At: 2026-07-29 23:15 JST
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: Git metadataは存在するが、この実働rootをGit repositoryとして解決できない
- Start Commit: 不明
- End Commit: commitしていない
- Work Lock: なし

## 目的

`docs/CONVERSATION_AB_SCRIPT.md`の実GUI実行で、長さ計画は変化したのに
短い相槌が長文応答となり、冒頭ハッシュが以前と8/8一致した原因を修正する。

## 作業開始時の状態

- Git status: `fatal: not a git repository`のため取得不能
- 既存変更: 共通台帳の2026-07-29 23:59項目までを引継ぎ
- 起動中process: 変更していない
- 読んだ憲法/ADR: PROJECT_CONSTITUTION、CURRENT_ARCHITECTURE、CODEMAP、AI_DEVELOPMENT_PROTOCOL、TEST_AND_ACCEPTANCE_POLICY、DECISION_LOG、最新handoff

## 調査結果

- 根本原因:
  1. `intent_plan._CONTINUING`の裸の`話してて`が「話しててもAI感が強い」に部分一致し、一般発言をBehaviorDirective候補にした。
  2. LLMが`ONGOING_DIRECTIVE / DIALOGUE / needs_user_input`を返し、`directive_waiting_for_user`が短い相槌のTurn Closureを無条件に迂回した。
  3. 通常semantic transcript想起が、同じユーザー発話に対する過去assistant回答を複数件system contextへ再注入した。
- 根拠ファイル/関数: `continuation_candidate`、`Mind.directive_waiting_for_user`、`Mind.build_context`のtranscript block
- state owner: BehaviorDirectiveはContinuationController、想起contextはMind、終了判断はConversationOrchestrator/TurnClosurePolicy
- async/queue/cancel: 新規task・queue・cancel変更なし。誤ったIntent-to-Plan taskを開始前に防ぐ。
- privacy/data: DBを読替・削除しない。raw/private transcriptを文書・ログへ複製していない。

## 変更

| File | Change | Why |
|---|---|---|
| `neuro_voice/dialogue/intent_plan.py` | 裸の「話してて／何か話して」を依頼文末だけに限定 | 通常叙述の継続誤検知を防ぐ |
| `neuro_voice/mind/mind.py` | 真の手番制interactionだけwaitingを公開 | 一般DIALOGUEによるTurn Closure迂回を防ぐ |
| `neuro_voice/mind/mind.py` | transcript重複排除、通常想起から過去assistant文面を除外 | 記憶を思考材料にし、表面コピーを防ぐ |
| `tests/test_behavior_directive.py` | 実例と明示依頼の正負回帰 | 指名精度を固定 |
| `tests/test_silent_turn_directive.py` | DIALOGUE/NARRATION/CO_THINKINGの待機契約 | 真のActivityを壊さず迂回を閉じる |
| `tests/test_transcript_memory.py` | 重複排除と明示/通常想起の表示契約 | 過去回答再注入を固定 |
| docs | CURRENT/CODEMAP/Decision/Risk/Changelog同期 | 次担当との共通正本 |

## 変更しなかったもの

- temperature、seed、モデル、persona prompt、recent_reply_avoidance既定OFF
- transcript DBの既存データ
- TRPG/CO_THINKING/Activityの手番意味論
- Local/Discordのsurface別応答実装

## 設計判断

記憶は現在の判断に影響させるが、通常想起で過去assistantの文章をfew-shot例のように
再投入しない。履歴そのものを尋ねられた時だけ、事実照合として過去回答を使う。

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| 最終対象5ファイル | 110 passed | directive/closure/transcript/kernel |
| 関連統合10ファイル | 238 passed | Kernel/Runtime/Narration/Generation含む |
| `pytest tests -q` | 1086 passed / 2 skipped | 最終58.07s、skipはpyflakes未導入 |

- 未実施: 修正後の実GUI 8文再走
- 未実施理由: アプリ再起動とユーザーの対話入力が必要
- Manual: 変更前の実GUIログを診断に使用。変更後は未実施。
- Performance p50/p95: 未計測。誤った補助LLM呼び出しと0.7〜1.2k tokenのDirective blockを除くため改善見込みだが、実測まで断定しない。

## 設定・Migration

- Config: 変更なし
- Environment: `.venv` launcherはbase interpreterを起動できなかったため、base Pythonへ`.venv\Lib\site-packages`をPYTHONPATHとしてテスト
- DB schema: 変更なし
- Data migration: なし
- Backup: なし

## 失敗した試行

- base Python単体はpytest未導入。
- `.venv\Scripts\python.exe`はWindows launcherがprocessを作成できなかった。
- base Python + venv site-packagesで全テストを実行した。

## 残課題

1. アプリ再起動後に同じ8文を再走し、#2/#3/#8が沈黙または短い直接応答になるか確認。
2. 1番で`Directive created`が出ないこと、初回TTFTが以前の12.8秒から下がるか確認。
3. opening hashと実音声長を再集計。単調さが残れば、今回と別の原因を新しい実測から追う。
4. 検索・視覚込みMind最大contextは未計測。

## Rollback

コード3ファイルの今回hunkと対応テストを戻す。DB/config migrationはない。

## 次に触るべきファイル

- `logs/conversation_metrics.jsonl`
- `logs/neuro_voice.log`
- `docs/CONVERSATION_AB_SCRIPT.md`

## 触らない方がよい箇所

- 実測なしのpersona prompt追加
- `recent_reply_avoidance`の既定ON
- 過去transcriptの削除

## 憲法・ADRへの影響

- Constitution amendment required: なし
- ADR added/updated: Decision Log D-027
- Architecture/Codemap updated: はい

## 秘密・個人情報確認

- `.env`値を記録していない: はい
- raw audio/imageを添付していない: はい
- private transcriptを複製していない: はい。既存の公開受入台本の最小例だけを使用。

## 次の担当への一文

次はコードを増やさず、再起動後の同じ8文でDirective作成数、沈黙3件、TTFT、opening hashを先に測ること。
