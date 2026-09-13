# 既知のリスクと設計負債

- Snapshot: 2026-07-26
- Severity: Critical / High / Medium / Low
- Likelihood: High / Medium / Low

## R-049 記憶保持方針と既存の自動削除処理が一致しない

- Status: CODE VERIFIED / USER-APPROVED PRODUCTION PROMOTED / 残存運用リスクあり（2026-09-10）。
- Evidence: 非削除policy、write-disabled retrieval、全source mutationのrollback/read-only、永続revision、fixed1000組Recall gate、cosine .90のMemory/transcript実働復旧、canonical persona/status/vector shapeとsidecar bucket/exact provenance再検査、logical poisoned sidecarの一回再構築、wrong-vector/invalid-only exactからfull ANNへの復旧、atomic repair競合、Windows `PermissionError` fallback、実`SQLITE_FULL`、query-only current/old schema、不正TABLE/VIEW schema一回修復、hardlink/path escape/open時差替え、backup失敗をtmp_pathだけで検証。canonical raw→unit float32は一度だけ正規化し、sidecarとprovenanceが同じ表現を使う。各100kのMemory/transcriptはlexical p95 6.821ms、target cosine .99985 fast Mind/transcript p95 10.374/6.115ms、target cosine .90 full Mind/transcript p95 367.410/163.909ms、index build 208.847s、Python allocation peak約371.0MB、sidecar約320.6MB、Recall/restart rebuild 0。2026-09-10の承認後に本番7 DBをonline backupし、全integrity・件数・ID/本文/summary/embedding指紋一致、隔離copy検証を経てschema/sidecarを昇格。canonical指紋不変、restart rebuild 0、既知Recall成功。容量healthは既定20% warningとprobe失敗UNKNOWN、自動削除0・write停止0を回帰。UIのnull/UNKNOWN表示と未丸め20%境界も実経路テストで修正し、全suite3078 passed / 2 expected skips。
- Risk: 自動消失経路はcode上閉じ、本番schema/sidecar昇格も指紋不変で完了したが、本番既存DBで過去に削除が起きたかは不明。確認付き自然言語削除UXは未実装。容量healthはstatus参照時のoperator警告で、自動通知や自動回収は行わない（自動削除しないための意図した制約）。probe不能時はUNKNOWNのまま会話と書込み方針を継続する。各100kで派生buildが同じsource lockを約209秒保持し、sidecar約320.6MBを要する。online snapshot rebuildは整合性を崩す大改造になるため未実装。cosine .90 full tierは高衝突syntheticで367.410ms p95となり、品質・provenance integrity gate維持との明示的tradeoffである。一般semantic latencyや本番SLOではないため、実モデル分布で別測定が必要。同じOS directoryへ書ける悪意あるownerがsidecar行とmetadataを任意に一貫改変・削除する場合、検出不能な候補omissionの可用性は保証外。canonical ACL/status/vector/provenance再検査はconfidentiality/integrityを守るが、悪意ある同権限writerによる完全な可用性破壊は防げない。path-name SQLite openのTOCTOUも、open前後/DDL前検査で狭めても完全防御ではない。9回p95は探索値で、RAMはOS RSS/SQLite/filesystem page cacheを含まない。
- Mitigation: `storage_health.capacity`と「こころ」で空き率・既定20%閾値・測定先・UNKNOWNを監視し、必要なら`mind.storage.warning_free_percent`を明示設定する。低容量でもcanonicalを自動削除しない。online backup証跡`data/backups/memory_retention_stage_m/20260910_074937_preindex`を保持する。自然言語から破壊APIを直接呼ばず、明示確認UXを別設計する。sidecar/cacheだけを再構築対象にし、sidecar directoryは信頼できる同一ownerだけが書ける権限で運用し、omission疑いは明示rebuildで回復する。
- Priority: P1 residual operations / 自然言語削除UX・実分布性能・長時間build lock。

## R-048 複合検索の実機受入と会話品質評価の未測定項目

- Status: CODE VERIFIED AFTER OBSERVED PROD REGRESSION / HARDWARE RE-ACCEPTANCE PENDING（2026-09-10）。
- Evidence: 複合Intent/query、共有route、query-aware Evidence、未測定品質のnull化、deadline/cancelに加え、実機Localの`まんじゅうこわい`検索とENDING追質問でWikipedia navigationが反復し、TTS HTTP 422 URLがUIへ露出する回帰を観測した。article-main/MediaWiki抽出、generic paragraph fallback、navigation棄却、完全なBRIEF、現在route modeの支持付きENDING、一回実行、Local/Discord TTS/UI秘匿と`.sys`選択/折返しを19件のRED→GREENで固定した。Astra reviewのDiscord漏洩、void/malformed stack、cross-result ENDING、meta cueも全件再現・修正。全suite3097 passed / 2 expected skips。外部検索・実TTS・Discordは再実行していない。
- Risk: HTML構造やactual outcome cueは有界なheuristicであり、検索providerが本文根拠を返さない場合は不足回答になる。保守的meta棄却は本物の結末文を不足扱いにする場合がある。抽出整合はWeb内容の真実性を保証しない。実機の検索結果分布、発話自然さ、TTS成功、Discord複数人での同意味論は未再受入。`.sys`はstatic CSS/DOM契約testまでで、実browserでの選択・copy・overflowは未確認。
- Mitigation: 変更後のLocalで複合依頼1件＋同epoch ENDING follow-up1件だけを再確認し、検索一回、本文短答、支持された結末または正直な不足、safe TTS診断、copyable source/errorを検査する。その後Discord複数人で同じ意味論とstale拒否を確認する。失敗時も自動再検索、LLM創作、Tool拡張をしない。
- User evidence: 変更前のLocal PROD 2turnは検索一回＋cached Evidence再利用だったが、両返答がnavigation boilerplateとなり、最初のTTSは長い句読点なし文字列でHTTP 422、UI errorは巨大なpercent-encoded URLだった。DAVE音楽継続・最新入力への切替・退出の過去報告とは区別する。
- Priority: P1 / 会話品質改善の第一段階。

