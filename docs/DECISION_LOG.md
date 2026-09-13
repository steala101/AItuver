# 設計決定ログ

## D-045 記憶sourceを自動削除せず、派生indexとbounded contextを分離する

- Date: 2026-09-08
- Status: ACCEPTED / CODE VERIFIED / USER-APPROVED PRODUCTION PROMOTED
- Decision: canonicalな`memories`/`transcripts`は件数・経過日数・低importanceだけでは削除/置換しない。startup/maintenanceの旧prune入口は単一`RetentionPolicy`の非削除契約を使う。検索はpersona/scope ACL付きの再構築可能なFTS5 sidecarからbounded候補を取り、canonical行でACLを再検査する。明示Recallは古い/archived sourceへ到達可能にする。明示削除、privacy訂正、DO_NOT_STOREは別の理由付き境界として維持する。SQLite source write失敗はrollbackしてprocess内read-onlyへ倒し、既存sourceを空き容量確保のため削除しない。
- 2026-09-09 review amendment: freshnessはmtimeでなくindex対象列だけの永続revisionが正本。semantic Recallはexact＋4 quantized bandのsidecar候補を各bandで事前LIMITし、canonical persona/scope/status再検査後にcosineで順位付けする。アクセス記録はrevisionを進めない。既存行更新も共通source-write failure境界を通る。不正schemaは派生物だけ一回修復し、linked/escaped sidecarを別DBとして開かない。
- 2026-09-09 final-review amendment: sampled/quantized bandは高cosine量子化境界とbucket衝突順でfalse-negativeになるため廃止。sidecar generation 5は全次元を使うdeterministic dense random-hyperplane 4表を持ち、exact＋Hamming距離1をbounded probeする。Memoryとtranscriptの実働経路だけがこれを使用し、canonical ACL/status/vectorを最終正本とする。完全migration済みquery-only DBはretrieval-only startup、migration必須DBはtyped error＋connection close。不正VIEW等は派生ファイル全体だけを一度置換する。
- Final evidence: tmp_pathだけのretention/restart/real `SQLITE_FULL`/query-only current+old schema/B→C→B/poisoned persona+status/explicit delete/DO_NOT_STORE/corrupt+TABLE/VIEW wrong-schema rebuild/link swap safety/backup failure tests。高cosine境界、旧bucket 300衝突後方、語彙違いMemory/transcriptは全embedding scanを失敗させても実働経路で成功し、embedder warmupの旧全件preloadも故障注入で禁止。各100kのMemory/transcriptはlexical p50/p95 1.745/6.766ms、実働Mind semantic 10.738/11.576ms、transcript候補 5.160/5.383ms、store reopen 5.478ms、build 65.934s、Python allocation peak 365,064,033 bytes、index 150,904,832 bytes、Recall/restart rebuild 0。最終全suite 3055 passed / 2 expected Windows symlink skips。
- 2026-09-09 v3 amendment: generation 5の4表/Hamming-1はcosine .72〜.95のnomination率が受入thresholdと不整合だったためgeneration 8へ置換。full tierは14表×16-bit/Hamming-2、各表1280 source-sidecar行まで。canonical cosine .95以上を実際に確認できた場合だけ4表/Hamming-1・各表256行のfast tierで停止する。candidate Memory80/transcript40、context各5は不変。canonical vectorはquery dim一致、float32長、finite、non-zeroを検査してunit length化し、不正vectorを候補から除外する。benchmarkのrounds注記は実引数を正本とする。
- V3 evidence: fixed-seed 384次元1000組のshared ANN nomination率はcosine .72/.80/.90/.95/.99で956/992/1000/1000/1000。cosine .90の既知missをMemory/transcript両方の実働Mind経路で復旧。dim mismatch、truncated、NaN、Inf、zeroの混在poison候補を除外しvalid候補を保持。各100kのMemory/transcriptはlexical p50/p95 2.172/5.269ms、実働Mind semantic 7.374/8.229ms、transcript候補 4.074/4.349ms、store reopen 6.717ms、build 194.973s、Python allocation peak 370,912,905 bytes、index 320,561,152 bytes、Recall/restart rebuild 0。最終全suite 3059 passed / 2 expected Windows symlink skips。
- 2026-09-09 v4 amendment: sidecar候補IDは最終80/40へ切る前のnominationにすぎず、canonical hydrateでID/vector/ACL/status不一致を一件でも検出した場合は派生sidecar全体だけを一度再構築し、同じsemantic queryを最大一回再試行する。再試行後は無限修復せず、invalid-only exact hitはbounded full ANNへ進む。正常sidecarには再構築も追加probeも生じない。benchmarkはcosine .99985 fast tierとcosine .90 full tierを別名で測る。
- V4 evidence: approximate候補の先頭をinvalid Memory80件/transcript40件で占有して後方validを隠すfixture、invalid-only forged exactとvalid full-ANN候補のfixtureを両sourceでRED→GREEN。各100kの9-roundはfast Mind p50/p95 7.237/11.343ms、fast transcript 3.631/5.086ms、full(.90) Mind 207.135/225.459ms、full transcript 89.767/119.396ms。fast候補1/1、full候補80/40、context各1/上限5、source不変、Recall/restart rebuild 0。build 199.084s、Python allocation peak 370,895,578 bytes、index 320,561,152 bytes。最終全suite 3063 passed / 2 expected Windows symlink skips。
- 2026-09-09 v5 amendment: sidecar nominationは選択に使ったdoc/table/band/bucket provenanceを保持し、canonical vectorからexact hashとdense ANN bucketをbatch再導出する。shape上validでも別vector由来のbucketなら不一致として派生sidecarだけを一度repairする。repairはStoreの単一reentrant lockをclose→safe unlink→recreate/rebuildの全区間で保持し、並行public searchへ中間sidecarを公開しない。Windows `PermissionError`はsemantic空候補またはlexical fallbackへ変換し、canonicalを変更しない。benchmarkはtarget cosineと実経路/probe tierを明示する。
- V5 evidence: cosine -1のwell-formed canonical rowsをquery bucketへ偽装しMemory80/transcript40を占有する4ケース、parallel-search barrier、logical/malformed sidecarのPermissionErrorをRED→GREEN。各100kの9-roundはfast(.99985, 4-table-Hamming-1) Mind p50/p95 10.654/12.419ms、fast transcript 5.292/6.610ms、full(.90, 14-table-Hamming-2) Mind 375.196/392.688ms、full transcript 154.252/174.066ms。fast候補1/1、full候補80/40、context各1/上限5、source不変、Recall/restart rebuild 0。build 214.969s、Python allocation peak 370,916,986 bytes、index 320,561,152 bytes。最終全suite 3070 passed / 2 expected Windows symlink skips。
- 2026-09-10 v6 amendment: canonical raw vectorの検証・unit float32正規化を一度だけ行い、sidecar exact/dense bandsとprovenance再導出の双方をその同一表現へ束縛する。hydrate済みunitを再正規化しない。float32丸めにより`N(v) != N(N(v))`となる正常vectorをpoisonと誤判定して不要repairすることを防ぎつつ、well-formed wrong-vector由来bucketの検出は維持する。
- V6 evidence: seed55・384次元のMemory/transcript exact実経路は修正前に連続2回ともrepair、修正後はtarget取得・repair 0・source指紋不変。fixed-seed ANN gateとwrong-vector poison回帰を含むfocused 10、Stage M contract 58、関連185、全suite3072が成功。各100kの9-roundはfast(.99985) Mind p50/p95 9.541/10.374ms、fast transcript 5.399/6.115ms、full(.90) Mind 358.708/367.410ms、full transcript145.127/163.909ms。build208.847s、source不変、Recall/restart rebuild0、Python allocation peak371,034,394 bytes、index320,561,152 bytes。
- 2026-09-10 operations amendment: ユーザーの明示承認後、停止中に本番7 DBをSQLite online backupし、7件すべてintegrity OK、backup前後の件数とID・本文・summary・embedding指紋一致を確認した。隔離copyでschema migration＋generation 8 sidecarと既知Recallを検証後、本番schema/sidecarへ昇格した。canonical指紋不変、schema current、sidecar合計2,797,568 bytes、restart rebuild 0。証跡は`data/backups/memory_retention_stage_m/20260910_074937_preindex/{manifest,isolated_validation,production_promotion}.json`。同日、保存先volumeをcontent-freeに測る容量healthを追加し、既定20%未満をwarning、probe失敗をUNKNOWNとした。どちらも自動削除・自動write停止を行わず、共有Mind statusでLocal/Discordへ同じ結果を出す。
- Operations evidence: 容量healthは4 RED→GREEN。独立レビューで発見したUI null/UNKNOWN表示と未丸め20%境界も2 RED→GREENし、再レビューCritical/Important/Minor 0。fresh focused 67 passed / 1 expected skip、関連255 passed / 1 expected skip、全suite3078 passed / 2 expected Windows symlink skips。永続configと`memory.write_enabled`は変更していない。
- Consequences: Recall品質と高類似度query速度のためsidecarは大きくなり、各100k＋100kのbuildは同じsource lockを約209秒保持する。cosine .90 full tierの高衝突synthetic p95は367.410msで、14表×137 multiprobe、bounded candidate hydration、canonical provenance batch再導出の品質/integrityコストであり、一般semantic latencyや本番SLOではない。品質gateを暗黙に緩めず、実モデル分布で別評価する。現write-off運用の既知Minorで、snapshot/online rebuildへの大改造はStage Mへ混ぜない。毎turnのsource全件scan・全prompt投入には連動せず、sidecar/cacheは削除・再構築可能。RAM測定はOS RSS/SQLite/filesystem page cacheを含まず、9回p95は探索値。同じdirectoryへ書ける悪意あるownerがsidecar行とmetadataを一貫改変・削除する場合、検出不能な候補omissionの可用性までは保証しない。canonical ACL/status/vector/provenance再検査でconfidentiality/integrityを守り、OS directory ownershipと明示rebuildを残存防御とする。容量healthはoperator向けの観測であり自動回収を行わない。確認付き自然言語削除callerと本番実モデル分布の性能評価は残存する。永続`memory.write_enabled`は変更しない。
- Related ADR: ADR-0005、PROJECT_CONSTITUTION第11・12・17条。
- User approval: 2026-09-07の「容量が増えても記憶は保持」、2026-09-08の継続実装依頼、2026-09-10のStage M後日確認事項・backup/本番昇格・容量20%警告の明示承認。

