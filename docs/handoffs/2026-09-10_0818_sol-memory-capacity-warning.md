# Stage M 本番昇格記録と容量20%警告 handoff

- Date: 2026-09-10 08:18 JST
- Agent: Sol
- Scope: Stage Mの承認後運用事実の同期と、保存volumeの最小容量警告
- Status: `CODE_VERIFIED / USER-APPROVED PRODUCTION PROMOTION COMPLETE`

## 1. 結果

canonical Memory/transcriptの`retain`契約を維持したまま、canonical DB、data、backup、sidecarが存在するvolumeの空き率をcontent-freeに診断できるようにした。既定20%未満は`warning`、probe失敗は`unknown`であり、どちらもcanonical行の自動削除、archive、write無効化を行わない。

`Mind`の初期化とpersona切替は同じStore factoryを使い、`storage_health.capacity`を共有`Mind.status()`へ載せるため、Local UIとDiscord statusは同じ意味論を読む。「こころ」画面では状態、空き率、警告閾値、測定先を表示する。本文、summary、embedding、秘密値は容量診断へ含めない。

## 2. 変更

- `neuro_voice/mind/store.py`
  - `capacity_warning_free_percent`とlogical probe pathsを追加。
  - canonical/sidecar/data/backupをexisting ancestorとdevice IDでvolume単位にまとめ、`shutil.disk_usage`を一volume一回実行。
  - `storage_health()`に`capacity.state/free_percent/warning_threshold_percent/measurement_path/volumes/probe_failures`を追加。
  - 不正な閾値は20%へ戻し、正常値は0〜100へclamp。probe例外は型名だけをlogし、UNKNOWNへ収束。
- `neuro_voice/mind/mind.py`
  - `_open_persona_store()`を単一ownerにし、初期化/切替で同じ保持policy・閾値・data/backup targetを渡す。
  - `Mind.status().memory.storage`へhealthを公開。
- `neuro_voice/ui/assets/index.html`
  - 既存「こころ」の記憶cardにcontent-free容量healthを表示。
- `tests/test_memory_retention_contract.py` / `tests/test_ui_mind_status.py`
  - 15%で既定20% warning、設定10%ならOK、probe失敗UNKNOWN、source指紋不変、自動削除0、後続write可、共有factory、UI fieldを固定。

現在の永続configへ`mind.storage.warning_free_percent`は書き込んでいない。未設定時だけ20%を使う。`memory.write_enabled`も変更していない。

## 3. RED → GREEN

新規契約4件は実装前に次のREDを確認した。

1. `MemoryStore.__init__`に容量probe引数がない。
2. `Mind._open_persona_store`がない。
3. `storage_health.capacity`がない。
4. 「こころ」に容量表示がない。

実装後は4件すべてGREEN。factory化で旧静的count testが1件REDになったため、実体の「初期化/切替が同じfactoryを2回使い、policy ownerはfactory内1箇所」を検査する形へ更新しGREENにした。

## 4. 検証

- focused: `5 passed`
- Stage M/Memory/backup/transcript/UI関連: `192 passed, 1 skipped in 45.85s`
- expected skip: Windows symlink privilege unavailable
- `python -m py_compile neuro_voice/mind/store.py neuro_voice/mind/mind.py`: success
- full: `3076 passed, 2 skipped in 124.43s`
- full expected skips: Windows symlink privilege unavailable 2件
- failed: 0

テストは`tmp_path`とfake disk usageだけを使用し、実機・外部I/O・本番dataを使っていない。

## 5. ユーザー承認後の本番昇格事実

この実装作業より前に、rootがユーザーの明示承認後、停止中（`python`/`pythonw`なし、全mind DB排他読取可）に7個の`mind_*.db`をSQLite online backupした。

- 保存先: `data/backups/memory_retention_stage_m/20260910_074937_preindex`
- 全7 DB: integrity OK
- backup前後: 件数とID・本文・summary・embedding指紋一致
- 隔離copy: schema migration＋generation 8 sidecar、raw/unit/cosine .90既知vectorを2回ずつRecall
- 隔離結果: 候補Memory80/transcript40、context5、persona/status漏れ0、rebuild 0/0、backup不変
- 本番昇格: `memory_search_state`＋triggers、必要な一部DBはreflection persona/scope、generation 8 sidecar
- 昇格後: canonical指紋不変、schema current、sidecar合計2,797,568 bytes、restart rebuild 0、known Recall成功
- 証跡: `manifest.json`、`isolated_validation.json`、`production_promotion.json`

過去handoff/changelogの`PRODUCTION_DATA_UNTOUCHED`は、その作業時点で本番へ触れていなかった正しい履歴なので書き換えていない。CURRENTとこのhandoffだけが承認後の現在状態を明示する。

## 6. 非変更対象

容量警告作業では本番DB、既存backup、既存sidecar、persona、永続config、過去Trace、raw audio/image、外部serviceへ触れていない。実機起動、Memory削除、write再開/停止、Git操作も行っていない。

## 7. 残存リスク

- 容量表示はoperatorがstatus/「こころ」を開いた時の観測で、OS通知や自動回収ではない。probe不能時はUNKNOWNとして会話を続ける。
- 確認付き自然言語削除UXは未実装。理由付きstorage primitiveとDO_NOT_STOREだけを維持し、自然言語から破壊操作を追加していない。
- 各100k＋100kのgeneration 8 buildは同一source lockを約209秒保持する。
- cosine .90 full-tierの高衝突synthetic p95 367.410msは一般semantic SLOではなく、本番実モデル分布の別測定が必要。
- 同じdirectoryへ書ける同権限の悪意ownerによる検出不能なsidecar omissionは可用性保証外。canonical ACL/status/vector再検査は維持する。