## R-047 Phase 8 production-session hardware acceptance remains pending

- Status: open / manual acceptance required.
- Evidence: the Local terminal-cue regression now passed with `REMAIN_SILENT` and complete silence provenance. The existing `web_search.search` is the sole Local, process-local READ_ONLY Tool probe, with a bounded evidence presenter, one-attempt cap, and shared terminal delivery coordinator. Discord explicit search now shares the evidence presenter, while DAVE latest-input cancellation, mixed music ducking, and leave acknowledgement are regression-tested. The full isolated suite is `2974 passed, 1 skipped`. Revised Local delivery and all revised Discord behaviors still require user-operated acceptance.
- Mitigation: keep persistent `cognition.rollout_mode=disabled`; restart before each hardware probe. Verify one Local PROD search delivery first, then Discord search grounding, latest-input cancellation, music ducking, and leave acknowledgement separately. Do not broaden Tool scope or enable write effects from these probes.

## R-046 Windows filesystem capability prevents one symlink integration test

- Status: open / environment capability.
- Evidence: Final separate `.venv-test` full pytest on 2026-08-05 completed `2897 passed, 1 skipped`. The KTaNE fixture now uses pytest `tmp_path`. `tests/test_tool_dialogue.py::test_a_symlinked_directory_cannot_escape` alone skips when this Windows account lacks `SeCreateSymbolicLinkPrivilege` (`WinError 1314`); a privilege-independent unit test still asserts the same resolved-path escape rejection.
- Affected: only the OS-level symlink creation integration setup is unavailable on this host; the escape-rejection logic remains covered.
- Mitigation: run the skipped integration test under Developer Mode, an account with symlink privilege, or approved Linux CI. Do not weaken the path-escape guard.

## R-001 実働ルートに有効なGit履歴がない

- Severity: Critical
- Likelihood: High
- Evidence: root `.git`は空で`git status`不能。nested `AItuber/`だけがclean repo、commit `52469d4`。
- Affected: 全ファイル、AI協業、rollback。
- Risk: 誤上書き、差分喪失、古いsnapshotを正本と誤認。
- Mitigation: 正本remoteを所有者確認後、実働rootを履歴へ接続。初期snapshot前に`.env`、data、logs、models、media、external model treeを監査。
- Priority: P0

## R-031 KTaNEローカルマニュアルの決定的solverが未完

- Severity: High
- Likelihood: Medium
- Evidence: `game_profiles/keep_talking_and_nobody_explodes/`は公式日本語版1-jaと検証コード122、Expert role、安全制約を固定したが、全モジュールの規則をcode-owned solverへ移していない。
- Affected: 時間制限下の爆弾解除助言。
- Risk: モデル既知知識だけでは版違い・表の取り違えが起きうるため、情報不足時は断定禁止としている。
- Mitigation: 公式マニュアルの明示同期、版hash、モジュール別deterministic solver、音声replay fixtureを順次追加する。
- Priority: P1

## R-032 ローカル音声stream復旧のGUI実機受入が未完

- Severity: High
- Likelihood: Low to Medium
- Evidence: 2026-07-26 17:23の実機ログでdevice watcherによるPortAudio全体再初期化後、入力と出力が停止した。read-only poll、実active監視、出力reopen/retryの自動テストは成功したが、修正後GUIでの30秒以上の連続入出力は未確認。
- Affected: ローカルマイク、ローカルTTS、TurnManagerのspeaking表示。
- Risk: 別のUSB切断・Windows backend状態でもstream停止が起きる場合、再接続の実機調整が必要。
- Mitigation: GUI再起動後、連続2回以上のSTT/TTSと30秒以上のlevel継続を確認し、未復旧なら`再生ストリームが停止しました`と`マイクを開き直しました`の時系列を採取する。
- 2026-07-27追記: 自動復旧が救えない2ケース（起動時に不在だった機器の後付け、Windowsがエンドポイントを移し古いハンドルが`active`を返し続ける）に対し、設定画面へ手動の再認識ボタンを追加した。自動監視の`refresh=False`方針は不変で、プロセス全体の再初期化は明示操作時のみ許す。手動経路のみ既定デバイスへのフォールバックを許可し、その場合は画面へ明示する。GUI実機受入は未実施。
- Priority: P0 until hardware acceptance, then close

## R-033 ポッポの新しい人格表現は実会話の受入調整が必要

- Severity: Medium
- Likelihood: Medium
- Evidence: `playfulness`、`cheekiness`、`spontaneity`の経路と深刻場面guardは自動テスト済みだが、ローカルLLMが選ぶ実際の冗談頻度とSBV2の聞こえ方は自動テストでは評価できない。
- Affected: ポッポの雑談、冗談、からかい、真面目な場面との切り替え。
- Risk: 生意気さが強すぎる、逆にほとんど出ない、同じ形式の小ボケを繰り返す可能性がある。
- Mitigation: 10〜20ターンの雑談、成功報告、訂正、落ち込み相談を実機で試し、developer traceのstyle/shape/patternと発話を照合する。調整はまずポッポ固有の3軸で行い、global既定や安全gateを安易に緩めない。
- Priority: P1
- Confidence: CONFIRMED

## R-034 Discord画面共有はOBS relay依存で実機受入が未完