## D-044 検索は共有Route/Outcome/Evidence契約で両surfaceへ渡す

- Date: 2026-09-07
- Status: ACCEPTED / CODE VERIFIED / HARDWARE PENDING
- Decision: 複合検索依頼を`SearchRouteDecision`へ一度だけ分類し、検索対象queryだけを既存の権限・実行ownerへ渡す。結果はquery-awareな`SearchOutcome`とhash付き`EvidenceSpan`へ変換し、決定的Presenterが抽出回答を作る。Local/Discordは意味論を共有するが、権限とtransportは共有しない。
- Consequences: BLOCKED/CLARIFY/DEFERRED/FAILED/EMPTY/CANCELLEDを通常生成と区別し、Web本文をAction/Tool指示として扱わない。一時Evidenceはpersona/surface/audience/epoch/TTLへ束縛し長期Memoryへ保存しない。query+fetchは7秒または設定の短い方へ束縛する。owner callbackを持つDiscord/shared executorではstale時に待機を早期解除して後続page/redirectを開始しない。Local Tool経路はstrict deadlineと既存のresponse/delivery gateを維持する。D-041のLLM要約禁止は維持。
- Evidence: synthetic 27ケース、Local/Discord fake統合、private/redirect/hash/stale/deadline/cancelのtargeted testと故障注入。2026-09-08最終全suiteは3014 passed / 1 expected Windows symlink skip。[監査](audits/2026-09-07_conversation-quality-p0-p2.md)。実機は未完。
- 2026-09-10 amendment: 実機LocalでWikipedia navigationが先に抽出され、BRIEFとcached ENDING follow-upの両方が同じnavigation文を発話したため、本文Evidence契約を強化した。HTMLはarticle-main/MediaWiki rootを優先し、fallbackはglobal nav listを使わない。navigation-only候補はtitle/query一致だけで選ばず、BRIEFは完全な支持文1〜2文・190文字以下とする。UI excerptとは別にprocess-localの有界支持本文をOutcomeへ保持し、現在routeのENDINGはそこから支持された結末spanだけを選ぶ。不足時は創作も自動再検索もしない。Local/Discordのreuseは同じ現在mode契約を使う。
- 2026-09-11 review amendment: Astra仕様レビューのImportant 4件を追加7 REDで再現。Discord TTSもLocalと同じtype/status＋文字数/hashのUI/log境界を使う。HTML stackはvoidを積まず一致closing frameまでunwindする。ENDINGはprimary relevant resultと同じresultだけに束縛し、meta言及を棄却してactual outcomeを優先する。
- 2026-09-11 verification: 現実的なWikipedia HTML、generic fallback、void/malformed/internal-nav、boilerplate ranking、BRIEF/ENDING/cross-result/meta、二重正規化、Local/Discord一回実行、Local/Discord TTS URL秘匿、`.sys`選択/折返しの新規19件をRED→GREEN。全suite3097 passed / 2 expected Windows symlink skips。実機再受入は未完。

## D-043 Local優先・Local LLM・文脈に合う強めのからかい・記憶保持

- Date: 2026-09-07
- Status: USER REQUIREMENTS CONFIRMED / STAGE M CODE VERIFIED / 会話人格はPLANNED
- Decision: Localを最優先、次にDiscord複数人を評価。ほどほどに活発、からかい多め、製作者へ強めの人格を目指すが、状況・関係・不快反応に追従する。速度と内容の整合性を優先し、会話LLMはLocal限定。現在の声を維持し、Fish Audioは検討候補。保存容量増を許容して記憶を保持し、検索/文脈量は有界にする。
- Consequences: クラウドLLM/外部judge案を除外。製作者のIdentityを表示名だけで推測しない。日数/件数による記憶削除は将来の保持policyと整合させる。明示削除とDO_NOT_STOREは維持。Memory書込みの本番再開・DB移行・音声切替は今回行わない。
- Evidence: 今回のユーザー回答、`mind/store.py::prune_transcripts/prune` と `mind/mind.py`の呼出し。現コードに自動削除経路があるが、実データの消失を調査したものではない。
- Plan: [Sol指示書](superpowers/plans/2026-09-07-conversation-quality-sol.md)。[ADR-0005](adr/ADR-0005-conversation-quality-recovery.md)に要件反映。実装方式は提案状態。

## D-042 会話品質は評価基準と検索経路から段階改善する

- Date: 2026-09-07
- Status: ACCEPTED / P0-P2 CODE VERIFIED; P3以降はPROPOSED
- Decision proposed: 既存の会話・感情・配送ownerを再利用し、P0評価→P1検索入口→P2根拠選択を先に閉じる。共通化・prompt・感情表現・性能は効果を比較して段階実装する。固定の品質点は未測定扱いとし、自然さは人間評価と区別する。
- Evidence: 複合依頼の検索判定false、本文よりbodyを選び160文字へ切る挙動を外部I/Oなしで再現。実機の自然さや最新モデル性能は未計測。
- Compatibility: D-041の抽出回答方針は維持。検索内容をメインLLMで要約する案は別途承認待ち。本番権限、設定、Memory書込み、人格成長の拡大は含めない。
- User authorization: Sol向け指示書作成・再レビュー。実装・モデル呼出し・本番昇格は実行していない。
- ADR: [ADR-0005](adr/ADR-0005-conversation-quality-recovery.md)。[実装計画](superpowers/plans/2026-09-07-conversation-quality-sol.md)。

