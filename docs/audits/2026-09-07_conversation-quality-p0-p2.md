# 2026-09-07 会話品質 P0〜P2 監査

## 判定

- 範囲: P0評価、P1検索route、P2Evidence契約。`CODE_VERIFIED / HARDWARE_PENDING`。
- 本番DB、persona、永続config、過去Trace、実行用venv、DAVE mix/leaveは変更していない。
- runtime fingerprint（process/config/model/TTS）は実機を起動していないため `null / NOT_MEASURED`。

## 実行経路

| surface | final inputから配送まで | 所有・拒否点 |
|---|---|---|
| Local | `VoicePipeline` final ASR → `_cognitive_decide` → `route_search_request` → 既存Tool proposal/gate/execution → `execute_search_route` → `SearchOutcome` → `present_search_outcome` → `_speak_loop` / `TurnTracker` | 検索対象だけをToolへ渡す。BLOCKED/CLARIFYは実行0。stale ownerは結果をcommitしない。 |
| Discord | `_handle_utterance` final ASR/address判定 → `_grounded_search_reply` → 同じroute/outcome/presenter → 通常Discord発話・delivery | await後にinput epoch/response ownerを再検証。取消済み結果はUI/TTSへ出さない。 |
| 両方 | `NOT_REQUESTED` / `DEFERRED` は検索0、`REUSE_EVIDENCE` はpersona/surface/audience/epoch/TTL一致時だけ | 一時Evidenceはprocess-local、最大4結果・TTL 10分。長期Memoryへ保存しない。 |

P0のoffline fixtureは27件。検索のattempt/completed/grounded/deliveryを別々に照合し、配送欠落を成功分母から除外しない。自然さ・感情適合・音質は本文や実音声を評価していないため `None / NOT_MEASURED`。既存の機械的指標は `HEURISTIC`。

## 保持処理の静的監査

| owner / 呼出し | 条件 | 現行処理 | P0判定 |
|---|---|---|---|
| `mind/mind.py` 初期化 | transcript有効 | `prune_transcripts(retention_days, max_items)` | 起動だけで更新し得る。write無効がmaintenanceも止めるとは仮定不可。 |
| `mind/store.py::prune_transcripts` | `created_at < now-retention_days` | transcriptをDELETE | 本番DB試験前にMの非削除契約が必要。 |
| 同上 | transcript件数が`max_items`超過 | 古い順に超過分をDELETE | 容量増を許容するユーザー方針と未整合。 |
| `mind/mind.py` 内省完了 | reflection処理後 | `store.prune(max_items)` | 保守時にも更新し得る。 |
| `mind/store.py::prune` | memory件数が`max_items`超過 | preference/correction/promise、self_failure、user_statementはarchive。それ以外はDELETE | archive保護対象以外は物理削除される。Mまで本番適用禁止。 |

この監査は静的確認のみ。削除・復元・migration・実DB件数確認は行っていない。

## P2の境界

- queryに関連するPAGEをSNIPPETより優先し、title用160文字cleanerとEvidence用1200文字cleanerを分離。TTS抽出は最大360文字。
- http(s)、資格情報なし、public addressのみ。redirectは3回まで、responseは1 MiBまで。Web本文は`UNTRUSTED_DATA`でありAction/Tool権限へ接続しない。
- `FAILED`、`EMPTY`、`PARTIAL`、`CANCELLED`を区別し、検索失敗を通常生成で補完しない。抽出整合をgroundedと呼び、真実性の保証とは呼ばない。
- query+fetch全体は一つのmonotonic deadline（最大7秒、既存設定が短ければその値）を共有する。owner callbackを持つDiscord/shared executorは、各query/page/redirectへ残時間と同じ`is_current`判定を渡し、期限切れまたはcancel後の後続fetchを始めない。開始前cancelに加え、最大50ms間隔で検索中staleを検出してPython側待機を解除する。実行中threadを強制終了せず、内部の次境界で停止させ、結果をcommitしない。Local Tool経路は同じstrict deadlineと既存のresponse/delivery gateを使うが、50ms pollingの直接利用は検証主張に含めない。

## 検証

- offline evaluator: 27 synthetic cases、外部I/O 0。
- TDD RED: API欠落4 collection error、複合検索未検出、query汚染、固定品質点、PAGE選択、private URL、provider error同一視を個別に確認。
- fault injection: 複合command判定除去、Evidence hash検証迂回、stale guard迂回がそれぞれ期待通りtestを赤化。復元後GREEN。
- deadline fault injection: 遅いprovider、期限切れpage、開始前cancel、検索中staleを注入し、late結果と後続fetchがcommitされないことを確認。
- 2026-09-08 cancellation RED/GREEN: stale化後もprovider完了まで約0.203秒待つ欠陥と、query後cancelをpage処理へ伝えられない欠陥を2件の失敗testで再現。修正後deadline単体11 passed、拡張targeted 242 passed / 1 expected Windows symlink skip、最終再検証の全suite 3014 passed / 1同skip（103.12秒）。
- 実機・外部検索・Local LLM・Discord・本番DB: 未実行。