- Severity: Medium
- Likelihood: Medium
- Evidence: `DiscordScreenShareSession`のcommand、Discord共有/OBSゲームsource切替、profile/解像度分離、Local/Discord routing、lifecycle、質問時frame世代固定、request ID一致時だけのsource付きUI preview、Minecraft background observationからGameCompanionDirectorへのLocal/Discord配線は自動テスト済み。実ログではOBS 1fps取得中にも質問が質問前の13秒級解析へjoinし、その後15秒級の通常LLM生成を重ねて古い景色を回答していた事実を確認し修正した。さらに22:01の「今、画面見てる？」ではfresh captureが発生せず、通常message buildがqueued frameを「いま見た画面」として再送していたため、明示語句追加・通常再送廃止・取得失敗時クリア・request ID検証を追加した。「豚肉を焼くには」は公式knowledge laneへ分離し、scene/OCR directとvisual reasoningを分離済み。関連33件、全体740件成功。修正後の実Discord Go Live + OBS Window Capture長時間受入は未実施。DAVE sidecarは映像を転送しない。
- Affected: Discordゲーム画面の継続認識、明示視覚質問、GPU応答遅延。
- Risk: OBSソース名不一致、ソース非表示、共有ポップアウト再生成。1280pxでも共有映像がOBS canvas内で極端に小さい場合、OCR精度は保証できない。Minecraft解析幅を768pxへ下げたため、細かいHUD/item icon/countの認識精度は実機確認が必要。質問時frameへ固定しても、現行Gemma 4 12B visionは数秒〜cold時約13秒を要するため、激しく変化するゲームでは回答時点までに状況が変わり得る。視覚根拠付き判断はvision+会話LLMの二段なので直接scene説明より遅い。自動相棒発話はmodelの`scene_changes/commentary_worthy/suggested_help`品質に依存し、変化の見逃し、不要発話、誤った助言の可能性がある。知識/視覚分類に未知の言い回しがあると誤routingし得る。
- Mitigation: OBS sourceを`Discord画面共有`として常時有効にし、Local micとDiscord direct音声の両方で開始・10秒継続・文字質問・source切替・停止を確認する。Minecraftで「肉の焼き方」「今の持ち物で焚き火を作れる」「次はどうすれば」を連続して試し、安定知識はvisionなし、後二つは質問時frame+公式knowledge+通常LLMになり、情景要約/internal keyを発話しないことを確認する。採掘・戦闘・発見・通常移動・静止を各1分試し、発話数、重複、危険時の助言、誤助言を記録する。過多ならprobability/cooldown、見逃しならimportance thresholdを設定だけで調整する。768pxでHUD/状況認識が落ちる場合は幅だけ1024pxへrollbackする。移動前後の質問でUI previewと回答を照合し、ログ`Explicit vision completed`の`analyzed_seq/latest_seq/capture_age/scene_distance`を確認する。OBS側ではDiscord共有映像をcanvasいっぱいにfitする。真の1〜2秒級semantic latencyには小型vision model、専用GPU、game telemetry/OCR併用を別フェーズで評価する。direct DAVE videoはsession transportを置換して段階導入する。
- Priority: P1
- Confidence: CONFIRMED

## R-002 同一folderを複数AIが編集

- Severity: Critical
- Likelihood: High
- Evidence: 運用要件。lock/AGENTS/handoff標準なし。
- Affected: 全コード。
- Mitigation: 一writer、branch/worktree、handoff。暫定lockは承認後。
- Priority: P0

## R-003 LocalとDiscordのresponse runtime重複

- Severity: High
- Likelihood: High
- Evidence: `VoicePipeline`と`DiscordBridge`が各自response ID、TurnManager、PlayedText、search、TTS、autonomyを所有。
- Affected: barge-in、stale audio、検索、会話参加、自発。
- Risk: 片側修正で他方が退行。
- Mitigation: characterization test後、共有Response Runtime/Autonomy semanticsを段階抽出。
- Priority: P0

## R-004 巨大classと責任集中

- Severity: High
- Likelihood: High
- Evidence: `DiscordBridge`約3,900行、`VoicePipeline`約3,000行、GUI約2,750行、`Mind`約1,900行。
- Affected: lifecycle、state、testability。
- Mitigation: interface/test-firstの段階抽出。一括rewrite禁止。
- Priority: P1

## R-005 `privacy`設定の二重定義

- Severity: High
- Likelihood: High
- Evidence: `config/config.yaml` 369行付近と723行付近。PyYAMLは後勝ち。
- Affected: media保存、context/output gate。
- Risk: 期待したprivacy値が読み込まれない。
- Mitigation: 一つのmappingへmigration、duplicate-key loader validation、config test。
- Priority: P0

## R-006 Memory schemaのACLメタデータ不足

- Severity: High
- Likelihood: Medium
- Evidence: `mind/store.py`のmemories/transcriptsにowner/source_audience/visibility/confidence/provenance列がない。
- Affected: group retrieval、forget、audit。
- Mitigation: versioned migration、legacy recordはgroup deny、ACL test。
- Priority: P0

## R-007 Config責任とbackend名の重複

- Severity: Medium
- Likelihood: High
- Evidence: `tts.backend`と`audio_output.backend`、ConfigとGUI persistence helper。
- Affected: TTS switch、UI表示、persona voice。
- Mitigation: canonical keyを決定、互換read + one-time migration、UI/factory contract test。
- Priority: P1

## R-008 Legacyと新autonomyの残存

- Severity: Medium
- Likelihood: Medium
- Evidence: `proactive`、`legacy_spontaneous_mode`、new AutonomousActionSystem、vision auto commentary。
- Affected: 自発発話、重複speech。
- Mitigation: runtime statusにactive engine表示、mutual exclusion test、削除条件をDecision Logへ。
- Priority: P1

## R-009 GPU schedulerが分散