## D-041 Grounded search evidence and DAVE terminal ownership

- Status: ACCEPTED / CURRENT
- Decision: A user-visible web explanation is extractive from a bounded, sanitised Evidence View, never an LLM completion from a title or raw page text. Local PROD Tool and Discord explicit search share that presentation boundary, while retaining their existing permission/transport ownership. Discord finalized inputs receive a monotonic epoch so newer input invalidates stale queued or generating work. DAVE music and TTS share one PCM writer with music ducked to 70%; DAVE leave is successful only after the sidecar confirms `left`.
- Evidence: `neuro_voice/cognition/search_result_presenter.py`, `neuro_voice/search/deepsearch.py`, `neuro_voice/pipeline.py`, `neuro_voice/discord_bridge/bot.py`, `neuro_voice/discord_bridge/direct_receiver.py`, `discord_receiver/receiver.js`, and focused regression tests.
- Consequence: Result quality is limited to retrieved evidence rather than invented detail; a source with no safe excerpt is honestly incomplete. A fresh Discord input may interrupt an older response. Music continues at reduced volume under speech. A DAVE disconnect/leave failure is visible rather than falsely marked successful.
- Related ADR: ADR backlog 5, Local/Discord common Response Runtime.
- User approval: 2026-08-07 request to ground search, preserve DAVE music with ducking, prioritize latest Discord input, and make leave reliable.

## D-040 Direct cognitive replies share terminal delivery ownership

- Status: ACCEPTED / CURRENT
- Decision: A deterministic direct reply (including a Phase 8 Tool Result Presenter) must not synthesize/play audio outside the same `_speak_loop` / TurnTracker / SpeakerPlayback coordinator used by normal conversation. One accepted SpeechRequest requires one logical playback session and terminal delivery evidence before a cognitive ActionOutcome can be `completed`. Missing session/segment is `failed`; incomplete physical segment is `interrupted`; finalizer failure is traceable and never causes Tool retry, Legacy fallback, or replay.
- Evidence: `neuro_voice/pipeline.py`, `neuro_voice/cognition/turn_tracker.py`, `neuro_voice/cognition/trace.py`, `tests/test_cognitive_outcome_payload.py`, `tests/test_turn_tracker_wiring.py`.
- Consequence: delivery completion can delay only turn closure, not the beginning of speech. Trace stores hashes/IDs and state only, never speech text.

## D-039 Phase 8 safe Tool probe is process-local and structured-result bounded

- Status: ACCEPTED / CURRENT
- Decision: The Phase 8 Local production-session probe exposes only the existing `web_search.search` READ_ONLY capability via a process-local `ToolRuntime` override. It does not write persistent feature flags, enable write effects, or apply to Discord. The response-required filter preserves this bounded explicit Tool candidate rather than discarding it for ordinary `ANSWER`. Once execution begins, Legacy fallback may not run or repeat it, and the runtime forces exactly one attempt. `SearchResultView` re-normalizes only title/URL/domain/rank metadata; snippets, HTML and raw Tool output do not enter an LLM or TTS. The deterministic presenter may show one cleaned title/URL in the UI and speak a grounded short result; Tool-returned instructions cannot control prompts or actions.
- Evidence: `neuro_voice/pipeline.py`, `neuro_voice/cognition/tool_runtime.py`, `neuro_voice/cognition/search_result_presenter.py`, `neuro_voice/cognition/trace.py`, `tests/test_tool_dialogue.py`, `tests/test_search_result_presenter.py`.
- Consequence: a single user-operated Local hardware probe is required before any Tool rollout broadening. A network failure or zero results produces one deterministic response, not fallback execution.

- Snapshot: 2026-07-26

## D-038 Phase 8 closure eligibility is a hard action constraint

- Status: ACCEPTED / CURRENT
- Decision: closure detection, response obligation, and action eligibility are distinct. On a closure-only turn, `ANSWER`, follow-up, and continuation are not score penalties but ineligible actions. The validator corrects an invalid selector result before planner/realizer without another LLM call or Legacy fallback. An explicit reply request on a closure turn is a one-sentence `BRIEF_ACKNOWLEDGE`; direct questions retain `ANSWER` eligibility.

## D-037 Phase 8 silence is a provenance-bearing outcome

- Status: ACCEPTED / CURRENT
- Decision: `silent_completed` is valid only for an Action Selector `REMAIN_SILENT` with an action decision ID, selector source, reason code, zero SpeechRequest, and completed turn closure. Only `ResponsePlan.requires_response_contract` (addressed answer/continuation intent) is frozen into CognitiveState; closure and Memory adjustments cannot select silence for that contract, while activity input and acknowledgement plans do not override a valid end signal. Missing silence provenance is a pre-effect technical fallback/failed outcome, not a successful silence.
- Evidence: `neuro_voice/pipeline.py`, `neuro_voice/cognition/kernel.py`, `neuro_voice/cognition/executor.py`, `neuro_voice/cognition/trace.py`, `tests/test_phase8_silence_contract.py`.
- Consequence: normal end signals remain eligible for intentional silence or brief acknowledgement, while a direct response plan cannot become a silent completion. Trace remains content-free.

## D-021 Phase 8 cognition rollout resolution is single-owner

- Status: ACCEPTED / CURRENT
- Decision: `CognitionRolloutResolver` is the only runtime authority for requested and resolved cognition rollout. `test_session` and `production_session` are process-local overrides; only `enabled=true` plus `rollout_mode=production` is persistent production. Unknown, legacy, and session modes written to config fail closed.
- Consequence: TurnMetrics/TurnFrame/Trace freeze one resolved decision and its epoch/source/fingerprint. A technical cognitive failure may use the same-frame Legacy fallback only once before tool, memory-write, SpeechRequest, or playback side effects.
- Evidence: `neuro_voice/cognition/rollout.py`, `neuro_voice/cognition/fallback.py`, `neuro_voice/pipeline.py`, `neuro_voice/discord_bridge/bot.py`.
- 形式: 決定、状態、根拠、影響

## D-001 単一メインLLM

- Status: ACCEPTED
- Decision: 一つのselected backendを会話主体とし、補助呼び出しはbounded taskとして扱う。
- Evidence: `neuro_voice/llm/factory.py`、`pipeline.py`、`discord_bridge/bot.py`。
- ADR: [ADR-0001](adr/ADR-0001-single-main-llm.md)

## D-020 ゲーム固有機能をフォルダ型プロファイルで管理

- Status: ACCEPTED / CURRENT
- Decision: ゲーム固有の表示名、役割、alias、映像方針、知識参照は`game_profiles/<game_id>/profile.json`を正本とし、実行可能コードはJSONから動的importしない。
- Evidence: `game_profiles/README.md`、`neuro_voice/games/profiles.py`、設定GUI。
- Consequence: 新しいゲームは一覧性を保って追加できる。Local/Discordは`GameProfileSessionManager`の同一意味論を使う。KTaNEのExpert roleでは画面を見ず、プレイ中のWeb検索を行わない。
- ADR Candidate: YES

## D-002 Canonical Stateを生成文より優先

- Status: ACCEPTED
- Decision: Activityと確定事実はcode ownerがvalidate/commitし、LLM文を状態保存先にしない。
- Evidence: `neuro_voice/activity/engine.py`、`dialogue/kernel.py`。
- ADR: [ADR-0002](adr/ADR-0002-canonical-state-and-generation-guards.md)

## D-003 世代IDでstale outputを破棄

- Status: ACCEPTED
- Decision: response IDをLLM/TTS/playback/PlayedTextへ伝播し、cancel済み結果を再生しない。
- Evidence: `realtime/playback_tracker.py`、`audio/playback.py`、local/Discord response code。
- ADR: ADR-0002。

