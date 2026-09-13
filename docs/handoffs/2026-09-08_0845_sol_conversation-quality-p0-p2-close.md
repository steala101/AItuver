# Sol handoff — 会話品質 P0〜P2 code close

- Date: 2026-09-08 08:45 JST
- 担当: Sol（前Sol実装の継承・再検査・P2 deadline/cancel完了）
- Status: `CODE_VERIFIED / HARDWARE_PENDING / HUMAN_QUALITY_PENDING`
- Scope: P0評価、P1検索route、P2 Outcome/Evidence/Presenter/deadline/cancelのみ。P3以降と独立Mは未着手。

## 結果

P0〜P2をdeadline/cancelまでcode closeした。27件offline corpus、Local/Discord fake境界、query-aware Evidence、最大7秒または既存設定の短い方を使う共有deadlineを確認した。Discord/shared executorでは検索中staleの早期待機解除と後続page/redirect停止も確認した。Local Tool経路はstrict deadlineと既存のresponse/delivery gateまでで、50ms pollingの直接利用は主張しない。外部検索、Local LLM、TTS、Discord実接続、本番DBは使っていない。

## 根本原因と修正

前実装はdeadline値と結果commit拒否を持っていたが、`execute_search_route()`が`future.result(timeout=budget)`を一括待機し、`is_current`を検索workerへ渡していなかった。そのため、検索中にepoch/response ownerが失効してもprovider完了まで待ち、query完了後にpage fetchを始め得た。既存testは開始前cancelと完了後stale拒否を確認しただけで、この2点を検出できなかった。

- `neuro_voice/cognition/search_result_presenter.py`: 最大50ms間隔でownerを再検査し、stale時は`CANCELLED`を早期返却。deadline/cancel callbackを対応searcherへ渡す。
- `neuro_voice/search/deepsearch.py`: 同じ`is_current`をquery前後、page submit前、各page worker、redirect前へ伝播。deadlineは従来どおりquery+fetchで一つ、7秒と`search.timeout_s`の短い方。
- `tests/test_search_fetch_safety.py`: 検索中cancelの待機解除、query後page抑止、短い設定値優先を追加。

## RED / GREEN / fault injection

- RED 1: slow provider中にownerを失効。期待0.1秒未満に対し約0.203秒待ち、失敗。
- RED 2: query中にownerを失効。strict APIがcancel callbackを受け取れず`TypeError`、後続page抑止不能を確認。
- GREEN: deadline/cancel単体 `11 passed`。
- 拡張targeted: P0〜P2とPhase 8/delivery回帰11ファイルで `242 passed, 1 skipped in 4.32s`。skipはWindows symlink権限のみ。
- offline CLI: synthetic 27/27、明示検索成功3、配送失敗0。naturalness/emotional_fit/voice_qualityは`null / NOT_MEASURED`。
- 最終全pytest: `3014 passed, 1 skipped in 103.12s`。skipは`tests/test_tool_dialogue.py:663`のWindows symlink権限のみ。
- 故障注入の復元: command判定、Evidence hash/bounds、開始前/検索中stale、deadline、private/redirect guardの各testが最終GREEN。今回作成した`.bak/.orig/.rej`はない。`data/backups/legacy_scope/`の既存DB backupと、2026-07-27の無関係な空`docs/DECISION_LOG.md.tmp`は触らず保持した。fault用変更は残っていない。

## 非変更・データ安全

- `config/config.yaml`: 最終更新2026-08-07のまま。
- `data/`内の最新更新: 2026-08-07のまま。Memory DB、persona JSON、backup、migrationを開いていない。
- `logs/cognitive_trace.jsonl`: 最終更新2026-08-07のまま。過去Traceへ追記・改変なし。
- `.env`、raw audio/image、model、実行用`.venv`、DAVE mix/leave、TTS設定は未変更。
- 自動検証はfake依存／ローカルfixtureだけ。外部network、クラウドAPI、有料推論なし。

## 変更ファイル

今回の実装差分は次の3ファイル。

- `neuro_voice/cognition/search_result_presenter.py`
- `neuro_voice/search/deepsearch.py`
- `tests/test_search_fetch_safety.py`

文書同期: 計画、ADR-0005、監査、CURRENT、CODEMAP、DECISION_LOG、KNOWN_RISKS_AND_DEBT、SHARED_CHANGELOG、旧handoff注記、`_NEXT_SESSION.md`、本handoff。

## rollback

Git rootは無効なのでcommit/resetは使わない。戻す必要がある場合は上記3ファイルの2026-09-08 hunkだけを手作業で戻し、新規3testを同時に外す。ただし検索中staleがprovider完了まで待ち、後続pageへ進む欠陥が再発する。DB・config・persona・Traceをrollback対象に含めない。

## 残課題と実機最小手順

自動試験はcode contractまで。実検索の内容、自然さ、音質は未測定。R-049により、本番DBを使う起動試験は独立Mの非削除契約より先に行わない。Mを閉じるか、本番dataへ接続しない隔離profileが確認できた後だけ、次の最小2発話をユーザー操作で行う。

1. アプリを完全終了・再起動し、idleでUIの認知ボタンを`PROD`へする（process-local、永続設定は変えない）。
2. `饅頭こわいを検索して、短く教えて`を一回だけ発話。検索一回、根拠付き短答、UI source一件、logical session一回、replay 0、finalizedを確認。
3. 続けて`さっきの話の結末も分かる？`を一回だけ発話。同epoch Evidenceが足りればその範囲だけ、足りなければ不足を明示し、題名から創作しないことを確認。

Local合格後にだけDiscord複数人で同じ2件と、検索中の新入力による旧結果UI/TTS 0を確認する。30turn、音声切替、Fish Audio、P3以降はこの受入へ混ぜない。

## 後日確認事項

- PARTIAL Evidenceでは現在の抽出文を読み上げてよいか、常に不足だけを短く伝えるか。
- 製作者Identity、強いからかいを避ける題材、transcript全文保持、backup/容量監視、D-041のLocal LLM要約、Fish Audio比較条件は計画末尾の確認待ちを維持。

## 関連

- `docs/superpowers/plans/2026-09-07-conversation-quality-sol.md`
- `docs/adr/ADR-0005-conversation-quality-recovery.md`
- `docs/audits/2026-09-07_conversation-quality-p0-p2.md`
- Decision D-044、Risk R-048/R-049