- Severity: High
- Likelihood: Medium
- Evidence: LLM/STT/TTS/visionがCUDAを使用、visionは独自busy/cancelのみ。共通reservation owner未確認。
- Affected: first token、TTS、vision stall、OOM。
- Mitigation: priority-aware GPU work gate、前景preemption、p95/VRAM metrics。
- Priority: P1

## R-010 非同期task所有の分散

- Severity: High
- Likelihood: Medium
- Evidence: local/Discord/GUI/Mind/vision/research/directiveが多数の`create_task`。
- Affected: GUI close、unretrieved exception、process残留。
- Mitigation: task registry/TaskGroup相当、owner close contract、shutdown/exception test。
- Priority: P1

## R-011 turn-local TTS sentence queueがunbounded

- Severity: Medium
- Likelihood: Medium
- Evidence: `pipeline.py`の`asyncio.Queue()`。
- Affected: 長文、遅いTTS、cancel時memory。
- Mitigation: bounded queueとbackpressure、generation guard。
- Priority: P2

## R-012 診断mediaとretention

- Severity: High
- Likelihood: Medium
- Evidence: `logs/`に過去のWAV/JPG診断artifactが存在。privacyコメントは非保存方針。
- Affected: raw voice/image、個人情報。
- Mitigation: explicit debug flag、consent、TTL cleanup、path separation、通常defaultで保存禁止。
- Priority: P0

## R-013 Privacy regex/出力guardの限界

- Severity: High
- Likelihood: Medium
- Evidence: `PrivacyManager`はrule-based regex中心。
- Affected: PII、group disclosure。
- Mitigation: structured ACLを第一境界、regexは補助。adversarial corpusとdeny-by-default。
- Priority: P1

## R-014 DB migrationとrecoveryの統一不足

- Severity: High
- Likelihood: Medium
- Evidence:複数SQLite/JSON store。Memory storeに明示schema versionなし。Backup temp unlinkのWinError 32実績。
- Affected: update、backup、persona data。
- Mitigation: storeごとのschema version/migration、atomic replace、closed-handle cleanup retry、restore test。
- Priority: P1

## R-015 README/設計文書の時点混在

- Severity: Medium
- Likelihood: High
- Evidence: 複数世代のconversation docs、README冒頭図/Discord identity/TTS例と現行差。
- Affected: AI開発判断。
- Mitigation: snapshot date、CURRENT/TARGET label、本憲法への入口をREADME先頭へ追加する提案。
- Priority: P1

## R-016 Python実行環境が壊れている

- Severity: High
- Likelihood: High
- Evidence: global `python`なし、`.venv` interpreter path不存在。
- Affected:起動、pytest、文書作成時の検証。
- Mitigation: Python 3.11/3.12方針を確認しvenv再構築。dependency lock/hashを検討。
- Priority: P0

## R-017 Optional emotion dependencyが実行時無効化

- Severity: Medium
- Likelihood: High
- Evidence: 過去ログで`No module named 'opensmile'`、recognizerが自己無効化。
- Affected: emotion input、Style Manager。
- Mitigation: startup capability matrixとGUI表示。optionalを必須のように表示しない。
- Priority: P2

## R-018 Remote management認証

- Severity: High
- Likelihood: Low to Medium
- Evidence: token/session/rate limit実装はあるが、実bindとempty token運用はNEEDS_CONFIRMATION。
- Affected: join/leave、shutdown、persona/TTS。
- Mitigation: non-loopbackはstrong token必須、TLS/reverse proxy、audit log、CORS/CSP test。
- Priority: P1

## R-019 Search provider品質・routing

- Severity: Medium
- Likelihood: High
- Evidence: DeepSearchとintent/plannerが複数経路、過去に短文/文脈参照の誤検索。
- Affected: 会話latency、誤回答、privacy。
- Mitigation: intent single source、history-before-web、query/evidence test、timeout/fallback。
- Priority: P1

## R-020 視覚のstale/current混同

- Severity: High
- Likelihood: Medium
- Evidence: cached Perceptionとexplicit current captureが併存し、過去にstale frame問題が発生した。2026-07-26の実ログでは「今、画面見てる？」が明示視覚質問に分類されずfresh OBS captureなしで通常応答へ進み、Local/Discord message buildが既存`latest_frame`をUIへ再送していた。
- Mitigation: 明示current frameへ`explicit_request_id`を付与し、UI previewは現在response ID一致時だけ許可する。取得前に旧`last_explicit_frame`を消去し、通常message buildからframe再送を禁止する。「画面見てる/見えてる」も明示視覚語句へ含める。
- Affected: game guidance、安全性、latency。
- Mitigation: captured_at/analyzed_at/age、explicit fresh capture、no silent fallback、replay test。
- Priority: P1

## R-021 話者identity誤統合

- Severity: High
- Likelihood: Medium
- Evidence: voiceprint、local alias、Discord nickname/accountを統合する機構。
- Affected: relationship、memory、privacy。
- Mitigation: confidence/hysteresis、account≠person、merge audit/undo、group ACL。
- 2026-07-27追記: ヒステリシスが確定閾値以上の一致まで直前話者へ引き戻していた。また「直前に話した人」を`time.time()`（Windowsは約15.6ms解像度）だけで決めており、同一tick内の2発話が登録順で決着していた。上限条件と単調カウンタを追加。実機Windowsでの連続発話中の名前安定は未確認。
- Priority: P1

## R-022 自発発話の曖昧な過去参照

- Severity: Medium
- Likelihood: Medium after 2026-07-30 mitigation
- Evidence: 過去のユーザー報告とworking/open-thread経路。2026-07-30にopen-thread callbackを既定OFFとし、未使用focus一件の選択・意味的重複拒否・候補枯渇時の沈黙・自発assistant履歴commitを追加。自動回帰は成功したが実GUI長時間受入は未実施。
- Affected: 人間らしさ、信頼。
- Mitigation: 5〜10分の実GUI会話で、自発質問への返答接続、同一focusの再発なし、候補枯渇後の沈黙をLocal/Discord別に確認する。再発時はprompt文言でなくdecision detailsの`focus_signature`と履歴commitを調べる。
- Priority: P1