## D-004 Discord direct DAVEをprimary receiveにする

- Status: ACCEPTED / CURRENT
- Decision: Node sidecarによるRTP/Opus/DAVE direct receiveを主経路とし、Windows loopbackはfallback扱い。
- Evidence: `config.yaml: discord.receive_mode`、`direct_receiver.py`、`receiver.js`。
- Consequence: Node/Python IPC contractとsidecar recoveryが必須。
- ADR Candidate: YES

## D-005 Legacy自発モードを標準無効

- Status: ACCEPTED / TRANSITION
- Decision: event/utility型AutonomousActionSystemを使い、random timer/proactiveを同時稼働させない。
- Evidence: `config.yaml`、`pipeline.py`、`autonomy/engine.py`。
- Consequence: legacy codeはmigration完了まで残す。

## D-006 自律研究はEvidenceを直接Factにしない

- Status: ACCEPTED
- Decision: Question、Run、Evidence、Knowledge、Reflectionを別recordにする。単一sourceはprovisional。
- Evidence: `research/service.py`、`research/store.py`。
- ADR Candidate: YES

## D-007 PrivacyはLLM投入前に適用

- Status: ACCEPTED / PARTIALLY_IMPLEMENTED
- Decision: output maskingだけでなくretrieval ACLを使う。
- Evidence: `privacy.py`。
- Gap: Memory schemaがowner/audience/scopeを完全保持しない。
- ADR Candidate: YES

## D-008 PersonaごとにMindデータを分離

- Status: ACCEPTED / CURRENT
- Decision: memory、personality、dialogue、relationship、temporal、activity、research、speakerをpersona別storeにする。
- Evidence: `mind/mind.py`。
- Consequence: persona switch時のresource close/reopenとTTS voice同期が必要。

## D-009 OBSをgame captureの推奨経路にする

- Status: ACCEPTED / FEATURE-FLAGGED
- Decision: 最大化/排他gameはOBS source screenshotを使い、silent desktop fallbackをしない。
- Evidence: `vision/obs.py`、`vision/video.py`、READMEの現行OBS章。
- ADR Candidate: YES

## D-010 WhisperをSTTの信頼できるdefault/fallbackとする

- Status: ACCEPTED / CURRENT
- Decision: Gemma native audio adapterがない現状ではfaster-whisperを使用する。感情認識は別module。
- Evidence: `stt/factory.py`、`emotion/recognizer.py`。

## D-011 TTS backendのuser-facing keyは`audio_output.backend`

- Status: CURRENT IMPLEMENTATION / DOCUMENTATION NEEDED
- Decision: factoryは`audio_output.backend`を優先し、`tts.backend`はcompatibility。
- Evidence: `tts/factory.py`。
- Gap: config/UIのcanonical migration未完了。

## D-012 一writer・handoff必須

- Status: PROPOSED FOR USER APPROVAL
- Decision: 同じtreeへの同時書き込みを禁止し、worktree/branchまたは承認済みlockを使う。
- Evidence: 複数AIによる同一local tree運用、実働Git不全。
- Consequence: 本憲法承認後に運用開始。

## D-013 憲法を最上位共通基準にする

- Date: 2026-07-26
- Status: ACCEPTED
- Decision: 最新ユーザー指示に次いで本憲法を設計判断の基準にする。変更はamendment proposalを通す。
- Evidence: 本文書作成指示、および所有者による憲法改正の明示承認。
- Consequence: `PROJECT_CONSTITUTION.md` version 1.1.0をACTIVEとし、CodexとClaude Codeは`AGENTS.md`経由で作業前に読む。

## D-014 継続依頼は専用エンジンでなく共通のBehaviorDirectiveで表す

- Date: 2026-07-26
- Status: ACCEPTED
- Context: 「しばらく話して」「終わるまで実況して」「静かに見守って」「一緒に考えて」「TRPGのGMをやって」が、いずれも1回の返答で終わっていた。素直に実装するとラジオ用、実況用、見守り用のエンジンが並ぶ。
- Decision: すべてを一つの`BehaviorDirective`として表し、`ContinuationController`が実行する。ラジオ専用・実況専用クラスを作らない。差はモデルが書いたフィールド（execution_mode / interaction_mode / constraints / stop_conditions）のみとする。
- Evidence: `neuro_voice/dialogue/directive.py`、`continuation.py`、`intent_plan.py`。`tests/test_behavior_directive.py`。
- Consequences: 新しい依頼の種類を足すのにクラス追加が不要。一方で`_MODE_GUIDE`が肥大しやすく、そこへ断定形の規則を書くとユーザーの依頼を上書きする危険がある（D-016）。
- Related ADR: なし（ADR化を推奨）
- User approval: 実装方針としてユーザー承認済み（2026-07-25）

## D-015 継続依頼の判断はMindが持ち、実行だけをサーフェスが担う

- Date: 2026-07-26
- Status: ACCEPTED
- Context: LocalとDiscordに同じ配線を複製すると必ずドリフトし、最初の症状は「同じ一文に別の結論を出す」形で現れる（R-003）。
- Decision: `ContinuationController`をMindが所有し、両サーフェスは`DirectiveRuntime` + `DirectiveHost`（speak / floor_busy / playback_ahead_s / run_tool / ready）だけを提供する。
- Evidence: `neuro_voice/dialogue/directive_runtime.py`、`tests/test_directive_runtime.py`のparityテスト。
- Consequences: R-003の縮小方向。ただしDiscord固有の事情（無人チャンネル、音楽混合の再生キュー）はhost側に残る。
- Related ADR: ADR backlog 5「Local/Discord共通Response Runtime」の部分実装
- User approval: 明示指示（2026-07-25「discordでの区間実行まで進めて下さい」）

## D-016 モード既定はユーザーの依頼に劣後する

- Date: 2026-07-26
- Status: ACCEPTED
- Context: TRPGでGMが物語内で行動する不具合を直す際、`_MODE_GUIDE`へ「あなたは物語の外にいる。自分用のキャラクターを作らない」と断定形で書いた。結果として「GM兼プレイヤーとしても参加して」という正当な依頼と矛盾する状態になった。ユーザーから「プログラム側で動きを固定すると柔軟性が欠ける」と指摘を受けた。
- Decision: モード指示は「既定の進め方」として提示し、依頼本文・style_constraints・content_constraintsが優先されることをプロンプトへ明記する。断定形の禁止事項は、破ると体験そのものが壊れる一点だけに絞る（NARRATIONなら「相手のキャラクターの行動を決めない」のみ）。加えて`plan_from_request`の雛形は素の依頼専用とし、条件付き・複合依頼はLLMへ渡す。依頼の原文を必ずプロンプトへ載せる。
- Evidence: `neuro_voice/dialogue/intent_plan.py`の`_MODE_GUIDE`・`_QUALIFIED`・`_template_can_express`。`tests/test_request_flexibility.py`。
- Consequences: 憲法第2条「人格表現を固定ルールだけへ押し込めない」に合致する。不具合修正のたびに規則を足す誘惑は残るので、レビュー時にこの判断を参照すること。
- Related ADR: なし
- User approval: ユーザー指摘に基づく修正（2026-07-26）

## D-017 ローカルLLMは直列である前提で補助呼び出しを設計する

- Date: 2026-07-26
- Status: ACCEPTED
- Context: 計画用LLM呼び出しを応答と並行実行したが、実機ログで`初トークン=1750ms`と`12087ms`が並存した。Ollamaは1リクエストずつ処理するため、並行化しても片方が待つだけで、体感は14.7秒のままだった。
- Decision: 補助LLM呼び出しは「増やしても隠せない」前提で設計する。素の依頼（「3分ラジオして」等）は決定的規則で計画を組み立て、モデル呼び出し自体をなくす。曖昧な依頼のみモデルへ渡す。
- Evidence: `logs/neuro_voice.log` 2026-07-26 06:17〜06:18。`neuro_voice/dialogue/intent_plan.py::plan_from_request`。
- Consequences: 憲法第3条の「補助LLM呼び出しは用途、予算、タイムアウト、キャンセル責任者を明示する」に加え、**そもそも呼ばない選択肢**を第一に検討する運用になる。
- Related ADR: なし
- User approval: 実機フィードバックに基づく（2026-07-26）