## R-023 テストの実行baseline不明

- Severity: High
- Likelihood: High
- Evidence: 186 Python files/約46,636行、testsはあるが現在runtime不能。
- Affected: 全改修。
- Mitigation:環境復旧後にfull baseline、failed/quarantined分類、CI導入。
- Priority: P0

## R-024 外部model/server lifecycle

- Severity: Medium
- Likelihood: Medium
- Evidence: Ollama/VOICEVOX/SBV2 auto launch/self-heal/unloadをGUIが管理。
- Affected: first response、GUI close、GPU/port残留。
- Mitigation: owned vs external processを区別、health/prewarm、shutdown contract、port conflict test。
- Priority: P1

## R-025 循環依存

- Severity: Medium
- Likelihood: Medium
- Evidence: Mind、dialogue、activity、autonomy間でruntime import/dynamic importが存在。完全なcycle graphは未作成。
- Affected: import、test、分割。
- Mitigation: dependency graphをCIで生成し、shared model/contractsを下位layerへ置く。
- Priority: P2
- Confidence: UNCERTAIN

## R-026 継続行動基盤が実機未検証

- Likelihood: High
- Evidence: BehaviorDirective一式、STT補正、話者統合、文脈予算、デバイス再接続を2026-07-26に実装した。実機ログではラジオ停止文が新規MONOLOGUEへ反転し、終了後も自律発話が続く不具合を確認して修正したが、修正後の実機音声受入は未実施。
- Affected: ラジオ／実況／見守り／物語の各モード、Discord区間実行、VRAM、レイテンシ。
- Mitigation: 実機で3分ラジオを開始後「一旦終わろう」で停止し、100ms級の無音化、古い区間の再出現なし、5分間の自発再開なし、明示的な再開要求は動作、をLocal/Discordで確認する。`directive_user_turn reason=stop_request`と`autonomy_suppressed`をログで確認する。
- 2026-07-27追記: 実機ログから2件の根本原因を特定して修正した。(1) VAD検知時点で合成済み音声を破棄しており、相槌で読み上げが永久に止まっていた。(2) `conversation_id`が話者識別完了前に決まるため、継続セッションが`local:user`へ登録され以降のターンから到達不能になっていた（TRPGのNARRATIVE Directiveが無言で孤児化）。以前「話題の乗っ取り」と解釈して話題ガードで塞いだ症状の真因はこちら。修正後の実機受入は未実施。
- Priority: P0
- Confidence: CONFIRMED

## R-037 状態遷移の「定常状態」を検証していない変更が続いている

- Severity: Medium
- Likelihood: High
- Evidence: 2026-07-26〜27に同型の見落としが3回。(1) `AudioDeviceMonitor`の既定`refresh=True`（3秒ごとにPortAudioを再初期化し入出力を停止）、(2) VAD検知時点での音声破棄（相槌でも復帰できない）、(3) `note_suppressed_user_turn`が手番を返した先で`WAIT`が再試行され、90秒で約20回のLLM生成。いずれも「1回の遷移」は正しく、「繰り返した時に落ち着くか」を見ていなかった。
- Affected: 継続行動基盤、音声デバイス、割り込み。周期処理を持つ箇所すべて。
- Risk: 症状が例外もエラーログも伴わない（無音、遅延、空回り）ため、実機で使うまで気づかない。
- 2026-07-28追記: 4例目と5例目。(4) プローブの設定キーを、隣に正しく読んでいる`factory.py`があるのに推測で書いた。(5) `keep_alive`送信を、ストリーミング用コルーチンの内部という**サーバなしでは一度も実行されないコード**へ追加し、存在しない属性名のまま全応答を停止させた。共通するのは「**実行して確かめられる形にせずに書いた**」こと。
- 2026-07-29追記: 6例目。`_respond_from_text` が存在しない名前 `transcript` を渡しており、**テキスト入力は一度も応答していなかった**（2026-07-26のASR作業で混入。音声中心の利用のため3日間表面化せず）。同じ形の `keep_alive` と合わせ、**どちらも `compileall` では捕まらず、テストが一度も実行しないコード**にあった。
- 2026-07-29追記: 7例目。sampling診断器が「アプリと同一経路」と記載しながら、実アプリの`reasoning_effort=none`を送っていなかった。Gemma 4が120 tokenを内部思考だけで使い切った空本文3件を「全部同じ」と判定し、sampling不全という偽の結論を出しかけた。実経路の`request_extra_body()`を確認し、空本文を判定不能とするまで診断結果を採用してはいけなかった。
- Mitigation: 状態を変える変更には「この状態がN回続いたら何が起きるか」のテストを1件必ず添える。周期処理は1周ではなく複数周で検証する。**テストのない箇所へ手を入れる時は、まず実行できる形へ切り出してから変更する**（`request_extra_body()` がその例）。**すでに動いている同種の実装が近くにないか先に探す**。診断ツールは「同一経路」と宣言するだけでなく、productionのrequest builderを正本にするか送信fieldを照合し、空・timeout・lengthを成功サンプルへ数えない。**`pytest tests/test_no_undefined_names.py` が pyflakes で未定義名・二重定義を静的に検出する**（実行されないコードでも読むだけで分かる）。レビュー時にこの項目を参照する。
- Priority: P1
- Confidence: CONFIRMED

## R-038 プロンプトが規則の山になり、人格を埋めている

- Severity: High
- Likelihood: High
- Evidence: `persona` ブロック2296トークンの内訳は「会話のしかた954 / 事実性ルール622 / 冒頭335 / 最終出力ルール199 / **キャラクター設定187**」。規則2109に対し人格187で、比率は約9対1。`persona.py` だけで禁止形が11個、68行目と69行目は完全に同一の文が2回書かれている。これに加えて `Mind.build_context` が毎ターン4700〜5800トークンを積む。
- Affected: 応答テンポ（プロンプト評価が約1350ms）、応答の質（規則の声で喋る）、憲法1.2の「固有で成長する人格」。
- Risk: 不具合を1つ直すたびにプロンプトへ1文足す運用が続く限り、単調に悪化する。個々の追加はすべて正当なので、誰も止めない。実際、2026-07-27の1日だけで確信度の許可文・継続依頼ブロック・物語メモが追加された。
- Mitigation: 削る基準を速さではなく**責任の所在**へ置く（第2条）。文字を比べれば分かること（繰り返し、echo、フォーマット、音量コマンド、検索意図）はコードへ移してプロンプトから消す。判断が要る規則だけ残す。重複と矛盾を消す。**personaはユーザーの創作物なので、分類の提案を出して承認を得てから触る。**
- 2026-07-29追記: `DialogueIntelligence.prompt_context`の代表turnを約1833→1281 tokenへ圧縮した。Candidate score/棄却候補、二重のpersona数値、重複したAgreement/Curiosity内部値を再送せず、Pythonで選択済みの方針だけを渡す。直近返答例を動的に1件足した場合は約1374 token。`tests/test_conversation_generation.py`で対話制御context 1500 token上限を固定した。常時persona+planner+surfaceの既存3000 token上限も維持。
- 2026-07-29追記: 実GUIの8文では通常semantic recallが同一入力の過去assistant回答を複数件再注入し、対話制御contextの外側で自己模倣を強めていた。通常想起からassistant文面を除外し、正規化した同一ユーザー発話を一件へ畳んだ。明示的な会話履歴質問だけは回答照合のため従来どおり両発話を使う。
- Remaining risk: これは代表turnの対話制御部分の計測で、検索根拠、視覚、transcript想起、relationship等を含むMind全体の最大値ではない。実会話A/Bと初トークン時間の再計測が必要。
- Priority: P1
- Confidence: CONFIRMED

## R-039 Conversation Contractの日本語表層境界

- Severity: Medium
- Likelihood: Medium
- Evidence: 2026-07-30にActivity開始、未計画質問、過去会話・自己経験の根拠を決定的契約へ移した。現在の経験検証は高確信な一人称過去形、質問検証はTTSサイズ文の末尾、Activity開始は明示的な日本語行為表現を対象にしている。
- Affected: 言い換え・崩れたSTTを含むActivity開始、引用内の疑問文、正当に根拠のある将来のAI経験。
- Risk: 網羅率を上げるためregexを広げすぎると、通常の話題言及や引用、現在知覚を誤って遮断する。狭すぎると未根拠主張が残る。Streaming tokenはfinal確定前に一時表示される。
- Mitigation: reason code（本文なし）の件数と実GUI受入を照合し、再現文をテストへ追加してから境界を変える。将来AIに実際の媒体閲覧・操作経験を保存する場合は、自由文でなくprovenance付き`grounded_experience_claims`へ明示的に接続する。UIは`assistant_done.text`をcanonical表示とする。
- Priority: P1
- Confidence: CONFIRMED

## R-041 反応学習と意味連想は弱い観測であり、事実・好みの確定には使えない

- Severity: Medium
- Likelihood: Medium
- Evidence: 2026-07-30にMove反応学習と短いtopic association graphを追加した。明示的な「違う」「同じこと」は強い証拠だが、笑い・長い追質問・終了相槌は内容以外の理由でも起きる。日本語の短いtopic抽出と共起は因果関係を示さない。
- Affected: Move選択の長期傾向、話題展開、訂正連想。
- Risk: implicit signalを強く学ぶと、一時的な反応で人格が偏る。共起edgeを事実として話すと捏造になる。group由来の関連を別sessionやdirectへ混ぜるとPrivacy境界を破る。
- Mitigation: Move重みを0.70〜1.30、implicit alphaを0.025に制限し、割り込み/沈黙だけでは更新しない。graph promptは「事実ではない候補」と明記し、direct/group session scopeを分離する。実会話で誤帰属を見つけたら反応本文を保存せずsignal/Move/reason codeだけで再現fixtureを追加する。
- Priority: P1
- Confidence: CONFIRMED

## R-036 Directiveのキーが可変値から導出される

- Severity: Medium
- Likelihood: Medium
- Evidence: `Mind._dialogue_user_key()`は話者IDから`speaker:{id}`を作るが、話者識別は非同期で数百ms遅れる。またDiscordのヒント、話者統合でも変わる。2026-07-27に`DirectiveStore.rekey`で追従させたが、キーを生成する規則自体は変えていない。
- Affected: 継続依頼全般、記憶・関係性・適応学習と共通のキー体系。
- Risk: 追従を呼び忘れた経路が新たに追加されると、同じ孤児化が再発する。現在の呼び出し箇所は`identify_speaker`/`set_speaker_hint`/`merge_speakers`の3つのみ。
- Mitigation: 長期的にはDirectiveへ不変のセッションIDを持たせ、話者キーは属性として持つ形が正しい。当面は`Directive rekeyed`と`Directive replaced`のログで観測する。
- Priority: P1
- Confidence: CONFIRMED

## R-027 文脈予算による長期セッションの筋の喪失