## D-018 会話理解と固有人格を最上位の品質目標として明文化する

- Date: 2026-07-26
- Status: ACCEPTED
- Context: 旧第1条は自然な音声会話と人格成長を掲げていたが、意図、指示対象、会話の流れ、経験が現在の判断へ参加すること、および外部参照目標の意味が分散していた。
- Decision: 発話を独立命令として扱わず、会話状態から解釈することを最上位目標とする。GPT Liveは音声対話品質、ネウロ様・Evilは固有で成長するキャラクター品質の参照とし、複製や同一実装を要件にしない。
- Evidence: 2026-07-26の所有者による趣旨確認と憲法改正指示。
- Consequences: 会話機能の設計・レビューでは、一問一答の正答率だけでなく、参照解決、意図理解、話題継続、人格の経験依存、自然なターンテイキングを評価する。
- Related ADR: なし。今後、会話状態ownerまたは評価基盤を長期固定する場合にADR化する。
- User approval: 2026-07-26

## D-019 沈黙を第一級の会話結果としてLLM前に決める

- Date: 2026-07-26
- Status: ACCEPTED
- Context: 一対一会話では宛先判定がほぼ常にrespondとなり、ユーザーが理解や相槌を示しただけでもLLMが説明・質問を追加していた。promptで「短く」と指示しても生成自体が始まり、一問一答の強制と長文化を止められなかった。
- Decision: `ConversationOrchestrator`配下の決定的`TurnClosurePolicy`が、final発話を`CONTINUE / SILENCE / BRIEF_ACK`へ分類する。終了相槌はLLM・検索・TTSを呼ばず、話題とfloorをsettleする。質問、訂正、依頼、実行確認、Activity入力は保護する。短い発話を返す場合は生成LLMを介さない。
- Evidence: `neuro_voice/dialogue/turn_closure.py`、`planner.py`、`state.py`、`tests/test_turn_closure.py`。
- Consequences: LocalとDiscordで沈黙の意味論が共通になる。規則ベースの誤沈黙リスクが生じるためfeature flag、保守的guard、replay corpusで管理する。
- Related ADR: ADR backlog 5「Local/Discord共通Response Runtime」の会話参加部分。
- User approval: 2026-07-26の「会話がここで終わる場合は、返答しない、又は簡単な応答のみ」という明示指示。

## D-020 ゲームプロファイルはフォルダを正本にし実行コードと分離する

- Date: 2026-07-26
- Status: ACCEPTED
- Context: Minecraft以外のゲームを追加すると、設定・知識・役割が巨大configとコードへ散在し、作成済みプロファイルを一覧できなくなる。
- Decision: `game_profiles/<game_id>/profile.json`を宣言データの正本とし、folder名とIDを一致させる。任意Python importは許さず、実行処理は既知IDに対してcode側で明示登録する。persona別sessionはLocal/Discordで共有する。
- Evidence: `game_profiles/`、`neuro_voice/games/profiles.py`、`neuro_voice/games/session.py`、`tests/test_game_profiles.py`。
- Consequences: ゲーム追加は一目で把握できる。KTaNEの決定的solverは別途必要であり、profileの存在を攻略精度の完成とみなさない。
- Related ADR: なし
- User approval: 2026-07-26のゲームプロファイルフォルダ作成とKTaNE専用プロファイルの明示指示。

## D-021 PortAudioの再初期化は停止検知後のidle recoveryに限定する

- Date: 2026-07-26
- Status: ACCEPTED
- Context: `AudioDeviceMonitor.poll()`が3秒ごとにPortAudioのterminate/initializeを実行し、動作中のマイク・スピーカーstreamを停止させた。実機ログではTTS開始1.7秒後に`Stream is stopped [PaErrorCode -9983]`となり、その後の入力・出力が無反応になった。
- Decision: 通常のdevice pollはread-onlyとする。process-global refreshは実stream停止を検知し、ユーザー発話・生成・再生がidleの時だけ行う。出力write停止はPCM位置を進めずstreamを再構築し、workerを終了させない。
- Evidence: `neuro_voice/audio/devices.py`、`capture.py`、`playback.py`、`pipeline.py`、`tests/test_local_audio_liveness.py`、`logs/neuro_voice.log` 2026-07-26 17:23。
- Consequences: 毎回の監視で新規機器cacheを強制refreshする挙動は廃止する。通常queryで検出できない新規機器は、既存stream停止後の明示recoveryで取り込む。
- Related ADR: なし
- User approval: 2026-07-26「マイクの音声が入らなくなった、音も聞こえない」の修正指示。

## D-022 子供っぽさを固定口調でなく文脈依存の人格軸として表す

- Date: 2026-07-26
- Status: ACCEPTED
- Context: ポッポへ子供っぽさ、人間味、質のよいジョーク、時折の生意気さが求められた。幼児語、固定語尾、毎回同じ自称やツッコミで実現すると、単調さと不自然さを増やし、深刻な相談でもふざける危険がある。
- Decision: `playfulness`、`cheekiness`、`spontaneity`を独立したpersona軸として追加し、Planner、HumorEngine、SurfaceRealizerへ段階的に反映する。無邪気さは好奇心と素直な反応、生意気さは親密度のある安全な雑談での軽い張り合いとして表す。support、correction、quiet、seriousでは抑制する。
- Evidence: `neuro_voice/dialogue/working_memory.py`、`conversation_planner.py`、`feature_engines.py`、`surface_realizer.py`、関連テスト。
- Consequences: 同じpersonaでも場面による落差を作れる。LLMの実際の表現頻度はモデルと会話履歴にも依存するため、実会話受入後に軸値の調整が必要。
- Related ADR: なし
- User approval: 2026-07-26「もう少し子供っぽさ、ジョークの質、たまには生意気な反応」の明示指示。

## D-023 継続発話の停止を開始判定より優先する取消トランザクションにする

- Date: 2026-07-26
- Status: ACCEPTED
- Context: 実機ログで「ラジオは一旦終わろう」がactive Directiveを止めず、「もうラジオは終わったよ」が`ラジオ`を含むため新しい5分MONOLOGUEを開始した。終了後もAutonomyがラジオ文脈を再利用して連続発話した。
- Decision: 明示停止をplan nomination/creationより先に判定する。停止時はController状態だけでなくpending plan、segment task、LLM generation、未再生audioを共通Runtimeで一括取消する。各surfaceは同じ停止通知でAutonomy taskとheartbeatを取消し、既定300秒はsilence/open-thread起点の発話を抑止する。
- Evidence: `logs/neuro_voice.log` 2026-07-26 17:48〜17:53、`intent_plan.py`、`directive_runtime.py`、`autonomy/engine.py`、関連回帰テスト。
- Consequences: Local/Discordの停止意味論が一致し、終了文が開始文へ反転しない。停止直後の自発性は意図的に抑えられるが、新しい明示Directiveは実行可能。
- Related ADR: ADR backlog 5「Local/Discord共通Response Runtime」。
- User approval: 2026-07-26の実機ログ・画像に基づく停止不具合の修正依頼。

## D-024 Discord画面共有はOBS transportから開始し会話sessionをtransport非依存にする

- Date: 2026-07-26
- Status: ACCEPTED
- Context: 現在のNode/Dysnomia DAVE sidecarは音声PCMと参加者eventだけをPythonへ渡し、Go Live映像を復号・転送する実装がない。一方、OBS current-frame基盤は実機で安定している。
- Decision: P0ではDiscord共有画面をOBSの専用sourceで取得し、`DiscordScreenShareSession`が開始・停止・source kind切替・時刻付き視覚contextを所有する。画面共有の指示がLocal micとDiscord direct音声のどちらから来ても同じsession意味論を適用し、Discordでは明示tool commandとactive requesterの視覚質問を通常addressee推定より優先する。Discord共有はMinecraft等の通常ゲームprofileを継承せず汎用1280pxで解析し、明示的な「OBSのマイクラの画面を見て」で通常ゲームOBS source/Minecraft profile/768pxへ切り替える。active中はmonitor captureへfallbackしない。開始直後のcold/background解析は会話の短いwait timeoutでcancelせず共有するが、current-screen turnは質問前のin-flight解析をcancelし、質問時に取得した新frameへ世代を固定する。明示frameへresponse/request IDを刻み、UIへは現在turnと一致するframeだけを出す。通常会話からqueued/latest frameを「いま見た画面」として再通知せず、明示取得失敗時は旧frameを破棄する。scene/OCR/state説明だけはstructured visionを直接短文化し、作成可否・現在の持ち物・攻略・次の一手はfresh観測と公式knowledgeを通常Conversation Planner/LLMへ渡す。同じresponse IDでvisionを二重起動せず、内部schema keyは発話しない。Minecraftのbackground観測はLocal/Discord共通で`GameCompanionDirector`へ渡し、意味ある変化だけを通常のcancellable発話経路へ送り、情景summaryの言い換えだけは許さない。安定recipe/how-toはactive Java JARの公式recipeで確定する調理法をLocal/Discord共通でvision/会話LLMより先に回答するが、現在の持ち物に基づく可否は視覚推論を優先する。ローカル映像設定とはconfig overlayで分離する。DAVE映像直接受信を実装する場合も、会話・記憶・停止処理を変更せずframe transportだけを置換する。
- Evidence: `logs/neuro_voice.log` 2026-07-26 19:41のOBS取得成功・初回vision 13007ms・2.5秒timeout、21:20〜21:23のscene summary直接TTSと内部key発話、`discord_receiver/receiver.js`、`neuro_voice/discord_bridge/direct_receiver.py`のaudio-only IPC、`neuro_voice/pipeline.py`のLocal routing、`vision/obs.py`と`vision/video.py`、今回関連72テスト、全体738 passed。
- Consequences: 今すぐ安定した共有画面会話が可能になるが、ユーザーはOBSでDiscord共有ウィンドウを捕捉する必要がある。OBSソースを非表示にするとフレームは得られない。選択sourceはUI previewのラベルと取得ログで確認できる。共有session開始後も明示的なゲームsource切替は可能で、停止までは通常monitor取得より優先される。背景認識は連続性を保ち、現在質問は質問時の証拠を優先するため、目的の異なるcancel policyを持つ。ゲーム相棒は静止/menu/duplicate/cooldownを沈黙させ、変化時だけ現在行動への反応や必要時の一手を返すが、精度はstructured visionのevent/助言品質に依存する。安定攻略質問は公式knowledge laneで初回実測約292msに回答文を確定できる。視覚根拠付き判断はvision一回と会話LLM一回を要するためscene説明より遅いが、問いへ答えず要約を読むことは避ける。12B visionの単一推論時間は残り、6.5秒を超えた場合は時刻付き直近状態へfallbackする。直接DAVE映像受信は別フェーズ。
- Related ADR: ADR backlog 1および4。
- User approval: 2026-07-26「画面共有見て、で共有された画面を画像認識」の実装合意。

## D-025 ためらいは演出でなく、実際の不確かさの表出とする

- Date: 2026-07-27
- Status: ACCEPTED
- Context: 「たまに言葉に詰まったり間を開けたりして人間に近い話し方をしてほしい。ただし機械的な実装ではなく、人間なら本当に悩む部分で出してほしい」という依頼。素直な実装は確率的なフィラー挿入だが、確信しているのに悩むフリをするのは能力の偽装であり、憲法第1条の`INVARIANT`が禁じている。同条は同時に「『覚えていない』を自然に言えることは品質の一部」とも述べており、本当の迷いが声に出ることは求められている。
- Decision: 仕草の許可は、既に他の目的で計算されている不確かさの信号（聞き取り確信度、参照解決、記憶ヒット数、検索判定）が立ったターンに限る。確信しているターンには明示的な禁止を出す。判定のための追加LLM呼び出しは行わない。文中のどこが迷いどころかはモデルが決め、間と話速はコードが音にする。
- Evidence: `neuro_voice/dialogue/hesitation.py`、`neuro_voice/tts/delivery.py`、`tests/test_hesitation.py`（確信時に出ないことを6件で固定）、`tests/test_delivery.py`。
- Consequences: 仕草の出る頻度は信号の出方に従属するため、実機で「少なすぎる」と感じられる可能性がある。その場合に閾値を下げるのは許容できるが、信号のない場所へ撒く方向へ倒すと本決定に反する。許可文へ「えっと」等の例示を書くと口癖化するため禁止。
- Related ADR: なし
- User approval: 2026-07-27（判定材料・仕草の種類・字幕の扱いを選択）

## D-026 永続的な応答長はsoft biasとし、反復回避は計測で昇格する

- Date: 2026-07-29
- Status: ACCEPTED / EXPERIMENTAL PART DISABLED
- Context: 一度の「詳しく」がUserModelの`response_length=long`として残り、その後の短い相槌や通常質問まで`long`へ固定していた。また、抽象的なCritic feedbackでは同じ書き出しへの収束を動かせなかった。
- Decision: 応答長は現在turnの明示要求、intent、発話量、momentumを優先し、永続値はsoft biasとしてだけ使う。一回限りの詳細要求をUserModelへ永続化しない。直近の実返答を使う反復回避は、既存`recent_replies`の冒頭だけをboundedなturn-local材料とし、追加LLM・新store・本文logを作らない。ただし自動A/Bで冒頭重複率が`0.25→0.25`だったため既定OFFとし、実会話で再現可能な改善が出るまで昇格しない。
- Evidence: `conversation_planner.py`、`user_model.py`、`surface_realizer.py`、`intelligence.py`、`plan_metrics.py`、`tools/check_reply_avoidance_ab.py`。全体1074 passed。
- Consequences: 短いturnが古い長文化設定に引っ張られない。実験機能は設定で比較可能だが、既定promptへ約93 tokenを追加しない。候補score・棄却候補・重複数値を削り、代表対話制御contextは約1833→1281 token。
- Related ADR: なし。
- User approval: 2026-07-29「それらを実施」の明示指示。

## D-027 会話記憶は現在の思考の根拠にし、過去回答を表現例として再生しない

- Date: 2026-07-29
- Status: ACCEPTED
- Context: 実GUIの8文受入で、同じ入力に対する過去transcriptが複数件意味想起され、ユーザー発話だけでなく過去assistant回答の冒頭まで毎回system contextへ入っていた。「なるほど」には同一の過去回答が5件あり、モデルは現在の返答を考えるより過去の表現を再現しやすくなっていた。同時に、通常文中の「話してても」がBehaviorDirective候補となり、一般会話を30分の継続DIALOGUEへ誤変換していた。
- Decision: 通常のsemantic transcript想起では、過去のユーザー発話を話題・経験の根拠としてだけ渡し、assistant文面は渡さない。同一ユーザー発話は正規化して一件へ畳む。「前に何を話した／何を勧めた」等、会話履歴そのものが質問対象の時だけ過去assistant文面を事実照合へ使う。継続依頼の裸の会話動詞は依頼形・文末を要求し、Directiveの手番待ちによるTurn Closure迂回は`NARRATION / CO_THINKING`へ限定する。
- Evidence: `intent_plan.py`、`mind.py`、実GUI turn 1768〜1775、関連統合238 passed、最終全体1086 passed / 2 skipped。
- Consequences: 記憶は残るが古い回答の言い回しを自己模倣しにくくなる。明示的な履歴質問の回答能力は維持する。一般DIALOGUEで短い相槌がLLM/TTSへ進む誤経路を閉じる。既存DBの削除・migrationは不要。
- Related ADR: なし。
- User approval: 2026-07-29「ではそれをやってみましょう」の明示指示。