- Likelihood: High
- Evidence: `neuro_voice/llm/context_budget.py`は古い往復を落とす。`num_ctx 8192` / 予約1280で履歴は約6900トークン。TRPGのような長文ターンでは十数ターンで最初の展開が落ちる。
- Affected: 物語進行、長い共同作業、過去参照の一貫性。
- Mitigation: セッション要約（落とす前に要点をDirectiveの`progress_summary`かMindへ畳む）を設計する。
- 2026-07-27: 実害を確認して部分対応した。TRPGで12ターン目に17件除外され、冒頭の場面設定が履歴から消える一方、systemのペルソナ指示は落ちないため、GMのナレーションが友達のコメントへ寄っていた。Directiveへ`opening_note`（永久保持）と`progress_notes`（直近6件）を持たせ、`directive_prompt_block`から注入する形にした。**モデルによる要約ではない**ため、細部は失われる。長い共同作業で細部が要る場合は別途設計が必要。
- Priority: P1（部分対応済み）
- Confidence: CONFIRMED

## R-028 継続依頼の指名が日本語表層に依存

- Likelihood: Medium
- Evidence: `intent_plan._CONTINUING` / `_SHAPES` / `_QUALIFIED` はいずれも正規表現。想定外の言い回しは指名されず、Directiveが作られないまま素の会話になる（実機で「GM兼プレイヤー」が指名されなかった）。
- Affected: 継続依頼全般。指名されないと自律発話が場を埋めに来る。
- Mitigation: 指名は広めに保つ方針を維持する。将来はKernelのLLM判断へ寄せる余地がある。指名漏れは`directive_plan_failed`イベントとログで観測できる。
- 2026-07-29追記: 広すぎる裸の`話してて`が「今のAIって話しててもAI感が強い」を指名し、LLMが30分の継続DIALOGUEを作った実害を確認した。裸の「話してて／何か話して」は依頼として文末にある場合だけ指名する。明示依頼3件と誤検知実例を回帰に固定した。
- Priority: P2
- Confidence: CONFIRMED

## R-029 巨大classがDirective配線でさらに肥大

- Likelihood: High
- Evidence: R-004の対象である`pipeline.py`と`discord_bridge/bot.py`へ、Directive配線・文脈予算・デバイス監視を追加した。共通部は`directive_runtime.py`へ切り出したが、host側の記述は両方に残る。
- Affected: R-004の解消difficulty。
- Mitigation: 次の分割時は`DirectiveHost`の構築を各surfaceのadapter moduleへ出す。
- Priority: P1
- Confidence: CONFIRMED

## R-030 Turn Closureの誤沈黙

- Likelihood: Medium
- Evidence: `TurnClosurePolicy`は低遅延のため決定的な語彙・構造判定を使う。短い日本語は「うん」だけでも相槌、実行承認、遊びの手など複数の意味を持つ。
- Affected: 一対一会話、Discord会話、音楽操作、Activity。
- Mitigation: 質問・訂正・依頼・actionable question・Activity・手番待ちDirectiveを優先するguard、`conversation.turn_closure.enabled`によるrollback、`plan_reason`の観測、実会話replay corpusでfalse-silenceを計測する。
- Priority: P1
- Confidence: CONFIRMED
- 2026-07-27追記: 沈黙の副作用として、ターンがDirective層へ届かない経路が存在した。`WAITING_FOR_USER`のセッションが無言で停止するため、`state.directive_waiting_for_user`ガードと`note_suppressed_user_turn`を追加した。語彙自体の誤沈黙は未解決のまま。
- 2026-07-29追記: 上記guardがinteraction種別を見ず、誤作成された一般DIALOGUEまで相槌終了を迂回していた。`Mind.directive_waiting_for_user()`を`needs_user_input`かつ`NARRATION / CO_THINKING`へ限定し、Activityの既存優先guardは維持した。

## R-035 Turn Closureが会話層より下のレイヤをまたぐ

- Severity: Medium
- Likelihood: Medium
- Evidence: `should_respond=False`の早期returnは、Directive層・Activity・記憶更新など「応答以外」の処理も同時に飛ばす。2026-07-27にDirective分だけを明示的に配線したが、同じ形の抜けが他にも残り得る。
- Affected: 継続依頼、Activity、話者統計、自律状態。
- Risk: 「返事をしない」判断が「そのターンが無かった」ことにされる。症状は例外もログも伴わない停止として出る。
- Mitigation: 早期returnの前に通す処理を列挙し、応答生成だけを条件分岐にする形へ寄せる。`plan_reason`と`directive_user_turn action=silent_turn`を突き合わせて、沈黙ターンで何が更新されたか観測できるようにする。
- Priority: P1
- Confidence: CONFIRMED

## R-040 省略の大きい日本語訂正は構造化判定から漏れる

- Severity: Medium
- Likelihood: Medium
- Evidence: 明示的な「AっていうのはBっていう意味」「どんなのってBだよ」は構造化できるが、主語も対象も省いた「いや、それじゃなくてさ」の正本は直近会話をさらに解決しないと確定できない。
- Affected: 会話修復、割り込み保留破棄、検索抑止、重複回答防止。
- Risk: patternを広げすぎると通常の反論や話題転換まで古い文脈の置換と誤認する。狭すぎると誤解した回答を継続する。
- Mitigation: 実会話traceから訂正ペアを匿名fixture化し、subject/meaning/reference confidenceを検証する。confidence不足時は古い回答を再開せず短く確認し、無根拠な正本を生成しない。
- Priority: P1
- Confidence: CONFIRMED

## R-042 自律研究の実ネットワーク品質とproducer範囲は実機受入が必要