## D-028 活動開始・自己経験・主会話行為をresponse_id付き契約で確定する

- Date: 2026-07-30
- Status: ACCEPTED
- Context: Activity名への言及だけで継続行動が始まる、取得できていない記憶や読書・視聴・プレイ経験を自分の経験として語る、Plannerが質問不要としてもモデルが末尾質問を足す、という三つの症状があった。いずれもpromptへ注意文を増やすだけでは、Local/Discordやターン境界で確実に守れない。
- Decision: Activity開始には名称でなくassistantへ向いた明示的な開始行為を要求する。既存`ConversationPlanner`は毎turn一つの主`conversation_move`を選び、質問は`ASK_QUESTION`の時だけ許可する。明示的な過去会話質問にはMemory/Transcript/Referenceの実取得有無を渡す。`ConversationContract`は同じ`response_id`・surfaceにだけ適用し、TTS直前とfinal UI/history確定前に、未計画質問と未根拠な記憶・自己経験主張を決定的に調整する。追加LLMや別会話エンジンは作らない。
- Evidence: `intent_plan.py`、`conversation_contract.py`、`conversation_planner.py`、`surface_realizer.py`、`intelligence.py`、`mind.py`、Local/Discord surface、関連122 passed、全体1111 passed / 2 skipped。
- Consequences: 名前を話題にしただけでActivityが始まらず、記憶・経験の正直さと質問方針が音声・UI・履歴で一致する。日本語表層の高確信規則なので、言い換えを完全には覆えず、実会話でfalse positive/negativeを継続観測する。streaming tokenはprovisionalで、final event時にcanonical textへ置換される。
- Related ADR: ADR-0002。
- User approval: 2026-07-29「では1~3の実装をして下さい」の明示指示。

## D-029 自発発話を履歴外イベントでなく会話のassistant turnとして扱う

- Date: 2026-07-30
- Status: ACCEPTED
- Context: 自発発話が毎回似た過去話題の確認になり、ユーザーが返してもポッポは自分の直前発話を履歴から参照できなかった。Localは自発生成時に会話履歴を捨て、Local/Discordとも生成本文を`ConversationManager`へ保存していなかった。さらに全context materialをLLMへ渡したため、毎回同じ顕著な項目を選んでいた。
- Decision: 自発生成の内部指示は一時情報として保持し、実際に読み上げた本文だけを通常のassistant turnへcommitする。候補選択はコード側で未使用focus一件とinitiative move一件へ絞り、同一・高類似focusを再利用しない。候補がなければ沈黙する。過去open-thread callbackは既定OFFとし、現在文脈に根拠がある場合だけbudget内で相手へ一つ質問できる。
- Evidence: `autonomy/engine.py`、`memory/conversation.py`、Local/Discord surface、`tests/test_autonomy.py`、`tests/test_autonomous_conversation_bridge.py`。全体1116 passed / 2 skipped。
- Consequences: 自発質問への返答が同じ会話として継続し、同じ独り言を候補がある限り繰り返す経路を閉じる。発話頻度は以前より下がり得るが、根拠のない発話より沈黙を優先する。surface別instanceは残るため、意味論を共通テストで維持する。
- Related ADR: ADR-0002。
- User approval: 2026-07-30「脈絡のない独り言ではなく、相互に質問や気になることを伝える対話」の明示指示。

## D-030 明示訂正は割り込み保留より優先し、遮られた誤回答は再開しない

- Date: 2026-07-30
- Status: ACCEPTED
- Context: AI発話中のユーザー訂正では、遮られた誤回答が通常履歴へ未commitのままdeferred contextへ入り、同じ回答を再生成しても既存の重複guardが検出できなかった。
- Decision: 明示的な意味訂正を構造化し、直前の解釈を置換する場合はその割り込み保留を無効化する。遮られたassistant本文は会話事実や履歴にせず、直後の重複検知証拠として有界保持する。Local/Discordの両方で、通常履歴・割り込み本文・現在のユーザー発話に対して生成冒頭を検証する。fallbackは実際の検索実行有無と訂正事実に合わせる。
- Evidence: `neuro_voice/dialogue/repair.py`、`neuro_voice/memory/conversation.py`、`tests/test_conversation_repair.py`。
- Consequences: 同じ誤回答の再生と、訂正turnでの無関係な検索失敗文を防げる。暗黙訂正の範囲を広げる時はfalse positive回帰が必要。
- Related ADR: なし。
- User approval: 会話が噛み合わず全く同じ回答をした実会話報告に基づく（2026-07-30）。

## D-031 返答本文を多重生成せず、Move候補競争・反応学習・連想・表現を共通化する

- Date: 2026-07-30
- Status: ACCEPTED
- Context: 話題候補とfeature候補は存在したが、直接回答・意見・短い反応・遊び・話題展開・質問という返し方自体は一つのutilityで比較されていなかった。任意featureが発話へ反映された時だけ学習するため、同じ返し方への不満もMove選択へ届かなかった。身体表現と配信コメントは現時点の主対象ではない。
- Decision: 既存`ConversationPlanner`内で`ConversationSelectionKernel`が複数Moveを決定的に比較し、選択後のメインLLM呼び出しは一度だけとする。次のユーザー反応からMove重みを小幅・有界に更新する。意味連想graphは事実DBと分離し、group由来はruntime session内だけにする。voice/avatarは`UnifiedExpressionPlan`へまとめるが、avatar sinkはNo-opとする。配信commentは型とprotocolだけを置き、現在は接続しない。
- Evidence: `selection_kernel.py`、`reaction_learning.py`、`semantic_graph.py`、`expression_plan.py`、`audience.py`、Local/Discord event統合。focused 223 passed、全体1130 passed / 2 skipped。
- Consequences: 返し方の変化に理由と観測可能性を持たせつつ、候補ごとのLLM生成による遅延を避ける。Implicit reactionと単純topic edgeには誤帰属の余地があるため、強い学習や事実確定には使わない。Avatar/配信は将来adapterを接続するまで何も実行しない。
- Related ADR: ADR-0003。
- User approval: 2026-07-30「優先順位を上から順に全て実装」「身体表現は機能だけ」「直接一対一とDiscord groupを重視」の明示指示。

## D-032 自律研究はgrounded producerから理由付き課題を作り、表現metadataは本文外に置く

- Date: 2026-07-30
- Status: ACCEPTED
- Context: 自律研究はqueue consumerだけが実装され、明示的な「あとで調べて」以外に課題producerがなかった。履歴APIはあるがGUIから到達不能だった。同時に、UnifiedExpressionPlan導入後もpersona promptが感情tag出力を要求し、未知labelが本文/TTSへ漏れた。関係性質問では期待されそうな強い好意へ迎合する例も出た。
- Decision: Research producerはLLMへtopicを自由生成させず、(1) active personaに明記された公開の興味、(2) 一対一会話で事実質問へ実際に非回答した知識gap、(3) 明示的な後調べ依頼、に限定する。全候補は通常のprivacy/risk/quota/duplicate gateを通し、persona別履歴へreason codeを保存・GUI表示する。自発発話ON/OFFは発話policyだけを変え、Heartbeat schedulerと裏の研究を停止しない。感情/styleの正本は`UnifiedExpressionPlan`とし、本文へtag出力を要求しない。legacy型付きtagは未知値も除去する。応答設計は温かさと同意を分け、関係状態より親密さを盛らない。
- Evidence: `research/service.py`、`mind/mind.py`、`pipeline.py`、`discord_bridge/bot.py`、`ui/assets/index.html`、`memory/persona.py`、`dialogue/response_director.py`、`utils/emotion.py`、`utils/textseg.py`。最終全体1140 passed / 2 skipped。
- Consequences: 検索には発生理由と見える履歴ができ、沈黙だけを理由に無制限なtopicを発明しない。ユーザーが独り言を望まず自発発話をOFFにしても、公開興味の裏調査は継続する。広いpersona興味の検索品質は外部検索へ依存し、単一sourceは暫定Knowledgeのまま。出力guardは明確なmetadata/限定英語語彙だけを扱い、意味判断をregexで全面置換しない。
- Related ADR: ADR-0004。
- User approval: 2026-07-30の非迎合、自発検索未稼働、履歴不在、tag/英語漏出の明示修正依頼。

## D-033 内容を加えない口語同意は会話を閉じ、短い導入の後ろまで反復検査する

- Date: 2026-07-30
- Status: ACCEPTED
- Context: 「ほんとそれ。」が終了相槌として認識されずLLMへ進み、モデルは最初の「あはは、確かに！」を「あはは、やっぱり！」へ変えただけで、その後の説明をほぼ同文で再生成した。既存Echo guardは最初の短文が8文字未満の実質内容だったため非重複としてstream全体を解放し、後続を検査できなかった。
- Decision: AI発話直後で、質問・訂正・依頼・Activity・実行確認を含まない「ほんとそれ / マジでそう / まさにそれ」等はTurn Closureで`SILENCE`とする。生成へ進んだ場合、Echo guardは比較不能な短い導入に限り最大もう一文を保留し、結合文または各実質文を直前assistant発話・ユーザー発話と比較する。重複なら表示/TTS前に抑止し、新内容なら保留分を解放する。追加LLMや全文生成待ちは導入しない。
- Evidence: `turn_closure.py`、`echo.py`、`tests/test_turn_closure.py`、`tests/test_reply_echo.py`。focused 193 passed、全体1143 passed / 2 skipped。
- Consequences: 内容のない同意へ義務的な返答をせず、短い感情表現だけを変えた自己反復も止められる。短い導入に新内容が続く時だけ、TTS開始が最大一文ぶん遅れる。口語同意の網羅性と意味的な長距離言い換えは実会話fixtureで継続評価する。
- Related ADR: ADR-0002。
- User approval: 2026-07-30の同じ台詞を続けて言う実会話画像に基づく修正依頼。

## D-034 重複判定は発話元を分け、抑止後は固定謝罪でなく一回だけ会話を修復する

- Date: 2026-07-30
- Status: ACCEPTED
- Context: Echo guardは真の直前返答ループを止めていた一方、直近4件のassistant発話とuser発話を同じ閾値で比較していたため、過去に使った短い定型導入や短い聞き返しを重複と誤判定した。抑止後は毎回同じ診断謝罪をTTSへ渡すため、防いだ反復の代わりに別の反復が目立った。
- Decision: 比較証拠を`assistant`自己反復と`user`オウム返しに型分離し、user側は高い一致率と十分な長さを要求する。短い導入だけでは確定せず、assistant証拠は直近2件を上限とする。真の重複候補はUI/TTS前に止める。明示訂正はcodeで短く確定し、通常turnは同じmessagesへ内部repair指示を加えて一回だけ再生成する。再生成は新内容1〜2文または`NO_REPLY`に限定し、再度重複なら沈黙する。診断にはkind/scoreだけを残し、会話本文を資料へ複製しない。
- Evidence: `dialogue/echo.py`、`dialogue/repair.py`、`pipeline.py`、`discord_bridge/bot.py`、`tests/test_reply_echo.py`、`tests/test_conversation_repair.py`。focused 46 passed、全体1148 passed / 2 skipped。
- Consequences: 通常turnで固定の「重複しかけた」謝罪を読まず、質問・訂正には復旧機会を残し、自然な終了には沈黙を選べる。重複発生時だけ追加1回分のLLM latencyが生じる。通常turnの初回応答速度は変わらない。
- Related ADR: ADR-0002。
- User approval: 2026-07-30の重複謝罪が頻発する実会話画像とログ調査依頼。

## D-035 Remote Launcherの通常入口はwscriptのwindow style 0とする

- Date: 2026-07-31
- Status: ACCEPTED
- Context: 通常用batchは`pythonw.exe`不在時に`python.exe`へフォールバックし、そのconsoleがランチャー常駐中ずっと黒い画面として残った。Startup shortcutもbatchを直接targetにしていたため、最小化設定では完全非表示にならなかった。
- Decision: Windows通常入口を`RemoteLauncher.vbs`とし、venvの`pythonw.exe`または`python.exe`を`WScript.Shell.Run`のwindow style 0で非同期起動する。通常batchは互換用の即時委譲だけとし、Startup shortcutは`wscript.exe //B //Nologo`を直接targetにする。エラー調査でconsoleが必要な場合だけ`RemoteLauncher_Console.bat`を明示使用する。
- Evidence: `RemoteLauncher.vbs`、`RemoteLauncher.bat`、`InstallAutoStart.bat`、`tests/test_remote_launcher.py`。既存実機shortcutのtarget/argumentsを更新後に再読して確認。focused 21 passed、全体1151 passed / 2 skipped。
- Consequences: pythonwがない環境でも黒いconsoleを残さない。通常時の標準出力は見えないため、診断はtray、`logs/remote_launcher.log`、または明示console入口を使う。
- Related ADR: なし。
- User approval: 2026-07-31「コマンドプロンプトは表示されないようにして」の明示指示。

## D-036 TTSの読点はsoft boundaryとし、単一styleモデルのweightを固定する

- Date: 2026-07-31
- Status: ACCEPTED
- Context: SBV2の発音が以前より不自然という報告を調査すると、Local/Discord共通segmenterが8文字以上の全読点を文末同様に確定し、短い節ごとにSBV2が抑揚を閉じ直していた。実モデル情報ではtsukuyomiは`Neutral`一つだけだが、継続感情のintonation値をstyle weightへ掛けてturnごとに単一style vectorも変動させていた。
- Decision: 句点等はhard boundaryのまま、読点は十分な文脈長を得た後だけ使うsoft boundaryとする。利用可能styleが一つだけならstyle weightは設定基準値へ固定し、複数styleモデルだけ従来の感情強度倍率を使う。どちらも設定・APIでrollback可能にし、診断ログへ実speed/length/固定状態を残す。
- Evidence: `logs/neuro_voice.log`の短い読点chunk、`/models/info`のtsukuyomi `style2id={Neutral:0}`、`utils/textseg.py`、`tts/style_bert_vits2.py`、追加回帰テスト。自動テスト実行は環境上限により未完。
- Consequences: 短い節の独立した語尾化が減り、単一style声質のturn間揺れを抑える。読点からTTS開始する最短時間は一部伸びるが、句点とhard ceilingは維持するため無制限には待たない。複数styleモデルの表現力は変えない。
- Related ADR: なし。
- User approval: 2026-07-31「SBV2の発音が前より違和感が強い」という実機報告に基づく修正依頼。

## ADR backlog

1. Discord direct DAVE primaryとfallback条件。
2. Privacy ACLとMemory schema v2。
3. 自律研究の追加producerとEvidence model拡張（基本境界はADR-0004で決定済み）。
4. OBS current-frame policyとGPU priority。
5. Local/Discord共通Response Runtime。
6. Config canonical namespace。

## 決定追加テンプレート

```text
## D-NNN Title
- Date:
- Status: PROPOSED / ACCEPTED / SUPERSEDED / REJECTED
- Context:
- Decision:
- Evidence:
- Consequences:
- Related ADR:
- User approval:
```