- Severity: Medium
- Likelihood: Medium
- Evidence: 公開persona興味と一対一会話の知識gapからResearchQuestionを作る経路、履歴UI、privacy/risk/quota/duplicate gateは自動テスト済み。Web検索は外部ネットワーク、検索engine、対象語の広さに依存する。
- Affected: 自律研究の実行頻度、検索結果の具体性、こころ画面の履歴、後の会話で利用するKnowledge。
- Risk: 「ゲーム」「テクノロジー」のような広い興味は一般的な検索結果になりやすい。起動中に前景busyが続けばworkerは待機する。外部検索失敗は履歴へFAILEDとして残る。単一sourceを確定事実へ昇格させると誤学習になるため、現状はPROVISIONAL。
- Mitigation: GUI再起動後60〜90秒で履歴のcreator/reason/statusを確認し、失敗時はworker last_errorと検索接続を採取する。具体性改善はpersona興味を設定画面で具体化するか、Evidence品質fixtureを追加してからquery templateを変更する。raw transcriptやLLM自由topic生成へ戻さない。
- Priority: P1
- Confidence: CONFIRMED

## R-043 口語終了判定と低遅延Echo guardには意味範囲の境界がある

- Severity: Medium
- Likelihood: Medium
- Evidence: 「ほんとそれ」「マジでそう」はTurn Closureへ追加し、短い導入後の本文反復は最大二文のEcho probeで防止した。2026-07-30 23:20にassistant/user証拠を分離し、短い定型導入と短いuser語の誤検知を回帰固定した。初回は全文を待たないため、三文目以降から始まる意味的言い換えは完全には覆わない。真の抑止後だけ一回再生成する。
- Affected: Local/Discordの短い同意後の沈黙、再生成本文、TTS開始遅延。
- Risk: 終了語彙を広げすぎると選択・承認・訂正を沈黙させる。Echo保留を長くしすぎるとGPT Live型の低遅延を損なう。重複時の一回再生成はそのturnだけ応答を約1 LLM分遅らせ、groupで不要な再発言になる可能性がある。
- Mitigation: 質問・訂正・依頼・Activity・実行確認の優先guardを維持し、実会話の匿名fixtureだけを追加する。Echoは短い導入時の最大二文に留め、通常文は一文で解放する。再生成には`NO_REPLY`を許し、同じguardで再検査し、二回目は発話しない。ログのkind/scoreと`reply_regenerated.success`で実機頻度を確認する。
- Priority: P1
- Confidence: CONFIRMED

## R-044 SBV2発音安定化は実機聴感と自動回帰が未完

- Severity: Medium
- Likelihood: Medium
- Evidence: 実行ログでは短い読点chunkが別々に合成され、tsukuyomiモデルは`Neutral`一種類だけにもかかわらず会話感情由来のstyle weightが変動していた。読点をsoft boundaryへ変更し、単一styleモデルのweightを固定する修正と回帰テストは追加済みだが、今回の環境ではPython実行の利用上限によりテストを起動できなかった。
- Affected: Style-Bert-VITS2の日本語抑揚、読点前後の連続性、割り込み時のTTS chunk粒度。
- Risk: 読点chunkを長くすると割り込み時に破棄される未再生音声が少し増える。モデルや話者固有のアクセント誤りは、今回の文分割・weight修正だけでは直らない。複数styleモデルまでweightを固定すると感情表現を失うため、固定対象は一種類だけのモデルに限定している。
- Mitigation: GUI再起動後、読点を含む同一文を通常・喜び・落ち着いた文脈で読み、ログの`single_style=True`、`w=1.00`、`speed`、`length`と聴感を突き合わせる。複数styleのamitaroへ切り替えた場合は従来どおりstyle/weightが変わることも確認する。次にPython実行可能な環境で追加済みfocused testsを実行する。
- Priority: P0（実機受入完了後にclose）
- Confidence: 原因の構造と修正はCONFIRMED、最終的な聴感改善はNEEDS_CONFIRMATION

## R-045 重複発話の層特定は配線済みだが既定で無効

- Severity: Medium
- Likelihood: High
- Evidence: 2026-08-03 11:20。`TurnFrame` / `TurnCommitLedger` を Local/Discord の実経路へ挿し、重複を検出した層が `TurnMetrics.duplicate_stage` に残るようにした（handoff `2026-08-03_1120_claude_turn-tracker-phase7d-wiring.md`）。ただし `turn_integrity.turn_frame_enabled` は既定 false のため、**このままでは実機で重複が出ても何も記録されない**。チビの継続指示（設定のデフォルトを勝手に本番有効へ変更しない）に従い、既定は変えていない。
- Affected: 「たまに同じ文を繰り返す」の原因特定、Local と Discord の両経路。
- Risk: フラグを上げないまま次の重複が起きると、Phase 7C から3回続けて「原因未特定」で終わる。逆に `duplicate_suppression_enabled` を先に上げると、重複の代わりに欠落が出て、しかも記録が残らない。
- Mitigation: 実機で `turn_frame_enabled: true`（記録のみ・挙動不変）にして再現を待つ。層が分かってから抑止の要否を判断する。あわせて、Discord 側の断片検査は chunk_id 鍵で「同じ断片の再配送」しか見えない——Local の `commit_chunk`（発話ループごとに 0 から数える）と同じ強さにするには、送出の通し番号を `_speak()` の15箇所の呼び出しへ渡す必要がある。層が判明してから着手する方が無駄が少ない。
- Priority: P1
- Confidence: 配線と自動回帰は CONFIRMED、実機での層特定は NEEDS_CONFIRMATION

## 優先実施案

### P0

Git正本、single-writer、Python baseline、privacy duplicate、Memory ACL、diagnostic media、full regression baseline、継続行動基盤の実機検証(R-026)。

### P1

Local/Discord runtime共通化、task/GPU owner、巨大class分割(R-029)、config canonicalization、migration、search/vision/identity、長期セッションの要約(R-027)、沈黙ターンが飛ばす処理の棚卸し(R-035)、**プロンプトの規則整理(R-038)**。

### P2

optional capability UX、queue backpressure、dependency graph、文書時点ラベル。
