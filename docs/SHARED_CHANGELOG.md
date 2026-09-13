# Codex・Claude 共通変更台帳

## 2026-09-11 00:05 — Sol — 検索Evidence品質・follow-up mode・TTS/UIエラー表示の本番回帰修正

- Local本番2発話で確認されたWikipediaナビゲーション読み上げ回帰を、先に12件の受入テストへ固定した。REDは当初`10 failed / 1 passed`（後続のTool境界再正規化fixtureも意図どおりRED）で、全文HTMLの`li`優先、titleだけの関連判定、360文字切断、cache側mode固定、TTS例外URL露出、`.sys`選択不可をそれぞれ再現した。
- `DeepSearch`は`main`/`article`/`role=main`/MediaWiki本文rootを優先し、nav/header/footer/aside/script/style系を除外する。generic fallbackは見出し・段落・定義本文を優先し、全体navの`li`で上限を埋めない。共有Presenterはnavigation-only候補を根拠として棄却し、BRIEFを完全な事実文1〜2文・190文字以下に限定する一方、process-local outcomeには追質問用の有界な支持本文を保持する。Local/DiscordのREUSE_EVIDENCEは現在のroute modeを適用し、ENDINGは支持された結末文だけを返し、なければ不足を明示する。再検索・Tool再実行はしない。
- TTS失敗はUIへ例外型と取得可能なHTTP statusだけを出し、ログは例外型/status・文字数・hashだけにした。全文URL/query/発話/response bodyはUI・ログへ出さない。top-level UIの`.sys`は`textContent`を維持したまま選択・コピー可能、pre-wrap/anywhere折返し・幅上限を持つ。
- Astra仕様レビューはCritical 0 / Important 4。Discord TTSログの同じprivacy漏洩、void/malformed HTMLで壊れるparser stack、無関係resultからのENDING借用、`結末が有名`というmeta文の誤採用を、追加7件すべてREDで再現した。tag-match unwind＋void非stack化、ENDINGをprimary relevant resultへ束縛、meta cue棄却とactual outcome優先、DiscordのLocal同等safe error境界で全件GREENへ修正した。
- 新規受入は合計`19 passed`、focused `222 passed / 1 expected Windows symlink skip`、関連`447 passed / 1 expected skip`、py_compile成功、全suite `3097 passed / 2 expected Windows symlink skips in 130.18s`、failed 0。本番DB/config/persona/既存Trace/backup/nested stale copyは不変。実機TTS・Discord・外部検索は未実施。UIは最強の利用可能なstatic契約testまででbrowser操作は未実施。[handoff](handoffs/2026-09-10_2345_sol-search-evidence-ui-fix.md)。

## 2026-09-10 08:48 — Astra / Sol — Stage M本番昇格・容量警告の最終検証

- ユーザー承認後の本番7 DB backup・隔離検証・generation 8 schema/sidecar昇格を独立再確認。Memory 232件、transcript 1,142件の件数とID・本文・summary・embedding指紋は不変、全DB integrity OK、sidecar合計2,797,568 bytes、restart rebuild 0、既知Recall成功。証跡は`data/backups/memory_retention_stage_m/20260910_074937_preindex`。
- 容量警告の独立レビューでUNKNOWNの`null`がUIで0.0%になる問題と、19.999%を丸めて20.0%/OKにする境界問題を検出。Solが実JS描画と未丸め比率判定でRED→GREEN修正し、再レビューはCritical/Important/Minor 0。
- fresh検証はfocused `67 passed / 1 expected skip`、関連`255 passed / 1 expected skip`、py_compile成功、全suite `3078 passed / 2 expected Windows symlink skips in 116.74s`、failed 0。永続config/`memory.write_enabled`/persona/既存Traceは不変、実機・Discord・外部検索は未実施。[handoff](handoffs/2026-09-10_0848_astra-stage-m-production-final.md)。

## 2026-09-10 08:18 — Sol — Stage M 本番昇格記録と容量20%警告

- ユーザーの明示承認後、停止中（`python`/`pythonw`なし、全mind DB排他読取可）に7個の`mind_*.db`をSQLite online backupし、`data/backups/memory_retention_stage_m/20260910_074937_preindex`へ保存した。全7 DBはintegrity OKで、backup前後の件数およびID・本文・summary・embedding指紋が一致した。隔離copyではschema migration＋generation 8 sidecarを作り、raw/unit/cosine .90既知vectorを各2回Recallして候補Memory80/transcript40・context5・persona/status漏れ0・rebuild 0/0・backup不変を確認した。
- 同じ承認範囲で本番7 DBへ`memory_search_state`＋triggers（必要な一部DBはreflection persona/scopeも）をmigrationし、generation 8 sidecarを作成した。canonical指紋は不変、schema current、sidecar合計2,797,568 bytes、restart rebuild 0、既知Recall成功。証跡はbackup内`manifest.json`、`isolated_validation.json`、`production_promotion.json`。過去の`PRODUCTION_DATA_UNTOUCHED`記録は各時点の履歴として保持する。
- `MemoryStore.storage_health()`へcanonical/data/backup/sidecar所在volumeのcontent-free容量診断を追加した。既定20%未満を`warning`、測定失敗を`unknown`とし、空き率・閾値・測定先・対象label・失敗数を共有`Mind.status()`からLocal/Discordへ同じ意味で公開する。「こころ」画面にも表示する。`mind.storage.warning_free_percent`で閾値を変更できるが、現在の永続configと`memory.write_enabled`は変更していない。低容量/UNKNOWNでも自動削除・自動write停止は行わない。
- TDDは新規4 RED→4 GREEN、factory回帰を含むfocused 5 passed、Stage M関連192 passed / 1 expected Windows symlink skip、py_compile成功、全suite `3076 passed / 2 expected Windows symlink skips in 124.43s`。容量警告実装では本番DB/config/persona/過去Trace/backup/既存sidecarを変更せず、実機・外部I/Oも実行していない。[handoff](handoffs/2026-09-10_0818_sol-memory-capacity-warning.md)。

## 2026-09-10 02:10 — Sol — Stage M v6 canonical vector単一正規化

- seed55・384次元のvectorで`N(v) != N(N(v))`となるfloat32境界をMemory/transcriptのexact実経路へ追加し、正常sidecarでも連続queryごとにlogical repairするREDを両sourceで再現した。原因はbuild時にcanonical rawを一度正規化してhash/bandを作る一方、hydrate後のunit float32をprovenance検証時に再正規化していたこと。
- canonical rawを検証・正規化するownerを一箇所にし、sidecar exact/dense bandsとprovenanceの双方を同じ一度だけ正規化済みunit float32から導出するよう統一。連続2回のMemory/transcript exact Recallはtarget取得、repair 0、source指紋不変。wrong-vector poison検出とfixed-seed ANN Recall gateも維持した。
- 最終100k＋100k・9-roundはlexical p50/p95 2.174/6.821ms、fast(.99985) Mind 9.541/10.374ms・transcript 5.399/6.115ms、full(.90) Mind 358.708/367.410ms・transcript 145.127/163.909ms。build208.847s、sidecar320,561,152 bytes、Python allocation peak371,034,394 bytes、source不変、Recall/restart rebuild 0/0。
- focused `10 passed`、Stage M contract `58 passed / 1 expected Windows symlink skip`、関連 `185 passed / 1同skip`、py_compile成功、全suite `3072 passed / 2 expected skips in 124.18s`。本番DB/config/persona/過去Trace/永続write設定/実機/外部I/Oは未変更。[handoff](handoffs/2026-09-10_0210_sol-memory-stage-m-single-normalization.md)。

## 2026-09-09 22:02 — Sol — Stage M v5 provenance検証・atomic repair

- shape上validなcosine -1 canonical vectorをqueryのapproximate bucketへ偽装してMemory80/transcript40を占有するケースと、wrong-vector exact hitがfull ANNを隠すケースを両sourceでRED化。選択sidecar rowのband/bucket/exact provenanceを保持し、canonical vectorからdense bucket/hashをbatch再導出。不一致は派生sidecar全体だけを一度repairして同queryを最大一回再試行する。
- repairのclose→safe unlink→recreate/rebuildをStoreの単一reentrant lock/owner区間へ収束し、並行public searchが中間sidecarをopenする競合をbarrier故障注入でRED→GREEN。logical/malformed sidecarのWindows `PermissionError`をsemantic空候補またはlexical fallbackへ変換し、canonical sourceを維持した。
- benchmarkは`fast_target_cosine=.99985`/`4-table-hamming-1`と`full_target_cosine=.90`/`14-table-hamming-2`、実経路`Mind._recall_semantic`を明示。各100k＋100kの9-roundはfast Mind p50/p95 10.654/12.419ms、fast transcript 5.292/6.610ms、full Mind 375.196/392.688ms、full transcript 154.252/174.066ms、source不変、rebuild0/0。build214.969s、sidecar320,561,152 bytes、Python allocation peak370,916,986 bytes。
- Stage M contract `56 passed / 1 expected Windows symlink skip`、関連 `183 passed / 1同skip`、py_compile成功、全suite `3070 passed / 2 expected skips in 134.89s`。同権限の悪意writerによる検出不能なsidecar omissionは可用性保証外と明記。本番DB/config/persona/過去Trace/永続write設定/実機/外部I/Oは未変更。[handoff](handoffs/2026-09-09_2202_sol-memory-stage-m-provenance-atomic-repair.md)。

## 2026-09-09 21:20 — Sol — Stage M v4 logical-poison修復とtier別実測

- logical poisoned sidecarがfinal cap前にIDを占有し、valid後方候補を消す穴をMemory80/transcript40でRED化。invalid-only forged exact hitがfull ANNを抑止する穴も両sourceで再現した。canonical hydrateのID/vector/ACL/status不一致時に派生sidecar全体だけを一度再構築し、同queryを最大一回再試行する境界へ収束。canonical不正vectorは再構築時に除外し、正常sidecarの追加probe/rebuildは0。
- synthetic benchmarkをcosine .99985 fast tierとcosine .90 full tierへ分離。各100k＋100kの9-roundはfast Mind p50/p95 7.237/11.343ms、fast transcript 3.631/5.086ms、full Mind 207.135/225.459ms、full transcript 89.767/119.396ms。fast候補1/1、full候補80/40、context各1/上限5、source不変、Recall/restart rebuild 0。build199.084s、sidecar320,561,152 bytes、Python allocation peak370,895,578 bytes。full値は14表×137 multiprobeと高衝突fixtureでRecall gateを守る品質コストとして明記し、一般semantic latency/SLOとはしていない。
- logical poison 4 RED→4 GREEN、Stage M contract `49 passed / 1 expected Windows symlink skip`、Memory関連 `176 passed / 1同skip`、py_compile成功、全suite `3063 passed / 2 expected Windows symlink skips in 125.51s`。本番DB/config/persona/過去Trace/永続write設定/実機/外部I/Oは未変更。build lock、確認付き自然言語削除UX等は既知残存。[handoff](handoffs/2026-09-09_2120_sol-memory-stage-m-logical-poison-close.md)。

## 2026-09-09 09:21 — Sol — Stage M v3 ANN品質・vector安全性の最終調整

- v2の4表/Hamming-1をfixed-seed 384次元1000組で再現し、cosine .72のnomination 268/1000とcosine .90のMind Memory/transcript 0件をRED化。full tierを14表/Hamming-2へ変更し、.72/.80/.90/.95/.99を956/992/1000/1000/1000へ改善した。canonical cosine .95以上を確認した時だけ4表/Hamming-1 fast tierで停止し、sidecar row上限1280/fast256、Memory候補80、transcript40、context各5、全件scan禁止を維持。
- poisoned sidecar後もcanonical vectorのquery dim一致、float32長、finite、non-zeroを検査しunit length化。不正vector 90件がexact bucket先頭を埋めても内部最大1280の有界overfetch→検証→最終80/40で後方valid候補を残す。benchmarkのroundsと注記を実引数連動へ修正。
- Memory/transcript各100kの9-round探索値はlexical p50/p95 2.172/5.269ms、実働Mind semantic 7.374/8.229ms、transcript候補 4.074/4.349ms、store reopen 6.717ms、rebuild0/0。品質の代償として派生buildは194.973秒、sidecar約320.6MB、Python allocation peak約370.9MB。同じsource lockをbuild中保持する点を既知Minorとして明記。
- Stage M contract `45 passed / 1 expected Windows symlink skip`、関連 `119 passed / 1同skip in 17.38s`、py_compile成功、全suite `3059 passed / 2 expected Windows symlink skips in 124.26s`。本番DB/config/persona/過去Trace/永続write設定/実機/外部I/Oは未変更。自然言語削除UX等の既知残存は維持。[handoff](handoffs/2026-09-09_0921_sol-memory-stage-m-final-tuning.md)。

## 2026-09-09 03:40 — Sol — Stage M final-review修正

- sampled/quantized semantic bandの高cosine境界false-negativeとbucket衝突順依存を実動RED化し、全384次元を使うdeterministic dense random-hyperplane ANN（4表、exact＋Hamming距離1、schema generation 5）へ置換した。Memory候補最大80、transcript候補最大40、最終context各5の実働`Mind`経路へ配線し、起動時の旧Memory/transcript全embedding RAM preloadも除去。canonical persona/scope/status/vector再検査を維持。
- valid SQLiteのTABLE/VIEW不正schemaはconnectionを閉じ、canonicalへ触れず派生sidecarファイル全体だけを一度unlink/recreateする。path/link/file identityをopen前後とDDL前に検査。完全migration済みquery-only DBはretrieval-only startup、migration必須DBはtyped error＋connection closeにした。
- production-limit benchmarkをMemory/transcript各1k/10k/100k、9 roundsで再測定。各100kはlexical p50/p95 1.745/6.766ms、実働Mind semantic 10.738/11.576ms、transcript候補 5.160/5.383ms、store reopen 5.478ms、派生build 65,934.354ms、Python allocation peak 365,064,033 bytes、sidecar 150,904,832 bytes、Recall中/touch後restart rebuild 0。9回p95は探索値、RAMはOS RSS/SQLite/filesystem page cacheを含まず、reopen値はembedderモデルloadを含まない。
- Stage M＋関連 `115 passed / 1 expected Windows symlink skip`、py_compile成功、最終全suite `3055 passed / 2 expected Windows symlink skips in 112.58s`。本番DB/config/persona/過去Trace/永続`memory.write_enabled`/外部I/O/実機は未変更。確認付き自然言語削除UX、容量/backup監視、全文transcript policy、悪意ある同directory同時writerの完全防御は残存。[handoff](handoffs/2026-09-09_0340_sol-memory-stage-m-final-review.md)。

## 2026-09-09 02:41 — Sol — Stage M 独立レビュー修正

- 独立レビュー7項目を実コードで13 REDとして再現し、`memory.write_enabled=false`のEpisodic/Mind想起を完全read-only化。`touch/reinforce/mark/upgrade`を共通rollback→process read-only境界へ収束し、書込可否判定もlock内へ移した。
- mtime/source署名をfreshness正本から外し、canonical DBのindex対象列だけで進む永続revision triggerへ置換。touch後の連続Recallと再起動は再構築0、insert/status/deleteは再構築、非index対象upgradeは再構築0を確認した。
- FTS5に加え、exact＋4 sampled/quantized bandのbounded semantic候補sidecarを`Mind._recall_semantic`実経路へ配線。候補後はcanonical persona/scope/statusを再検査する。破損/不正schemaは派生schemaだけ一回修復し、hardlink/symlink/path escapeから別DBを保護する。
- synthetic実vector 1k/10k/100kを9回測定。100kは通常query p50 1.497ms / p95 4.494ms、semantic p50 4.331ms / p95 4.521ms、startup 5.634ms、index build 36,417.474ms、Python allocation peak 26,701,179 bytes、sidecar 92,069,888 bytes、Recall中/touch後restartのrebuildはいずれも0。RAM値はOS RSSとSQLite/filesystem page cacheを含まず、9回p95は探索値で本番SLOではない。
- Stage M contract `33 passed / 1 expected symlink skip`、関連 `107 passed / 1同skip`、py_compile成功、最終全suite `3047 passed / 2 expected symlink skips in 105.74s`。本番DB/config/persona/過去Trace/永続`memory.write_enabled`/外部I/O/実機は未変更。確認付き自然言語削除UXは未実装のまま別承認事項。[handoff](handoffs/2026-09-09_0241_sol-memory-stage-m-review-fix.md)。

## 2026-09-08 21:45 — Sol — Stage M 非削除Memory保持とbounded検索

- 前Solのpartial diffを再監査し、canonical `memories`/`transcripts`を件数・年齢・importanceだけで削除しない`RetentionPolicy`、非破壊startup/maintenance hook、理由付き明示削除、SQLite write失敗時のprocess-local read-only退避を完成させた。
- sourceとは別の`*.search-index`へbounded FTS5候補取得を置き、source署名によるfreshness、restart後の破損sidecar自動再構築、canonical source行でのpersona/scope ACL再検査を追加。古い/archived記憶の明示Recall、B→C→B、DO_NOT_STORE、backup失敗、実`SQLITE_FULL`をtmp_pathだけで検証した。
- REDはpoisoned sidecar経由の別persona source返却と、破損sidecar restart後にrecent fallbackで古い記憶を見失う2件。修正後Stage M 19 passed、関連Memory/backup 124 passed、persona分離/migration 46 passed、py_compile成功。最終全suiteは3033 passed / 1 expected Windows symlink skip（110.16秒）。
- 隔離benchmark 1k/10k/100kを実行。100kはsource 100,000→100,000、候補1（上限8）、query p50 1.624ms / p95 3.838ms、startup 5.158ms、index build 4711.074ms、測定範囲peak RAM 23,813,074 bytes、sidecar 30,695,424 bytes。探索値で本番SLOではない。
- 本番DB、persona/config、過去Trace、外部I/O、実機起動、永続`memory.write_enabled`は未変更。P3以降未着手。本番移行はbackup/dry-run/件数・指紋照合と別承認が必要。[handoff](handoffs/2026-09-08_2137_sol-memory-retention-stage-m.md)。

## 2026-09-08 08:45 — Sol — 会話品質 P0〜P2 deadline/cancel code close

- 前Sol実装を再検査し、共有deadlineが結果の7秒上限と完了後stale拒否だけで、検索中のowner失効を待機解除・後続page停止へ伝播していない穴を特定。slow provider待機とquery後page開始をRED 2件で再現した。
- `execute_search_route`へ最大50msのstale-owner poll、`DeepSearch`のquery後・page開始前・redirect前へ同じ`is_current`を配線。7秒と既存`search.timeout_s`の短い方、response bytes上限、危険URL拒否は維持。
- deadline単体11 passed、拡張targeted 242 passed / 1 expected Windows symlink skip、offline synthetic 27/27、最終再検証の全suite 3014 passed / 1同skip（103.12秒）。fault injection用変更の残置なし。
- P0〜P2は`CODE_VERIFIED / HARDWARE_PENDING / HUMAN_QUALITY_PENDING`。P3/M、外部検索、Local LLM、TTS、Discord実接続、本番DB、persona、config、過去Traceは未変更。実機LocalはMまたは隔離data前提で2発話だけ残存。[handoff](handoffs/2026-09-08_0845_sol_conversation-quality-p0-p2-close.md)。

## 2026-09-07 22:04 — Sol — 会話品質 P0〜P2（offline評価・検索route・Evidence契約）

- 27件のsynthetic corpusとoffline評価CLIを追加し、検索のattempt/completed/grounded/deliveryを分離。本文なしで測れない自然さ・感情適合・音質および旧固定品質軸を`None / NOT_MEASURED`にした。
- 複合検索依頼から対象queryだけを抽出する共有`SearchRouteDecision`をLocal/Discordへ配線。BLOCKED/CLARIFY/DEFERRED/REUSEを通常生成と区別し、Discordはawait後のstale結果をUI/TTSへcommitしない。
- query-awareな`SearchOutcome`/`EvidenceSpan`を追加。PAGE/SNIPPET選択、FAILED/EMPTY/PARTIAL区別、public URL/redirect/1MiB制限、hash付き抽出整合、process-local Evidence cacheを両surfaceで共有した。DAVE mix/leave、Memory DB/persona/config/過去Traceは未変更。
- REDはAPI欠落、複合Intent、固定点、query汚染、Evidence/URL/error境界で確認。fault injectionはcommand判定、Evidence hash、stale guardの3系統が期待通り赤化し復元後GREEN。最終targetedは163 passed / 1 Windows symlink skip、最終全suiteは3005 passed / 1同skip（102.10秒）。
- query+fetchへ共有monotonic deadline（最大7秒、既存設定が短ければ優先）を追加。開始前cancel、遅いprovider、期限後のpage/redirect停止、検索中staleを故障注入で確認。P0保持監査では起動時transcript DELETEと保守時memory DELETE/archiveを確認したため、Mまでは本番DB起動試験/Memory write再開を禁止。実機Local→Discord受入は残存。[監査](audits/2026-09-07_conversation-quality-p0-p2.md)／[handoff](handoffs/2026-09-07_2204_sol_conversation-quality-p0-p2.md)。

## 2026-09-07 10:04 — Codex — ユーザーの会話品質方針をSol指示書へ反映（文書のみ）

- Local優先→Discord複数人、適度な活発さ・からかい多め・確認済み製作者へ強め、速度優先、Local LLM限定、現音声維持、Fish Audioは候補、状況依存の発話、容量増を許容した記憶保持を確定要件にした。
- [実装計画](superpowers/plans/2026-09-07-conversation-quality-sol.md)と[ADR-0005](adr/ADR-0005-conversation-quality-recovery.md)を更新。回答済みの質問を除去し、人格/複数人の追加受入系列と独立単位M（保持・検索負荷分離）を追加。
- `MemoryStore.prune_transcripts/prune`とMind側呼出しを静的確認。日数/件数の自動削除経路があるため、本番DB起動試験・write再開前に保持契約の検証を要求。今回のDB・コード・設定変更なし。
- Decision D-043、risk R-049、次session入口を同期。全pytest・実機・モデル推論は未実行。[handoff](handoffs/2026-09-07_1004_codex_quality-preferences.md)。

## 2026-09-07 — Codex — 会話品質の再レビューとSol向け実装計画（文書のみ）

- 実コードと純粋関数probeから、複合検索依頼の未検出、query抽出不足、攻略偏重の検索、Evidenceの160文字制限、固定値の品質スコアを確認。実機全原因・自然さの改善は未判定。
- [ADR-0005](adr/ADR-0005-conversation-quality-recovery.md)をPROPOSEDとして追加。[Sol向け指示書](superpowers/plans/2026-09-07-conversation-quality-sol.md)にP0〜P7、変更対象、型境界、27ケースの人工corpus、故障注入、受入・rollback・後日確認事項を記載。第1便はP0〜P2へ限定。
- Decision Log、risk、次session入口を同期。CURRENTの実装は変わっていないためCURRENT/CODEMAPは変更なし。アプリコード・本番DB・persona・設定・過去Traceを編集していない。
- 検証: 外部I/Oなしの関数probe、文書参照・構成の検査。全pytest/LLM/外部検索/実機試験は未実行。手順詳細は [handoff](handoffs/2026-09-07_0857_codex_conversation-quality-sol-plan.md)。

## 2026-08-07 — Codex — grounded search and Discord DAVE reliability

- Replaced the Local result-only `「○○を見つけたよ」` response with a shared, bounded extractive Evidence View.  It accepts only sanitised title/URL/domain plus a short non-instruction-like source excerpt; it never turns source HTML or instructions into actions.  Missing evidence is explicitly reported as unconfirmed rather than completed from a title alone.
- Discord explicit search now uses that same deterministic evidence presenter instead of injecting raw DeepSearch text into an LLM prompt.  The result card and Trace remain content-free; the spoken explanation is bounded to source evidence.  Existing Local PROD Tool permissions and persistent flags are unchanged.
- Discord final input now carries a monotonic epoch.  A newer finalized utterance invalidates queued/delayed older work and cancels an active older response before it can claim the response slot.  DAVE music and TTS now use one mixed PCM writer; voice ducks music to 70% instead of stopping it.
- DAVE leave is now acknowledged end-to-end.  The sidecar awaits its Discord leave operation and emits `left`; Python waits for it and preserves a failed state rather than falsely reporting a successful GUI exit.
- Verification: focused `247 passed, 1 skipped`; full isolated `.venv-test` suite `2974 passed, 1 skipped in 112.15s` (sole skip: Windows symlink privilege). No Memory DB, persona, persistent config, historical Trace, or real Discord session changed. See `docs/handoffs/2026-08-07_0500_codex_grounded-search-discord-reliability.md`.

## 2026-08-07 — Codex — Phase 8 Tool Presenter delivery unification

- Diagnosed Local PROD Tool turn `81d8be729c9b` read-only: it created one SpeechRequest and accepted one TTS job, but `_respond_direct_text()` bypassed TurnTracker's TTS-chunk/Playback-session callbacks. Therefore there is no evidence of a logical session, Playback start/complete, or delivery finalization in that historical Trace; actual physical playback cannot be inferred from it.
- Replaced direct TTS/Playback in deterministic replies with the shared `_speak_loop` / TurnTracker / SpeakerPlayback coordinator. Tool Presenter, direct control, and verified game replies now share logical session, segment, callback, seal, and final delivery path.
- ActionOutcome now waits for terminal delivery evidence: no session or no segment is `failed`; an incomplete segment is `interrupted`; only completed physical segments may close as `completed`. Trace records finalizer call/state/error and final playback values without text.
- Verification: focused delivery/Tool suite `246 passed, 1 skipped`; final isolated suite `2968 passed, 1 skipped` (only Windows symlink privilege, 101.70s). Hardware regression remains pending; see `docs/handoffs/2026-08-07_0330_codex_phase8-tool-delivery-unification.md`.

## 2026-08-07 — Codex — Phase 8 safe read-only Tool wiring

- Wired the existing `web_search.search` capability into the Local `production_session` cognitive route only. The session override is process-local, enables only that READ_ONLY capability, and never writes `config.yaml`.
- An explicit read-only search request is proposed as `EXECUTE_TOOL`; after execution starts, the route never invokes Legacy fallback or re-executes the tool. Result content is never sent to a prompt or speech; only verified success/failure selects a fixed short report.
- Added privacy-safe Tool Trace accounting: proposal ID, permission decision, execution counts/status, result availability, planner-use bit, and fixed `side_effect_committed=false`. No Memory DB, persona, migration, configuration, or historical Trace changed.
- Corrected the existing response-required candidate filter so its normal `ANSWER` contract cannot discard the bounded explicit Local PROD Tool candidate before selection.
- Added the Tool Result → deterministic Result Presenter route. It re-normalizes untrusted title/URL metadata, rejects non-http(s), credentialed and HTML-like URLs, passes no snippets/HTML to an LLM, and emits one safe UI title/URL plus grounded short TTS response. The PROD-session execution is forced to one attempt even after a Tool error; a presenter exception becomes one display-error response and cannot re-execute the Tool.
- Verification: focused Tool/Phase 8 suite `182 passed, 1 skipped`; final isolated suite `2964 passed, 1 skipped` (only Windows symlink privilege, 85.16s). Manual hardware probe remains pending; see `docs/handoffs/2026-08-07_0300_codex_phase8-tool-result-presenter.md`.

## 2026-08-07 — Codex — Phase 8 closure action eligibility

- Diagnosed the failed Local PROD terminal probe read-only: the existing stop intent and `end_signal` were present, and `REMAIN_SILENT` had the highest raw score, but the old `response_required` answer-only filter replaced the candidate set before selection.
- Added `ActionEligibility` and `ActionConstraintValidator` before the planner. A closure-only turn may select only `REMAIN_SILENT`/`BRIEF_ACKNOWLEDGE`; an explicit reply request selects only `BRIEF_ACKNOWLEDGE`; a direct question keeps `ANSWER` eligible. Invalid selector output is corrected without an LLM call or Legacy fallback.
- Trace now records privacy-safe closure features, response obligation, eligible actions, raw candidate scores, initial/final action, and validator result. `BRIEF_ACKNOWLEDGE` uses a one-sentence direct speech route rather than the normal response prompt. No Memory DB, persona, migration, persistent configuration, or historical Trace changed.
- Verification: targeted Phase 8 tests `17 passed`; final full isolated suite `2954 passed, 1 skipped` (Windows symlink privilege only, 101.83s). See `docs/handoffs/2026-08-07_0130_codex_phase8-closure-eligibility.md`.

## 2026-08-06 01:51 JST — Codex — Phase 8 silence contract and response-required guard

- Corrected the handoff acceptance wording: the originally silent production turn is a terminal-cue probe, and the three response-required regression probes are evaluated separately.

- Corrected the terminal-cue regression found in the first post-restart run: `activity_input_has_priority` reaches its command owner but no longer becomes the stronger spoken-ANSWER contract. `ResponsePlan.requires_response_contract` is restricted to addressed answer/continuation intent, leaving a recognized end signal free to select `REMAIN_SILENT` or `BRIEF_ACKNOWLEDGE`. Verification: `2945 passed, 1 skipped` (Windows symlink privilege only, 104.47s). See `docs/handoffs/2026-08-06_0210_codex_phase8-terminal-intent.md`.

- Diagnosed the Local PROD SESSION silence `6ecff02d0db6` read-only: it was a genuine Action Selector `REMAIN_SILENT` decision (`user_is_ending,nothing_to_close`), not an exception, empty response, or delivery failure. Its Memory influence was only `continue_previous_topic:+0.108`.
- Added a privacy-safe silence contract to Trace: decision ID/source, silence reason code, suppression reason, and response-required provenance. A normal silent turn now closes through the same authoritative ActionOutcome path, so closure/outcome fields are complete.
- ConversationKernel's existing response plan now freezes `response_required` into CognitiveState. When true, the Action Selector selects ANSWER rather than allowing closure/Memory heuristics to choose REMAIN_SILENT. No text-only duplicate classifier was added.
- An incomplete silent decision is a technical pre-effect failure: it cannot become `silent_completed`; it may use the existing one-shot Legacy fallback, otherwise becomes an explicit failed outcome. No Memory DB, persona, config, migration, or historical Trace changed.
- Verification: `.venv-test\Scripts\python.exe -m pytest -q` → `2944 passed, 1 skipped` (Windows symlink privilege only, 93.81s).
- Handoff: `docs/handoffs/2026-08-06_0150_codex_phase8-silence-contract.md`

## 2026-08-06 00:35 JST — Codex — Phase 8 rollout resolver and atomic fallback foundation

- Added one `CognitionRolloutResolver` for `disabled`, `test_session`, `production_session`, and explicit persistent `production`; session overrides remain process-local and restart-disabled.
- Extended frozen TurnMetrics/TurnFrame/Trace context with requested/resolved rollout, activation source, config fingerprint, and transport for Local and Discord.
- Added UI OFF → TEST → PROD SESSION cycling and `--cognition-production-session`; neither writes `config.yaml`.
- Added a side-effect-aware, one-shot technical fallback policy and privacy-safe fallback Trace fields. Local delivery marks accepted SpeechRequest/playback; historical Echo delivery semantics remain unchanged.
- Discord now consumes the same resolver/frozen context and runs the existing no-LLM Action Selector on enabled normal turns; its Trace carries the same rollout fields.
- Verification: isolated `.venv-test` full suite `2937 passed, 1 skipped` (Windows symlink privilege only, 99.47s). No Memory DB, persona, migration, persistent cognition setting, or historical Trace changed.
- Handoff: `docs/handoffs/2026-08-06_0035_codex_phase8-rollout-foundation.md`

## 2026-08-06 00:13 JST — Codex — explicit-answer echo hardware acceptance

- Status: completed for the requested four-turn echo-recovery regression.
- Evidence: fresh post-restart cognition test-session turns `e6218ba7efe7`, `e4993d12f2ca`, `370e7e5b57b3`, and `eca2d45bf9a7` all selected ANSWER, performed zero Memory retrieval/tool execution, created one SpeechRequest and one logical playback session, had zero replay/duplicate, produced a nonempty response, and closed successfully with no delivery failure.
- Echo evidence: the second repeated-fact turn resolved historical similarity as `ALLOW_HISTORICAL_SIMILARITY` (primary and regeneration score `1.0`) and still delivered. The final repeated-affect turn regenerated once and delivered its non-historical regeneration. This confirms historical similarity is no longer used as a duplicate-delivery key.
- Config / DB migration: none. No Memory DB, persona, config, migration, or historical Trace change.
- Remaining: the prior 30-turn run remains reference-only because it contained the two pre-fix omissions and an unmarked 31st Trace item. Do not enter Phase 8 without the user’s next direction.
- Handoff: `docs/handoffs/2026-08-06_0013_codex_echo-recovery-hardware-acceptance.md`

## 2026-08-05 23:40 JST — Codex — explicit-answer echo recovery

- Status: code and automated verification completed; the requested four-turn hardware regression remains.
- Fixed: historical ReplyEchoGuard similarity no longer discards an undelivered explicit ANSWER. The guard may trigger one regeneration, but a historical match on that regeneration retains the unshipped primary rather than silently completing the turn.
- Fixed: an explicit cognitive ANSWER is now `failed` with `response_empty` or `tts_or_playback` if no logical playback session exists. It is `completed` only after one is accepted; outcome/Trace now distinguish speech generation, closure, and delivery failure.
- Changed: Trace adds privacy-safe echo and response-delivery diagnostics; the same historical-similarity resolution is used for Local and Discord. Hard duplicate rejection remains the SpeechRequest/Playback ledger's responsibility.
- Tests: separate `.venv-test` full pytest: `2931 passed, 1 skipped in 84.62s` (Windows symlink privilege only).
- Config / DB migration: none. No Memory DB, persona, config, migration, or historical Trace change.
- Remaining: fully restart, enable cognition test session, then say `月曜日の次は？` and `今日は静かに過ごしたい。短く返して` twice each. Verify one logical playback session per turn, no replay, nonempty final response, completed closure, no Memory retrieval, and no tool path before accepting the two regressions.
- Handoff: `docs/handoffs/2026-08-05_2340_codex_explicit-answer-echo-recovery.md`

## 2026-08-05 22:45 JST — Codex — obligation retrieval gate

- Status: code and automated verification completed; the requested one-turn cognition hardware check remains.
- Fixed: a current-turn `ConversationKernel` response duty had been treated as an unresolved Memory obligation, which made an unrelated question eligible for retrieval. Only active, same-persona WorkingMemory open questions are now candidates; explicit resumption, deterministic topic/entity overlap, or the selected continuation action is required.
- Changed: due obligations emit a privacy-safe `OBLIGATION_DUE` attention event for the Initiative path. They do not silently become Memory context for an unrelated ANSWER. Trace records obligation-retrieval considered/triggered/reason/candidate-count/relevance/effect-on-action; non-retrieval now records `not_applicable` / `NOT_APPLICABLE` explicitly.
- Tests: separate `.venv-test` full pytest: `2925 passed, 1 skipped in 98.47s` (Windows symlink privilege only).
- Config / DB migration: none. No Memory DB, persona, config, migration, or historical Trace change.
- Remaining: fully restart, enable the cognition test session, and say only `5足す3は？`; verify cognition ANSWER, Memory `NOT_APPLICABLE`, zero retrieval, obligation trigger false, one logical playback session, and zero replayed segments before the final 30-turn measurement.
- Handoff: `docs/handoffs/2026-08-05_2245_codex_obligation-retrieval-gate.md`

## 2026-08-05 22:18 JST — Codex — streaming playback hardware acceptance

- Status: completed for the requested one-turn playback-delivery acceptance.
- Evidence: fresh cognition test-session turn `c966f56ef6c7` emitted a completed Trace: SpeechRequest `1`; logical playback session `1`; physical/unique segments `4/4`; replayed segments `0`; overlap `false`; duplicate stage `none`; delivery trace finalized `true`.
- Config / DB migration: none. No Memory DB, persona, config, migration, or historical Trace change.
- Remaining: inspect the unrelated `unresolved_obligation` retrieval trigger before beginning the final 30-turn measurement.
- Handoff: `docs/handoffs/2026-08-05_2216_codex_trace-shutdown-delivery-flush.md`

## 2026-08-05 22:16 JST — Codex — trace shutdown delivery flush

- Status: partial. The 22:12 hardware utterance was handled, but its Trace was lost during normal shutdown before its multi-chunk physical playback completed. The shutdown-safe Trace fix is verified; one new post-restart utterance is still required.
- Fixed: pending local playback-closure Trace tasks are now included in shutdown. Cancellation emits one final privacy-safe delivery snapshot with `trace_delivery_finalized=false`, then allows normal shutdown and bounded writer flush.
- Tests: full separate `.venv-test`: `2916 passed, 1 skipped in 96.80s` (Windows symlink privilege only).
- Config / DB migration: none. No Memory DB, persona, config, migration, or historical Trace change.
- Remaining: restart again and say only `短く自己紹介して`; wait until speech fully finishes before closing. Verify the fresh Trace instead of the 22:12 run.
- Handoff: `docs/handoffs/2026-08-05_2216_codex_trace-shutdown-delivery-flush.md`

## 2026-08-05 22:10 JST — Codex — streaming playback delivery accounting

- Status: partial. Code and automated verification are complete; the requested post-restart one-turn hardware confirmation remains.
- Changed: delivery Trace now separates `speech_request_count`, `tts_chunk_count`, logical playback sessions, physical playback segments, unique segments, and replayed segments. It records IDs, indexes, safe text/audio fingerprints, and start/end times only—never text or PCM.
- Changed: a streaming response may enqueue multiple unique segments in one logical session. A second session for the same SpeechRequest, a repeated segment ID, or a new ID with the same session/index/audio fingerprint is rejected and diagnosed as `PLAYBACK_SESSION` or `PLAYBACK_SEGMENT`.
- Changed: Local and Discord both use the same delivery ledger. Trace emission waits asynchronously for physical playback closure (maximum 30 seconds), without blocking speech or using synchronous disk I/O/fsync.
- Tests: full separate `.venv-test`: `2915 passed, 1 skipped in 79.36s` (Windows symlink privilege only).
- Config / DB migration: none. No Memory DB, persona, config, migration, or historical Trace change.
- Remaining: fully restart the current local code and speak only `短く自己紹介して`; accept only if one logical playback session, zero replayed segments, and `duplicate_stage=none`. The historical `b6e9958e5590` remains unclassifiable because its old Trace has no per-segment identifiers/hashes/timing.
- Handoff: `docs/handoffs/2026-08-05_2210_codex_streaming-playback-delivery.md`

## 2026-08-05 21:32 JST — Codex — cognitive outcome single payload

- Status: partial. The completion-path defect is fixed and fully tested; the requested one-turn cognitive hardware smoke remains pending.
- Fixed: `VoicePipeline._cognitive_outcome()` previously expanded `ActionOutcome.snapshot()` and separately supplied `obligations`, even though the snapshot already owns that field. It now confirms obligations on the `ActionOutcome`, constructs one event payload once, and shares that payload with the event and Trace outcome summary.
- Fixed: ANSWER completion now stamps the outcome with the frozen turn ID. REMAIN_SILENT uses the same one-payload event/Trace sequence, with zero accepted SpeechRequests.
- Changed: Trace writer enqueue failures are isolated and logged, while ActionOutcome and Speech Gate failures are no longer swallowed. Turn/session cleanup still runs through `finally` when a real completion error propagates.
- Tests: added isolated completion-path coverage for empty/nonempty obligations, Trace writer failure, REMAIN_SILENT, same-turn re-delivery, and non-swallowed ActionOutcome/Speech Gate failures. Full separate `.venv-test`: `2910 passed, 1 skipped in 92.57s` (Windows symlink privilege only).
- Config / DB migration: none. No Memory DB, persona, config, migration, or historical Trace changes. The failed 21:21 turns were not reconstructed or appended.
- Remaining: fully restart, enable the UI cognition test session, and perform only `2足す2は？` first. Do not run the remaining two warmup turns or the 30-turn measurement until its Trace and playback meet the requested contract.
- Handoff: `docs/handoffs/2026-08-05_2132_codex_cognitive-outcome-payload.md`

## 2026-08-05 01:11 JST — Codex — cognition test-session control

- Status: partial. Controlled entry and automated verification are complete; the requested hardware smokes and 30-turn measurement are not yet run.
- Changed: GUI has a process-local cognition test-session toggle and launcher accepts `--cognition-test-session`; ordinary launch/restart remains Legacy and no setting is persisted.
- Changed: queued state applies only at a turn boundary and increments an epoch. `TurnMetrics`/`TurnFrame` freeze cognition enabled/mode/epoch; stale test-epoch speech is rejected at final gate.
- Changed: Trace records frozen cognition contract, selector/gate/legacy-path booleans, selected action, and accepted SpeechRequest count. Silent outcomes record zero SpeechRequests.
- Changed: an explicit 🩺 search now overrides stale “attention only” / favorites filters, so `memory.write_enabled=false` and other smoke-check values cannot be hidden while searching.
- Fixed: diagnostic matching now treats `memory.write_enabled=false` and `memory.write_enabled = false` as equivalent.
- Fixed: Trace now separates configured `cognition.rollout_mode` from `cognition.execution_path`; Legacy records `disabled` / `legacy`, test session records `test_session` / `cognitive`.
- Tests: final separate `.venv-test` full pytest after the Trace split: `2903 passed, 1 skipped in 92.12s`.
- Config / DB migration: none. Memory DBs, persona settings, migration state, and persistent cognition settings unchanged.
- Tests: final separate `.venv-test` full pytest `2901 passed, 1 skipped in 95.26s` (Windows symlink privilege only).
- Remaining: requested Legacy/cognition smokes and one cognitive 30-turn measurement; no Phase 8 work.
- Handoff: `docs/handoffs/2026-08-05_0111_codex_cognition-test-session.md`

- Purpose: CodexとClaude Codeが、相手の変更を次の作業開始時に把握するための共通入口
- Update policy: 新しい記録を先頭へ追加する（逆時系列）
- Detail policy: ここには要点を記録し、調査・テスト・rollbackの詳細は `docs/handoffs/` へ記録する
- Privacy: 秘密情報、個人情報、private transcript、raw audio、raw imageを記録しない

## 必須記録形式

```md
## YYYY-MM-DD HH:MM JST — agent — task

- Status: completed / partial / blocked / reverted
- Summary:
- Changed:
  - `path`: 変更内容
- Behavior impact:
- Config / DB migration:
- Tests:
- Remaining:
- Handoff: `docs/handoffs/<file>.md`
- Related ADR / decision:
```

---

## 2026-08-05 00:41 JST — Codex — retrieval trigger / latency diagnostics / Windows pytest

- Status: partial. The implementation and automated verification are complete; replay of the nonpersistent real 30-ASR corpus and cognition test-session hardware acceptance remain.
- Changed: `ContextAssembler` now treats `memory_recall_budget_ms` as a timeout only. A no-LLM explicit past/labelled-recall trigger is now required before `include_recall=true`; ordinary turns are Trace `NOT_APPLICABLE/not_applicable`. Generic noun/proper-name presence no longer triggers retrieval.
- Changed: `llm_diagnostics` records prompt-build ms, estimated prompt tokens/reuse, retrieved count, provider first-token time, queue contention, and native provider durations only when supplied. Prompt/transcript text is never recorded.
- Changed: KTaNE fixture uses `tmp_path`; symlink integration skips only on missing Windows symlink privilege, while a privilege-independent escape-rejection unit test remains.
- Config / DB migration: none. Runtime `.venv`, Memory DBs, persona settings, and migration state were not changed.
- Tests: affected `101 passed`; Windows fixtures `129 passed, 1 skipped`; full separate `.venv-test` `2897 passed, 1 skipped`.
- Remaining: current Trace/metrics intentionally retain no ASR text, so the exact real 30 utterances cannot be replayed without a supplied transient corpus. `start_cognition_test_session()` has no UI/launcher caller, so cognitive real-session acceptance is not yet runnable from the app surface.
- Handoff: `docs/handoffs/2026-08-05_0041_codex_retrieval-trigger-latency-pytest.md`

## 2026-08-04 23:02 JST — Codex — Phase 7D recall, latency, delivery, Discord Trace

- Status: partial (implementation and isolated/full test environment complete; 30-turn real measurement remains).
- Summary: Recorded the accepted B→C→B recall result, generalized deterministic explicit recall labels, then attached the existing real-path metrics backbone to local rolling latency, privacy-safe delivery-layer diagnostics, and Discord normal-turn Trace.
- Changed:
  - `neuro_voice/mind/mind.py`, `neuro_voice/cognition/trace.py`: `recall_evidence_memory_ids`; explicit recall contract covers passphrase, plan name, number, and equipment name without an LLM call.
  - `neuro_voice/utils/latency.py` integration in `neuro_voice/pipeline.py`: local `LatencyWindow` now emits source-separated rolling p50/p90/p95; final local Trace carries the real `TurnMetrics` snapshot.
  - `neuro_voice/cognition/turn_integrity.py`, `neuro_voice/cognition/turn_tracker.py`: privacy-safe `SpeechRequest → TTS job → Playback` attempted/accepted/rejected counts and first duplicate layer.
  - `neuro_voice/discord_bridge/bot.py`, `neuro_voice/cognition/async_trace_writer.py`: Discord normal turns emit one Trace record to the same project-root JSONL; concurrent local/Discord append is serialized.
  - `tests/test_turn_tracker_wiring.py`, `tests/test_trace_output.py`: delivery/latency/Discord wiring assertions.
- Behavior impact: Trace contains no conversation text, prompt, raw audio, or delivery IDs. It now identifies the exact turn latency and the delivery layer that duplicated when an anomaly occurs.
- Config / DB migration: none. Runtime `.venv`, Memory DBs, persona settings, and migration state were not changed.
- Tests: new/affected suites 166 passed; final separate `.venv-test` full pytest: 2892 passed, 2 failed solely due to Windows environment assumptions: `C:\tmp` write failure for a Linux `/tmp` fixture and missing symlink creation privilege (`WinError 1314`).
- Remaining: restart before using new code; collect 30 ordinary local turns, then report only `turn_latency.total_ms` p50/p90/p95 with sample count and inspect `speech_delivery`. Run a real Discord normal conversation before treating its runtime Trace path as accepted.
- Handoff: `docs/handoffs/2026-08-04_2302_codex_latency-delivery-discord-trace.md`

## 2026-08-04 22:44 JST — Codex — Phase 7D accepted b/c/b evidence trace

- Status: completed (persona-retrieval smoke scope); Phase 7D overall remains partial.
- Summary: Recorded the post-restart b/c/b pass and separated all retrieved candidates from the record actually used for a deterministic recall answer.
- Changed:
  - `neuro_voice/mind/mind.py`, `neuro_voice/cognition/trace.py`: add privacy-safe `recall_evidence_memory_ids`.
  - `tests/test_memory_retrieval_wiring.py`: asserts evidence ID 1 for the blue-lighthouse contract.
- Behavior impact: Trace can distinguish broad retrieval candidates `[1,2,3]` from answer evidence `[1]`, without logging fact text.
- Config / DB migration: none; read-only verification confirms B=7, C=2.
- Tests: pending alongside the next ordered test-environment work.
- Remaining: generic recall intents, isolated full pytest, latency/duplicate instrumentation, Discord normal-turn Trace, and 30-turn measurement.
- Handoff: `docs/handoffs/2026-08-04_2244_codex_bcb-smoke-evidence.md`


## 2026-08-04 08:24 JST — Codex — Phase 7D ASR-tolerant recall contract

- Status: partial
- Summary: The post-restart b/c/b Trace proved scope isolation and no writes, but two ASR variants bypassed the narrow recall matcher. Extended the deterministic passphrase contract to those explicit variants and made its zero LLM calls observable.
- Changed:
  - `neuro_voice/mind/mind.py`: accepts the passphrase label plus recall-seeking verbs as explicit recall; every explicit recall contract now returns a deterministic reply and records zero LLM calls.
  - `neuro_voice/cognition/trace.py`: adds `memory.diagnostics.recall_llm_call_count`.
  - `tests/test_memory_retrieval_wiring.py`: checks the ASR variant and zero-call invariant.
- Behavior impact: passphrase recall cannot fall through to a free LLM response merely because ASR omits the leading past-reference phrase.
- Config / DB migration: none; read-only DB count check remains b=7, c=2.
- Tests: compileall and isolated retrieval/contract checks passed. The already-run real test is not a full contract pass: Trace was FOUND / NOT_APPLICABLE / NOT_APPLICABLE, though scope and write invariants held.
- Remaining: one new full restart, then b/c/b once; require Trace FOUND / NOT_FOUND / FOUND, retrieved 1 / none / 1, zero recall LLM calls, and empty writes. Full pytest remains blocked.
- Handoff: `docs/handoffs/2026-08-04_0824_codex_asr-recall-contract.md`


## 2026-08-04 03:34 JST — Codex — Phase 7D recall evidence and response contract

- Status: partial
- Summary: Read-only probing proved that stored question records outranked the factual b record. Excluded stored recall questions from both lexical and semantic retrieval, then added deterministic FOUND/NOT_FOUND response contracts for explicit passphrase recall.
- Changed:
  - `neuro_voice/mind/mind.py`: question-shaped stored records cannot be recall evidence; remembers selected safe records only for the current turn; derives a labelled passphrase fact or a NOT_FOUND reply.
  - `neuro_voice/pipeline.py`: a recall contract bypasses the LLM for the deterministic reply, before any TTS output.
  - `neuro_voice/cognition/trace.py`: adds privacy-safe `memory.diagnostics.recall_status`.
  - `tests/test_memory_retrieval_wiring.py`: response-contract isolation test.
- Behavior impact: explicit memory questions cannot be answered by a stored copy of the same question. A missing fact produces an "I do not remember" reply; the known b fact produces the exact stored passphrase.
- Config / DB migration: none. Read-only inspection only; b remains 7 and c remains 2.
- Tests: compileall and isolated checks passed. Real DB, same implementation, read-only probe: b selects ID 1 (blue lighthouse, 0.3333); c selects none; contracts yield FOUND/blue lighthouse and NOT_FOUND/do-not-remember respectively.
- Remaining: full restart and one controlled b/c/b test; full pytest environment remains a Phase 7D blocker. Review whether the test-only write gate should later include transcript persistence.
- Handoff: `docs/handoffs/2026-08-04_0334_codex_recall-response-contract.md`


## 2026-08-04 03:07 JST — Codex — Phase 7D retrieval-only smoke guard

- Status: partial
- Summary: Fixed the verified no-vector recall blind spot and added an independent, currently disabled Memory write gate for the b/c/b retrieval-only smoke test.
- Changed:
  - `neuro_voice/mind/mind.py`: semantic retrieval falls back to strict scoped lexical retrieval when a persona DB has no vectors; retrieval-only mode prevents `touch()` and transcript writes.
  - `neuro_voice/mind/episodes.py`: `memory.write_enabled` blocks every episode mutation and clears per-turn write decisions.
  - `config/config.yaml`: `memory.write_enabled: false` for the controlled smoke test.
  - `neuro_voice/diagnostics/wiring.py`, `neuro_voice/ui/assets/index.html`: show the write gate and its retrieval-only explanation in 🩺 smoke settings.
  - `tests/test_memory_retrieval_wiring.py`: isolated no-write and no-vector fallback checks.
- Behavior impact: normal response context can retrieve pre-embedding scoped memories; controlled test turns do not change Memory DB rows or access metadata.
- Config / DB migration: config-only; no Memory DB was restored, deleted, or edited.
- Tests: compileall and isolated temporary-DB checks passed; pytest and config loader remain unavailable because the current test runtimes lack pytest/PyYAML and the project venv base interpreter is broken.
- Remaining: b/c/b must be run once after a complete restart. Existing prior test contamination is b=7/c=2, not the requested 5/1 baseline; do not alter DBs without explicit recovery authorization.
- Handoff: `docs/handoffs/2026-08-04_0307_codex_retrieval-only-smoke-guard.md`


## 2026-08-04 01:24 JST — Codex — 🩺診断項目のお気に入りを追加

- Status: completed
- Summary: 頻繁に確認する配線診断を各行の ☆ で登録し、「お気に入りのみ」で一覧を絞り込めるようにした。
- Changed:
  - `neuro_voice/ui/assets/index.html`: 診断行へお気に入りの登録・解除ボタン、フィルタ、未登録時の案内を追加。登録対象は診断キーのみで、端末のブラウザ保存領域へ保持する。
  - `tests/test_migration_ui.py`: ローカル保存、フィルタ、診断キーのデータ属性を検査する回帰テストを追加。
  - `docs/CODEMAP.md`: お気に入り機能と保存範囲を追記。
- Behavior impact: 🩺 パネルの表示・端末内UI設定のみ。設定、ペルソナ切替、会話経路、Memory、Trace、DBは変更なし。
- Config / DB migration: なし。
- Tests: Node によるJS構文チェック、およびお気に入りのキー保存・お気に入りのみフィルタの直接検証が成功。`pytest` は仮想環境が存在しないPython 3.11本体を参照して起動できず未実施。
- Remaining: スモークテスト結果待ちを継続。Python 実行環境復旧後に `tests/test_migration_ui.py` を再実行する。
- Handoff: `docs/handoffs/2026-08-04_0124_codex_diagnostic-favorites.md`
- Related ADR / decision: 第12条、第17条。

## 2026-08-04 01:20 JST — Codex — 🩺スモークテスト確認値へ説明を追加

- Status: completed
- Summary: スモークテスト確認値をチップ表示から通常の診断行へ変更し、項目名・技術キー・役割・現在値を併記した。後から見ても各値を確認する理由が分かる。
- Changed:
  - `neuro_voice/ui/assets/index.html`: 5つの確認値へ、Memory想起・ペルソナ分離・ターン追跡・Traceのプライバシー境界・選択中ペルソナの役割を説明として追加。
  - `tests/test_migration_ui.py`: 必須の説明文がUIに残ることを検査する回帰テストを追加。
  - `docs/CODEMAP.md`: 診断行としての説明・現在値表示を追記。
- Behavior impact: 🩺 パネルの表示のみ。設定、ペルソナ切替、会話経路、DBは変更なし。
- Config / DB migration: なし。
- Tests: Node によるJS構文チェック、およびTrace・active personaの説明と現在値を含む表示ヘルパー直接検証が成功。`pytest` は仮想環境が存在しないPython 3.11本体を参照して起動できず未実施。
- Remaining: スモークテスト結果待ちを継続。Python 実行環境復旧後に `tests/test_migration_ui.py` を再実行する。
- Handoff: `docs/handoffs/2026-08-04_0120_codex_diagnostic-smoke-descriptions.md`
- Related ADR / decision: 第12条、第17条。

## 2026-08-04 00:59 JST — Codex — 🩺検索へスモークテスト確認値を統合

- Status: completed
- Summary: `cognition.trace.enabled = true` と `active_persona_id = b` は表示されても、当初は配線プローブだけを対象にする検索から漏れていた。スモークテスト確認の5値を検索結果と一致件数へ統合した。
- Changed:
  - `neuro_voice/ui/assets/index.html`: スモークテスト確認値を検索対象へ追加。技術キーだけでなく、表示どおりの`key = value`（例: `active_persona_id = b`）でも一致する。
  - `tests/test_migration_ui.py`: 完全表記を検索文字列へ組み立てる回帰検査を追加。
  - `docs/CODEMAP.md`: スモークテスト確認値の検索対象化を追記。
- Behavior impact: 🩺 パネルの表示・検索のみ。設定、ペルソナ切替、会話経路、DBは変更なし。
- Config / DB migration: なし。
- Tests: Node によるJS構文チェック、および `cognition.trace.enabled` / `active_persona_id = b` の検索ヘルパー直接検証が成功。`pytest` は仮想環境が存在しないPython 3.11本体を参照して起動できず未実施。
- Remaining: スモークテスト結果待ちを継続。Python 実行環境復旧後に `tests/test_migration_ui.py` を再実行する。
- Handoff: `docs/handoffs/2026-08-04_0059_codex_diagnostic-smoke-search.md`
- Related ADR / decision: 第12条、第17条。

## 2026-08-04 00:50 JST — Codex — 🩺にスモークテストの設定値を明示

- Status: completed
- Summary: 一般フラグ一覧では追いにくかった実機スモークテストの確認値を、専用区画へ技術キー付きで固定表示した。
- Changed:
  - `neuro_voice/ui/webview_app.py`: 診断ペイロードへ、設定の正本から読んだ`active_persona_id`を追加。
  - `neuro_voice/ui/assets/index.html`: `memory.retrieval_enabled` / `turn_integrity.persona_scope_enabled` / `turn_integrity.turn_frame_enabled` / `cognition.trace.enabled` / `active_persona_id`を`key = value`で表示する「スモークテスト確認」を追加。
  - `tests/test_migration_ui.py`: 必須5値がUIと診断ペイロードに存在することを検査する回帰テストを追加。
  - `docs/CODEMAP.md`: 診断のスモークテスト表示を追記。
- Behavior impact: 🩺 パネルの表示のみ。設定、ペルソナ切替、会話経路、DBは変更なし。
- Config / DB migration: なし。
- Tests: Node によるJS構文チェック、および5値の表示ヘルパー直接検証が成功。`pytest` は仮想環境が存在しないPython 3.11本体を参照して起動できず未実施。
- Remaining: スモークテスト結果待ちを継続。Python 実行環境復旧後に `tests/test_migration_ui.py` を再実行する。
- Handoff: `docs/handoffs/2026-08-04_0050_codex_diagnostic-smoke-check.md`
- Related ADR / decision: 第12条、第17条。

## 2026-08-04 00:44 JST — Codex — 配線診断の画面内検索を追加

- Status: completed
- Summary: 100件超の 🩺 配線診断から目的の項目を見つけやすくした。項目名だけでなく技術キー、説明、影響、関連フラグ、層で絞り込め、要注意だけの表示にも切り替えられる。
- Changed:
  - `neuro_voice/ui/assets/index.html`: 診断パネルへ検索欄・要注意フィルタ・一致件数・技術キー表示を追加。検索語とフィルタ状態は3秒自動更新後も保持する。
  - `tests/test_migration_ui.py`: 検索対象とUI制御の存在を検査する回帰テストを追加。
  - `docs/CODEMAP.md`: 配線診断の探索機能を追記。
- Behavior impact: 🩺 パネルの表示のみ。診断API、設定、会話経路、DBは変更なし。
- Config / DB migration: なし。
- Tests: Node によるJS構文チェックと、技術キー検索・要注意フィルタの直接検証が成功。`pytest` は仮想環境が存在しない `C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe` を参照して起動できず未実施。
- Remaining: スモークテスト結果待ちを継続。Python 実行環境復旧後に `tests/test_migration_ui.py` を再実行する。
- Handoff: `docs/handoffs/2026-08-04_0044_codex_diagnostics-search.md`
- Related ADR / decision: 第12条、第17条。

## 2026-08-04 02:00 JST — Claude — Codex への引き継ぎ文書を作成

- Status: completed
- Summary: Phase 7D の残り（⓪スモークテスト → ①実測 → ④⑦）を Codex が担当する。守ること・このプロジェクトで実際に踏んだ罠・次の作業順を1枚にまとめた。
- Changed:
  - `docs/handoffs/_FOR_CODEX.md`（新規）: 現在地、⓪の結果待ちであること（**結果が出る前にコードを直さない**）、失敗時の切り分けとロールバック手順、有効化済みフラグと**上げてはいけないフラグ**、マウント越しに SQLite を書かない（実際に `mind_luna.db` を壊した）、故障注入の習慣と**「呼び出し側が無い」パターン3例**、**自分のテストが空振りする3つの型**、ファイル地図、サンドボックス手順。
  - `docs/handoffs/_NEXT_SESSION.md`: 読む順の先頭へ `_FOR_CODEX.md` を追加。
- Behavior impact: なし（文書のみ）。
- Tests: 変更なし（**2761 passed / 17 subtests**、診断 要注意 0 件）。
- Remaining: ⓪実機スモークテスト、①30ターン実測、TurnLatency の実経路配線、④ PersonaContext のターン固定、⑦ 静的キャッシュの要否判断。**Phase 8 へは進まない。**
- Handoff: `docs/handoffs/_FOR_CODEX.md`

## 2026-08-04 01:00 JST — Claude — 所有者不明の記憶を移行し、分離を本番有効化。人格分離の観測点を追加

- Status: completed（移行は実データへ適用済み・**A/Bスモークテストは実機待ち**）
- Summary: `persona_scope_enabled: true` にすると隠れてしまう所有者不明の記憶が **neuro 202件 / luna 12件**あった（前回「0件」と報告したのはテスト用ペルソナだけを見ていた私の見落とし）。監査 → dry-run → バックアップ → トランザクション → 検証 → commit の順で1ペルソナへ帰属させ、そのうえで分離を本番有効にした。あわせて「分離が効いているか」を目で確かめるための観測点を Cognitive Trace へ追加した。
- Changed:
  - `neuro_voice/mind/legacy_scope_migration.py`（新規）: DB 監査（**DB名だけで持ち主を決めない**——設定のプリセットと照合し、一致しなければ BLOCKED）、`MigrationLedger`（migration_id / database_id / target_persona_id / affected_*_ids / previous_persona_values / started_at / completed_at / status / backup_reference）、`migrate_database()`（dry-run 既定・SQLite ファイルバックアップ・**空の行だけ**更新・別持ち主は CONFLICT で停止・件数と本文指紋で検証してから commit）、`rollback()`（**このジョブが空から設定した行だけ**を戻す。移行後に別処理が変えた行は触らない）。旧スキーマの列追加は `MemoryStore` を開くだけで済ませる——DDL の正本を2つにしない。
  - `neuro_voice/mind/store.py`: `owners_of()`（取れた記憶の持ち主とスコープだけを1回の索引引きで）、`scope_rejections()`（読めなかった候補を持ち主ごとに1回の集計で。**毎ターン全記憶を読み出さない**）。
  - `neuro_voice/mind/mind.py`: `persona_trace_fields()`。
  - `neuro_voice/cognition/trace.py`: `active_persona_id` / `active_persona_version` / `persona_epoch` / `retrieved_memory_persona_ids` / `retrieved_memory_scopes` / `cross_persona_rejection_count` / `cross_persona_rejection_persona_ids` / `legacy_unscoped_rejection_count` / `reflection_persona_ids` / `source_memory_persona_ids` / `persona_switch_state|from|to|epoch`、`persona_leak` 判定。**本文は入れない。**
  - `neuro_voice/pipeline.py`: 想起の2箇所で `_record_persona_trace()` を呼ぶ。
  - `config/config.yaml`: `memory.episodic_enabled` / `retrieval_enabled` / `turn_integrity.turn_frame_enabled` / `persona_scope_enabled` を **true**（移行完了が前提）。`reflection_enabled` / `duplicate_suppression_enabled` / `allow_legacy_unscoped_memory` は **false のまま**。
  - `tests/test_legacy_scope_migration.py`（新規, 20件）。`tests/test_turn_integrity.py` / `tests/test_turn_tracker_wiring.py`: フラグ期待値を更新（**危険な3つは false のまま**を引き続き検査）。
- Behavior impact: **ペルソナ間の記憶分離が本番で有効になった。** 移行済みなので既存の記憶は引き続き読める。観測点は ID と件数のみで、本文はログへ出さない。
- Config / DB migration: `mind_neuro.db` 202件 → `persona_id=neuro`、`mind_luna.db` 12件 → 列追加後 `persona_id=luna`。どちらも `scope=persona_private`（**共有へ上げない**）。総数・本文・embedding の指紋は移行前後で同一。履歴は `data/legacy_scope_migrations.json`、バックアップは `data/backups/legacy_scope/`。`mind_a.db` は設定に無いペルソナのため **BLOCKED（移行せず）**。
- Tests: 20件を追加（計 **2761 passed / 17 subtests**）、pyflakes clean、診断 **要注意 0 件**（110プローブ / warm 48ms）。故障注入で**自分の空振りテストを3件発見**（`DEFAULT_SCOPE` と比べていて定数ごと書き換わる／同一ペルソナへの上書きは値が変わらず検出できない／指紋を弱めても前後で同じ関数なので一致する）。修正後は7種すべてで赤くなることを確認。
- Remaining: **A/B スモークテストとポッポの既存記憶取得確認は実機待ち。** Reflection は Memory 分離が通ってから有効化する。Phase 7D は ①実測 ④⑦ が残り。
- 追記(01:40): **`cognition.trace.enabled` も true にした。** ここが false だと、今回追加した観測点は**どこにも書かれない**——スモークテストを丸ごと回してから「何も見えない」になるところだった。会話本文もプロンプトも書かない（第12条）ので、増えるのは1ターン1行の JSON だけ。出力先は `logs/cognitive_trace.jsonl`。
- Handoff: `docs/handoffs/2026-08-04_0100_claude_legacy-scope-migration.md`
- Related ADR / decision: 第2条、第12条、第17条。

## 2026-08-04 00:10 JST — Claude — Phase 7D ⑥: 関係値を persona_id + person_id の複合キーへ

- Status: completed（自動回帰済み・**実機未検証**）
- Summary: 関係値はファイル名（`relationships_<key>.json`）で分かれていたが、**中身に持ち主が書かれていなかった**。名前だけの分離は、ファイルをコピー・改名・バックアップ復元した瞬間に崩れ、しかも崩れても気づけない。ヘッダへ `persona_id` を持たせ、他ペルソナのファイルは**読まないし書かない**ようにした。
- Changed:
  - `neuro_voice/mind/relationship.py`: `RelationshipStore(path, cfg, persona_id="")`。保存時に `schema_version: 2` と `persona_id` を書く。読み込み時、**持ち主が違えば読まない**（`readonly`）。**`save()` も止める**——読まないだけにして書き込みを許すと、次の保存が**相手の関係値を空で上書きする**（読めないより悪い）。持ち主の無い旧ファイルは、そのペルソナが**引き取る**（ファイル名で既に分かれているので配布ではない）。冪等・`rollback_adoption()` で取り消し可。`persona_id` を渡さない呼び出しは従来どおり素通し。
  - `neuro_voice/mind/mind.py`: `RelationshipStore` を作る**4箇所すべて**へ持ち主を渡す（初期化・ペルソナ切替・person スコープ2箇所）。
  - `config/config.yaml`: `mind.relationship.allow_foreign_persona_file`（互換 fallback、**既定 false**）。
  - `tests/test_relationship_persona_key.py`（新規, 13件）。
- Behavior impact: 既存の `relationships_*.json`（schema 1・持ち主無し）は次回起動時にそれぞれのペルソナが引き取る。**値は動かないので、なかよし度は失われない。** 挙動が変わるのは、ファイルがペルソナ間で入れ替わっていた場合だけ——その時は読まずに警告を出す。
- Config / DB migration: JSON のヘッダに `persona_id` が付く（schema 1 → 2）。旧形式もそのまま読める。
- Tests: 13件を追加。全体 **2741 passed / 17 subtests**、pyflakes clean。故障注入6種（他人のファイルでも保存する・他人のファイルでも読む・互換フラグを既定で有効にする・持ち主を書かない・引き取りを冪等でなくする・`mind.py` の1箇所で渡し忘れる）で 1/3/3/5/2/1 件が赤くなることを確認。⑤と同じく「作る箇所の数」と「持ち主を渡す箇所の数」を比べるテストを入れた。
- Remaining: **Phase 7D の残りは ①（実測）④⑦のみ。** 実機計測はまだ——`memory.retrieval_enabled` が false のままだったため 8/3 の計測は成立していない（handoff 2320 §6 に手順）。
- Handoff: `docs/handoffs/2026-08-04_0010_claude_relationship-persona-key.md`
- Related ADR / decision: 第2条、第12条。

## 2026-08-03 23:20 JST — Claude — Phase 7D ⑤: 書き込み側へ persona_id を伝播

- Status: completed（自動回帰済み・**実機未検証**。計測のやり直しはこの後）
- Summary: Phase 7C は**読み側だけ**を直しており、**新しく作られる記憶は `persona_id` が空**のまま溜まっていた。実機ログの確認で、さらに悪いことが分かった——`EpisodicMemoryService.bind_persona()` も定義があるだけで**一度も呼ばれておらず**、読み側の絞り込みも空のまま効いていなかった。書き込みの持ち主を Store の1箇所で確定させ、Store を作り直す所（初期化・ペルソナ切替）の両方で結ぶようにした。
- Changed:
  - `neuro_voice/mind/store.py`: `bind_persona()` / `_owner()`。`add()` と `add_episode()` へ `persona_id` / `scope` を追加して書く。`reflections` へも `persona_id` / `scope`（**仮説は複数の記憶から作られるので、元が分かれていても仮説が共有だと逆に漏れる**）。既存 DB 用に `_migrate_reflection_columns()`——`CREATE TABLE IF NOT EXISTS` は既にある表へ列を足さないので、無いまま開くと仮説の保存が `OperationalError` で落ちる。
  - `neuro_voice/mind/episodes.py`: `_write()` が `persona_id` と `PERSONA_PRIVATE` を付ける。
  - `neuro_voice/mind/mind.py`: `_bind_persona_scope()` を追加し、`EpisodicMemoryService` を作る**2箇所とも**から呼ぶ。
  - `tests/test_persona_write_scope.py`（新規, 13件）。
- Behavior impact: **書き込みの持ち主はフラグに関係なく必ず付ける。** 空のまま溜めると、あとで分離を有効にした瞬間に全部隠れるため。フラグで変わるのは「持ち主不明の私的記憶を**拒否するか**」だけ——`persona_scope_enabled: false` の間は保存して警告のみ（無効時は持ち主が空でも読めるので、拒否すると読める記憶を落とす）。スコープを書いていない古い呼び出しは拒否しない（まだ直していない経路の記憶が黙って消えるのを避ける）。共有3スコープは持ち主不要。
- Config / DB migration: `reflections` へ2列を `ALTER TABLE ADD COLUMN`（既存行は空＝隔離側）。設定の追加なし。**既定値は1つも変えていない。**
- Tests: 13件を追加。全体 **2728 passed / 17 subtests**、pyflakes clean。故障注入6種（切替時に結び直さない・書き込みの持ち主をフラグ付きにする・エピソードに持ち主を付けない・strict でも保存する・共有スコープも持ち主必須にする・reflections の列移行を外す）で赤くなることを確認。
- Remaining: **Relationship の複合キー（⑥）は未着手。** 実機計測はこれから——`memory.retrieval_enabled` / `reflection_enabled` が false のままだったため、8/3 の30ターン計測は「記憶検索あり」が0ターンで成立していなかった。
- Related ADR / decision: 第2条、第12条。

## 2026-08-03 13:10 JST — Claude — ペルソナの削除をGUIから可能に（記憶ファイルは残す）

- Status: completed（自動回帰済み・**GUI実機未検証**）
- Summary: 12:10 で追加した新規作成の対になる削除。**`config.yaml` の一覧から外すだけで、`data/` の記憶・関係値ファイルには触らない。** 消すと戻せないため——設定を1つ消したつもりで、そのペルソナと話した記録が全部無くなるのがいちばんまずい。一覧から外すだけなら、同じ ID で作り直せば記憶も戻る。消さない代わりに、**どのファイルが残っているかを返して伝える。**
- Changed:
  - `neuro_voice/ui/webview_app.py`: `_remove_yaml_preset()`（プリセットのブロックだけ削除。コメントと他プリセットは保つ）、`PERSONA_DATA_PATTERNS` / `_persona_data_files()`、API `delete_persona()`。**使用中のペルソナと最後の1つは拒否**（前者は今喋っている人格が消える、後者は選べるものが無くなる）。判定は削除の**前**。
  - `neuro_voice/ui/assets/index.html`: ペルソナタブに「選択中のペルソナを削除」。`confirm()` を1回挟む。削除後は残ったファイル名を画面へ出す。
  - `tests/test_persona_create.py`: 7件追加（計35件）。
- Behavior impact: 削除操作をするまで何も起きない。削除しても記憶ファイルは残るので、同じ ID で作り直せば元に戻る。
- Config / DB migration: なし。
- Tests: 7件を追加。全体 **2715 passed / 17 subtests**、pyflakes clean。故障注入4種（記憶ファイルも消す・使用中/最後の判定を削除の後ろへ動かす・ブロックの範囲を1行だけにする・残ファイル一覧から1つ抜く）で赤くなることを確認。`mind.py` が作るペルソナ別ファイル名を正規表現で拾い、一覧が取りこぼしていないかを見るテストも入れた（`mind.py` 側にファイルが増えた時に気づける）。
- Remaining: 複製・リネームは無い。**本当にファイルごと消す機能は入れていない**——戻せない操作なので、必要になったら退避（`data/deleted/` へ移す）とセットで設計する方がよい。
- Handoff: `docs/handoffs/2026-08-03_1210_claude_persona-create.md`（§7 に追記）

## 2026-08-03 12:40 JST — Claude — 追加直後のペルソナで「保存は一部失敗」になる不具合を修正

- Status: completed（自動回帰済み・**GUI実機未検証**）
- Summary: 12:10 の追加機能に不具合。新規プリセットに `conversation_traits` の行を書いていなかったため、「適用」時に `_persist_yaml_mapping()` が書き換える対象を見つけられず False を返し、**追加した直後のペルソナだけ**「config.yaml への保存は一部失敗」と出ていた（チビが実機で発見）。型のテストは全部通っていた——**往復させていなかったのが原因**。修正しながら同種のもう1件（呼び出し側が渡し忘れた `PERSONA_FIELDS` の項目が丸ごと欠ける）も見つけた。
- Changed:
  - `neuro_voice/ui/webview_app.py`: `_persist_yaml_new_preset()` に `traits` 引数を追加し、`conversation_traits` の行を必ず書く。あわせて **`PERSONA_FIELDS` を必ず全部書く**ようにした（呼び出し側に任せない。1つ欠けると同じ「一部失敗」が出る）。`create_persona()` は生成した traits を書き込みと実行時設定の両方へ渡す。
  - `tests/test_persona_create.py`: **往復のテスト**を2件追加。`apply_persona()` が書き換える鍵（`PERSONA_FIELDS` 全部・`system_prompt`・`conversation_traits`）を、作った直後のプリセットに対して**実際に書き戻せること**を確認する。
- Behavior impact: 追加したペルソナで「適用」が正常に保存されるようになる。**12:10 より前に追加したペルソナがある場合は、`config.yaml` の該当プリセットへ `conversation_traits: {}` の行を手で足すか、作り直しが要る。**
- Config / DB migration: なし。
- Tests: 2件を追加（計28件）。全体 **2708 passed / 17 subtests**、pyflakes clean。故障注入2種（`conversation_traits` を書かない＝今回の不具合そのもの／呼び出し側が渡した項目だけ書く）で 2件・1件が赤くなることを確認。
- Remaining: 削除・複製・リネームは引き続き無い。
- Handoff: `docs/handoffs/2026-08-03_1210_claude_persona-create.md`（§6 に追記）

## 2026-08-03 12:10 JST — Claude — ペルソナの新規追加をGUIから可能に

- Status: completed（自動回帰済み・**GUI実機未検証**）
- Summary: `apply_persona` は `presets` に無いキーを拒否するため、**新しいペルソナは config.yaml を手で編集しないと増やせなかった**。既存の `_persist_yaml_value` は行の置換しかできず新規キーを書けないので、`persona.presets` の末尾へブロックごと足す関数を追加した。**`yaml.dump` で全体を書き直さない**——`config.yaml` のコメントは設定の説明そのもので、1つ足すたびに失うのは割に合わない。
- Changed:
  - `neuro_voice/ui/webview_app.py`: `_PERSONA_KEY_RE`（**キーは `mind_<key>.db` などのファイル名になる**ので、`..` や `/` を通さない）、`_persist_yaml_new_preset()`、API `create_persona()`。中身は**空**で作る（既存プリセットから写すと、新しい人格が最初から他人の口調を持つ）。**`persona.active` は触らない**——記憶も関係値も空の人格へいきなり切り替わると取り消しが効かないので、内容を書いてから「適用」で切り替える。
  - `neuro_voice/ui/assets/index.html`: ペルソナタブに「新しいペルソナを追加」（ID・表示名・追加ボタン）。追加後は一覧を更新して選択状態にするだけで、切り替えはしない。
  - `tests/test_persona_create.py`（新規, 26件）。
- Behavior impact: 既存ペルソナと会話の挙動は変わらない。追加操作をするまで何も起きない。
- Config / DB migration: なし（新しいペルソナを作ると `mind_<key>.db` 等が初回起動時に作られる）。
- Tests: 26件を追加。全体 **2706 passed / 17 subtests**、pyflakes clean。故障注入4種（キー検査を緩める・値を引用しない・presets の外へ足す・追加時に active も切り替える）で 9/2/3/1 件が赤くなることを確認。
- Remaining: **削除・複製・リネームは無い。** 削除は `mind_<key>.db` や関係値ファイルの扱いを決める必要があり、追加より重い。GUI の見た目は実機未確認。
- Related ADR / decision: 第2条、第19条。

## 2026-08-03 11:20 JST — Claude — Phase 7D ②③: TurnFrame / TurnCommitLedger を実経路へ配線

- Status: **partial**（`_NEXT_SESSION.md` §2 の②③のみ。①実測と④⑤⑥⑦は未着手）
- Summary: Phase 7C で型もテストも揃っていた `TurnFrame` / `TurnCommitLedger` に**呼び出し側が無かった**問題を閉じた。実装があることと効いていることは違う——この配線が無い間は、実機で同じ文が二度聞こえても `duplicate_stage` には何も入らなかった。あわせて、Phase 7D 前半で足したのに**一度も呼ばれていなかった**計測項目を2つ実配線した（Discord の `source_type`、`bind_persona`）。**機能フラグは既定 false のまま**なので、既定では挙動も記録も変わらない。
- Changed:
  - `neuro_voice/cognition/turn_tracker.py`（新規）: `TurnTracker`。ターン開始時に `TurnFrame` を**1件だけ**作る（`TurnMetrics.turn_id` が正本・冪等）、確定が二度来たら**どの層か**を `TurnMetrics.duplicate_stage` へ残す、**既定では止めず記録して素通し**、中断した再生は確定を取り消す（重複を嫌って欠落を作らない）、ID が無い呼び出しは重複扱いにしない。
  - `neuro_voice/pipeline.py`: `_begin_turn()` を追加し `speech_end` 2箇所と `respond_text()` から呼ぶ。`SPEECH_REQUEST`（`decision_id=utterance_id`＝**1発話から出る最終応答は1件**）、`_speak_loop()` の `commit_chunk`（**発話ループごとに 0 から数える**——断片ごとに新 ID を振ると検査が常に通ってしまう）、`_on_play_start` の `PLAYBACK`、`_on_complete` 未完了時の `release`、`_respond_direct_text()` の `TTS_JOB`/`PLAYBACK`、`LLM_OUTPUT`/`SURFACE_REALIZED` の記録。
  - `neuro_voice/discord_bridge/bot.py`: 同じ `TurnTracker` を使う `_begin_turn()`。**`source_type` が Discord でも `local_mic` のままだったのを修正**——前半で作った経路別合計が、分けているつもりで混ざっていた。`_generate_and_speak()` の `SPEECH_REQUEST`、`_speak()` の `TTS_JOB`/`PLAYBACK`（chunk_id 鍵）、`LLM_OUTPUT`/`SURFACE_REALIZED`。
  - `neuro_voice/diagnostics/wiring.py`: プローブ `turn.tracker_records_duplicate` を追加（**実際に往復させる**。ソースを読むだけの点検は故障注入をすり抜ける）。
  - `config/config.yaml`: `turn_integrity.duplicate_suppression_enabled` / `frame_capacity` / `commit_capacity` を追加。**すべて既定 false / 保守的な値。既存のデフォルトは1つも変えていない。**
  - `tests/test_turn_tracker_wiring.py`（新規, 27件）、`tests/test_turn_latency_wiring.py`: 固定長の窓を関数末尾までに変更（順序を見たいのであって行数を見たいのではない）。
- Behavior impact: **既定では何も変わらない**（`turn_frame_enabled: false`）。有効にしても記録するだけで発話は止めない。止めるのは `duplicate_suppression_enabled` を別途 true にした時だけ。フラグと無関係に変わるのは 🩺 の表示2箇所——Discord のターンが Discord として集計されるようになり、ペルソナ欄が埋まるようになった。
- Config / DB migration: 新規キー3つのみ。DB 変更なし。
- Tests: 27件を追加。全体 **2683 passed / 17 subtests**、対象ファイルの pyflakes clean、診断**要注意 0 件**（warm 47ms / **110プローブ**）。**故障注入13種**すべてでテストまたはプローブが赤くなることを確認。そのうち1種で**自分のバグを1件発見**（プローブの冪等性チェックが「2回目の戻り値」と「今入っている物」を比べていて、作り直しても両方が新しい方になるため常に一致していた。テスト側は同じ故障で赤くなった）。
- Remaining: **①実測は未実施。** 重複の原因も未特定——特定できる準備が整っただけ。`TurnFrame.duplicate_site()` は1ターン1ジョブ前提でストリーミングでは誤検出するため実経路では使っていない。Discord 側の断片検査は Local より弱い（chunk_id 鍵なので「同じ断片の再配送」しか見えない）。④⑤⑥⑦は未着手で、**⑤（書き込み側の `persona_id`）は Phase 8 のブロッカーのまま**。
- Handoff: `docs/handoffs/2026-08-03_1120_claude_turn-tracker-phase7d-wiring.md`
- Related ADR / decision: 第2条、第12条、第19条。

## 2026-08-03 07:40 JST — Claude — Phase 7D（前半）: 実経路の計測終点を修正、計測の背骨を拡張

- Status: **partial。Phase 7D は完了していない**（実測未実施・§10〜13 未着手）
- Summary: 実経路を監査したところ、**計測の背骨は既にあった**（`utils/latency.py` の `TurnMetrics`。`pipeline` と `bot` が引数で運んでいる）。Phase 7C の `TurnLatency` は二重になるので、新しい計測系を足さず**既存を拡張**した。そのうえで、既存の背骨に**終点の誤りが2箇所**見つかった——`play_start` という1つの名前に「音声が出来た時点」と「鳴り始めた時点」が混ざっており、**再生開始の区間が 0ms として集計され、合計も実際より短く出ていた**。ここを直した。
- Changed:
  - `docs/audits/2026-08-03_turn_path.md`（新規）: Local/Discord の実経路地図。各段階の実ファイル・実関数・同期/非同期・既存 mark・turn_id を渡せる地点。
  - `neuro_voice/pipeline.py`: `_respond_direct_text()` で `mark("play_start")` が `playback.play()` の**前**にあったのを修正。`tts_audio_ready` を分離。`_speak_loop()` にも `tts_audio_ready` を追加（`play_start` は元から callback で正しい）。
  - `neuro_voice/discord_bridge/bot.py`: `self._source.write(pcm)` は **Opus キューへ積むだけ**なのに `play_start` を打っていたのを `discord_enqueued` へ変更し、`vc.play()` 後に `play_start` を打つ。DAVE 経路にも `discord_enqueued` を追加。
  - `neuro_voice/utils/latency.py`: `turn_id` / `source_type` / persona / `origin_turn_id`、`skip()` と `not_applicable`（**通らなかった段階を 0ms にしない**）、p50/p90/p95/min/max、経路別の合計、Discord 固有区間。percentile は **nearest-rank**（補間すると一度も起きていない値が p95 になる）。
  - `tests/test_turn_latency_wiring.py`（新規, 16件）。
- Behavior impact: **会話の挙動は変わらない。** 変わるのは 🩺 に出る数字で、**これまでより長く（正しく）出る**——再生開始までの区間が計上されるようになったため。以前の数値と直接比較しないこと。
- Config / DB migration: なし。
- Tests: 16件を追加。全体 **2712 passed / 17 subtests**、pyflakes clean。故障注入4種（生成完了を再生開始へ戻す・キューを再生開始へ戻す・通らない段階を 0ms 扱い・percentile を補間へ）でテストが赤くなることを確認。
- Remaining: **実測が未実施**（マイク・Whisper・Ollama・SBV2 が要る）。`TurnCommitLedger` の実配線、`TurnFrame` の実経路生成、PersonaContext のターン固定、Memory/Reflection への `persona_id` 伝播、Relationship 複合キー、静的キャッシュ、🩺 のレイテンシ区画が**すべて未着手**。
- Handoff: `docs/handoffs/2026-08-03_0740_claude_turn-path-phase7d-partial.md`
- **引き継ぎ入口**: `docs/handoffs/_NEXT_SESSION.md`（新しいセッション／別AIはここから読む）
- Related ADR / decision: 第2条、第20条。

## 2026-08-03 05:10 JST — Claude — Phase 7C: 重複発話・レイテンシ回帰・ペルソナ混入の診断基盤と最小修正

- Status: partial（**計測基盤と特定済みの原因は修正。実測値は実機待ち**）
- Summary: 実機で3つの回帰が出た（同じ文の繰り返し / 応答開始が3000→4000-5000ms / ペルソナ変更後に旧ペルソナらしい反応）。**推測で3つ同時に直さず**、まず再現・特定できる形にした。重複は生成／発話要求／TTS／再生の4層に分けて分類でき、ID による一意性で再配送を止める。レイテンシは15工程＋合計を会話種別ごとに p50/p90/p95 で出せる。ペルソナは6スコープを定義し、**DB 検索の時点で**絞る。**新機能は追加していない。**
- Changed:
  - `neuro_voice/cognition/turn_integrity.py`（新規）: `TurnFrame`（相関ID・6段階の文章記録。**全文は残さずハッシュ/文数/文字数/隣接類似度のみ**）、`DuplicateSite` 4分類、`TurnCommitLedger`（speech_request / tts_job / playback / streaming chunk の一意性）、`collapse_adjacent_duplicates()`。
  - `neuro_voice/cognition/turn_latency.py`（新規）: 15工程＋`turn_end_to_first_audio_ms`、`LatencyRecorder`（p50/p90/p95・会話種別・warmup除外・機能別 delta）、**待ち時間と実処理の分離**、`detect_tool_intent()`。
  - `neuro_voice/cognition/persona_scope.py`（新規）: `PersonaScope` 6種、`SHARED_SCOPES` は3つだけ、`readable()`、`sql_scope_filter()`、`PersonaContext`（id+version+**epoch**）、`PersonaSwitch` 状態機械、`PersonaStamp`、`cache_key()`。
  - `neuro_voice/mind/store.py`: `memories` へ `persona_id` / `scope` 列（**既定は空。「不明」を「共有」に読み替えない**）、`episodes()` に検索時点のスコープ条件。
  - `neuro_voice/mind/episodes.py`: `bind_persona()` と `_existing()` の絞り込み。**人格未設定なら引数を足さない**（古い Store で `TypeError` が `suppress` に飲まれ、検索が静かに0件になる事故を防ぐ）。
  - `neuro_voice/mind/mind.py`: `persona_switch()` / `persona_context` / `accepts_persona_result()`、切替時に in-flight を drain してから世代を進め、`_tool_runtime` を破棄。
  - `neuro_voice/memory/conversation.py`: `switch_persona()`。**旧ペルソナの AI 発話・保留話題・中断発話を捨てる。**
  - `neuro_voice/ui/webview_app.py`: ペルソナ切替で `switch_persona()` を呼ぶ。**ここが `set_system_prompt()` だけだったのが漏れの直接原因。**
  - `neuro_voice/diagnostics/wiring.py`: 「ターン」層のプローブ9件。
  - `config/config.yaml`: `turn_integrity.*`。**計測系は既定 false、`skip_tool_path_without_intent` と `drop_history_on_persona_switch` だけ true。**
  - `tests/test_turn_integrity.py`（新規, 80件）。
- Behavior impact: 通常会話の見た目は変わらない。**ペルソナ切替だけ挙動が変わる**——会話履歴を引き継がなくなる（これが漏れの直接原因だった）。記憶のスコープ絞り込みは `persona_scope_enabled` が false のうちは効かない（列は追加されるが検索条件に入らない）。
- Config / DB migration: `memories` へ2列を `ALTER TABLE ADD COLUMN` で追加（既存行は空のまま＝隔離側）。設定は新セクション1つ。
- Tests: `tests/test_turn_integrity.py` 80件。全体 **2695 passed / 17 subtests**、pyflakes clean、診断**要注意 0 件**（warm 44ms / 109プローブ）。故障注入7種でテストとプローブが赤くなることを確認。
- Remaining: **レイテンシの実測はまだ。** 計測できる状態にしただけで、3000→4000-5000ms の内訳は実機で取る必要がある。`TurnFrame` / `TurnLatency` を pipeline・bot の実経路へ挿す配線が未着手（型と集計だけ）。`persona_id` を書き込み側（`MemoryCandidate` / `Reflection` / `Relationship`）へ伝播させる配線も未着手。
- Handoff: `docs/handoffs/2026-08-03_0510_claude_turn-integrity-phase7c.md`
- Related ADR / decision: 第2条、第12条、第17条、第19条。

## 2026-08-03 02:30 JST — Claude — Phase 7B: ツール結果の会話統合・Discord配線・最小書き込み実経路

- Status: completed（自動回帰済み・**実機未検証。段階3と段階5は耳で確かめるまで完了扱いにしない**）
- Summary: Phase 7 は**実行して記録するところで止まっていた**——`ToolResult` は `mind.db` に入るが、そこから先が繋がっておらず、調べても何も言わない状態だった。今回そこを閉じた。結果を `ToolOutcomeEvent` へ変換し、Action Selector の候補（`REPORT_TOOL_*` 5種＋`REMAIN_SILENT`）へ載せ、`COGNITIVE_TOOL_OUTCOME` として Speech Gate を通す。あわせて信頼境界（Planner へ渡すのは6項目の構造化結果だけ）、`test_scratch_create_text` の実書き込みとパス安全性、Discord の人物・チャンネル照合、限定再計画、Phase 6F の二重起動 flake の正本修正。**Phase 7B の機能フラグは全て false。**
- Changed:
  - `neuro_voice/cognition/tool_report.py`（新規）: `ToolOutcomeEvent`、`ToolPayload`（structured/raw/evidence/user_safe/diagnostic の5区画）、`redact()`（**許可制**）、`planner_payload()`、`ReportLedger`、`report_candidates()`。
  - `neuro_voice/cognition/scratch_tool.py`（新規）: `ScratchWriter`。`normalise_relative()`、symlink 実体解決、排他的作成、上書き拒否、`rollback()`。
  - `neuro_voice/cognition/tools.py`: `ToolOperation.user_safe_keys`（**話してよい鍵の宣言**）、`test_scratch_create_text` を登録。
  - `neuro_voice/cognition/types.py`: `REPORT_TOOL_SUCCESS` / `REPORT_TOOL_FAILURE` / `REPORT_PARTIAL_RESULT` / `ASK_TOOL_RECOVERY_CONFIRMATION` / `REPORT_UNKNOWN_OUTCOME`（全て `OPTIONAL`）。
  - `neuro_voice/cognition/rollout.py`: `SpeechSource.COGNITIVE_TOOL_OUTCOME`、`TOOL_OUTCOME_ALLOWLIST`、`SpeechRequest` へ `execution_id` / `tool_outcome_event_id` / `conversation_id` / `channel_id` / `outcome_verified` / `already_reported`、`check_speech()` に9条件と `active_conversation_id` / `active_channel_id`。
  - `neuro_voice/cognition/planning.py`: `ActionIntent` へ `conversation_id` / `channel_id` / `origin`、`fingerprint()` が依頼者と会話も覆う、`BoundedPlan.replan_of`、`can_replan()` に `effect_category` / `side_effect_confirmed`、`REPLANNABLE_EFFECTS`。
  - `neuro_voice/cognition/tool_exec.py`: 確認へ `conversation_id` / `channel_id`、`approve()` に会話・チャンネル照合と `ambiguous_utterance`、**timeout の UNKNOWN_OUTCOME 判定を `side_effect_free` 基準へ修正**。
  - `neuro_voice/cognition/tool_runtime.py`: 実行後の順序（保存→検証→Step→Goal/World→出来事→候補）、`speech_request()` / `claim_report()` / `bind_scratch()` / `_readback_for()`、レイテンシ4項目。
  - `neuro_voice/cognition/trace.py`: 会話統合 17 項目、`note_tool_result_dialogue()` / `note_delivery_match()` / `_dialogue_snapshot()`。
  - `neuro_voice/mind/store.py`: `confirmations` へ `conversation_id` / `channel_id`、`tool_executions` へ `reported_at`、`mark_execution_reported()` / `unreported_executions()`、`_migrate_tool_columns()`（**既存DBへも `ALTER TABLE ADD COLUMN` で足す**）。
  - `neuro_voice/mind/migration.py`: `operation_key()` と `_by_key` による get-or-create（**sleep ではなく同一性で1件に束ねる**）、`MigrationJob.operation_key`。
  - `neuro_voice/pipeline.py` / `neuro_voice/discord_bridge/bot.py`: 同じ `ToolRuntime` を使う配線。Discord 側に `_bind_tool_runtime()` と `tool_context()`。
  - `neuro_voice/diagnostics/wiring.py`: 「道具」層へ8プローブ追加（うち `migration.single_start` は移行層）。
  - `config/config.yaml`: `tools.result_dialogue_enabled` / `discord_enabled` / `test_scratch_enabled` / `test_scratch_root` / `bounded_replan_enabled`。**全て false・root は空**。
  - `tests/test_tool_dialogue.py`（新規, 72件）。
- Behavior impact: **既定では何も変わらない。** `tools.result_dialogue_enabled: false` なら Phase 7 と同じ——実行して記録するところで止まる。有効化しても、未検証の成功は「できた」と言えず、同じ結果は二度言わず、頼まれたチャンネル以外へは出ない。
- Config / DB migration: `confirmations` に2列、`tool_executions` に1列を追加。`CREATE TABLE IF NOT EXISTS` は既存の表に列を足さないため、起動時に `ALTER TABLE ADD COLUMN` で補う（既存の行は壊さない。既定値で埋まる）。
- Tests: `tests/test_tool_dialogue.py` 73件を追加。全体 **2615 passed / 17 subtests**、pyflakes clean（既存の未使用変数を除く）、診断**要注意 0 件**（warm 38ms / 100プローブ）。故障注入6種でテストとプローブが赤くなることを確認。
- Remaining: **実機未検証**。段階3（Local 音声品質）と段階5（Discord 実機）は耳で確かめるまで完了扱いにしない。`ToolOutcomeEvent` → Conversation Planner の実プロンプト組み立ては未接続（`planner_payload()` は用意済み、渡す側が未着手）。
- Handoff: `docs/handoffs/2026-08-03_0230_claude_tool-dialogue-phase7b.md`
- Related ADR / decision: 第2条、第12条、第17条、第19条、第20条。

## 2026-08-02 23:55 JST — Claude — Phase 7: 目標から安全に道具を使う経路（既定オフ）

- Status: completed（自動回帰済み・**実機未検証**）
- Summary: Phase 6 まで、副作用のある操作は**一つも接続されていなかった**（監査 `docs/audits/2026-08-02_tool_paths.md`。tool/function calling を使っておらず、LLM から直接ツールを呼ぶ経路は存在しない）。そのため今回は「危ない経路を塞ぐ」のではなく、**まだ何も無い所へ最初から門を付けて繋ぐ**作業になった。ツールの副作用分類をコード側へ置き、1〜3手の Bounded Plan・Action Intent・Plan Admission Gate・Confirmation・Tool Gate（12検査）・冪等台帳・Outcome Verification を追加し、READ_ONLY の `web_search` 1種類だけを実経路へ繋いだ。**Phase 7 の機能フラグは全て false。**
- Changed:
  - `neuro_voice/cognition/tools.py`（新規）: `EffectCategory` 6分類、`ParameterSpec`（`sensitive` / `is_target`）、`ToolOperation` / `ToolCapability` / `ToolRegistry`、既定台帳4種。副作用分類は**設定でもLLM説明文でもなくコード**が持つ。
  - `neuro_voice/cognition/planning.py`（新規）: `ActionIntent`、`BoundedPlan`（上限3手）、`PlanAdmissionGate`（ACCEPT / REQUIRE_CLARIFICATION / REQUIRE_CONFIRMATION / REJECT / WAIT）、`can_replan`、`plan_candidates`（Action Selector の候補列へ載せる）。
  - `neuro_voice/cognition/tool_exec.py`（新規）: `ConfirmationStore`（hash 拘束・本人確認・単一 pending・TTL・一度でCONSUMED）、`ToolExecutionRequest`、`ToolGate`（12検査）、`IdempotencyLedger`、`ToolExecutor`（別スレッド・限定Retry・Timeout・Cancel）、`ToolResult` 6状態、`verify_result`、`apply_tool_outcome`、`restart_disposition`。
  - `neuro_voice/cognition/tool_runtime.py`（新規）: 設定から一式を組み立てて実経路へ繋ぐ層。`propose()` / `execute()` / `cancel()` / `close_turn()` / `recover()`。
  - `neuro_voice/cognition/types.py`: `ActionType` へ `EXECUTE_TOOL` / `ASK_CONFIRMATION` / `SUGGEST_ACTION` / `CANCEL_PLAN` / `WAIT` を追加（実行は `FORBIDDEN`＝発話ではない）。
  - `neuro_voice/cognition/executor.py`: `EXECUTE_TOOL` / `WAIT` を内部行動として扱う（沈黙と結果を分ける）。
  - `neuro_voice/cognition/goals.py`: `decide_permission()` に `category=` を追加。**判定表は1つのまま**ツール側から使えるようにした。
  - `neuro_voice/cognition/trace.py`: 道具まわり 26 項目と `_tool_snapshot()`、`note_plan` / `note_permission` / `note_confirmation` / `note_tool_gate` / `note_tool_outcome`。
  - `neuro_voice/mind/store.py`: `bounded_plans` / `confirmations` / `tool_executions` 表と `mark_executions_unknown()`。**引数の値は実行の記録へ入れない。**
  - `neuro_voice/mind/mind.py`: `tool_runtime()`。
  - `neuro_voice/pipeline.py`: `_bind_tool_runtime()`——`web_search` を実物の `DeepSearch` へ束ねる。
  - `neuro_voice/diagnostics/wiring.py`: 「道具」層のプローブ15件と Phase 7 フラグの依存関係。
  - `config/config.yaml`: `planning` / `tool_execution` / `confirmation`。**全て false**（`planning.max_steps: 3`、`max_replans: 1`、`confirmation.ttl_seconds: 120` のみ数値）。
  - `tests/test_tool_execution.py`（新規, 82件）。
- Behavior impact: **既定では何も変わらない。** 全フラグ false のため、計画は `planning_disabled`、門は `tool_execution_disabled` で止まる。有効化しても、READ_ONLY 以外は確認が要り、確認は引数へ拘束され一度で使い切る。
- Config / DB migration: `mind.db` へ3表を追加（`CREATE TABLE IF NOT EXISTS`。既存データへの変更なし）。設定は新規セクション3つ、全て false。
- Tests: `tests/test_tool_execution.py` 82件を追加。全体 **2542 passed / 17 subtests**、pyflakes clean、診断**要注意 0 件**（warm 27.5ms / 90プローブ）。故障注入10種でテストとプローブが赤くなることを確認。
- Remaining: **実機未検証。** 有効化は `docs/handoffs/2026-08-02_2355_claude_tool-execution-phase7.md` の手順どおり8段階で。`test_a_double_click_does_not_start_two_jobs`（Phase 6F）が稀に落ちる既知の timing flake（Phase 7 とは無関係）。
- Handoff: `docs/handoffs/2026-08-02_2355_claude_tool-execution-phase7.md`
- Related ADR / decision: 第2条（LLMが意味・コードが検証）、第5条、第12条、第17条、第19条、第20条。

## 2026-07-31 22:02 JST — Codex — SBV2の読点分割と単一スタイル強度を安定化

- Status: completed（自動テスト実行は環境上限で未完）
- Summary: SBV2の音が以前より不自然に聞こえる報告を、実行ログ・モデル情報・合成経路から調査した。短い読点断片を別々に合成して毎回抑揚を閉じていた経路と、`Neutral`しか持たないtsukuyomiへ感情由来のstyle weight変動を掛けていた経路を修正した。
- Changed:
  - `neuro_voice/utils/textseg.py`: 句点等は従来どおり即時境界、読点は20文字以上の時だけ低遅延境界とするsoft boundaryへ変更。短い前置きと本文を一つの意味・抑揚単位でSBV2へ渡す。
  - `neuro_voice/tts/style_bert_vits2.py` / `tts/factory.py`: 利用可能styleが一つだけのモデルでは設定基準のstyle weightを固定し、turnごとのintonation倍率で単一voice embeddingを揺らさない。話速・length・固定有無を診断ログへ追加。
  - `config/config.yaml`: `lock_single_style_weight: true`を追加。過去の誤学習で残っていた無関係な読み置換一件を除去。
  - `tests/test_style_bert_vits2.py` / `tests/test_interaction.py`: 単一style固定、明示解除、短い読点の結合、長い読点の低遅延flushを追加。
- Behavior impact: Local/Discord共通のSentenceSegmenterで、短い読点ごとの語尾化・抑揚再開始が減る。tsukuyomiは`Neutral w=1.00`を維持する。複数styleを持つamitaro等の感情強度変化は維持する。
- Config / DB migration: DBなし。新設定は既定true。falseで従来の単一style weight変動へ戻せる。GUI再起動が必要。
- Tests: 回帰テストを追加。実行はCodex環境の実行枠上限によりPython起動前に拒否され、未実施。構文・実機音質を次回最優先で確認する。
- Remaining: GUI再起動後、同じ2〜3文を通常・喜び・落ち着きで読み、ログが`single_style=True / w=1.00`になることと、短い読点で不自然に語尾が閉じないことを聴感確認する。SBV2モデル固有の読み間違いは具体的な語句ごとに辞書へ登録する。
- Handoff: `docs/handoffs/2026-07-31_2202_codex_sbv2-pronunciation-stability.md`
- Related ADR / decision: D-036、R-044。

## 2026-07-31 21:48 JST — Codex — Remote LauncherのWindows完全非表示起動

- Status: completed
- Summary: `pythonw.exe`が見つからない場合に`RemoteLauncher.bat`が`python.exe`をコンソール付きで常駐させ、黒い画面が残る問題を修正した。通常起動と自動起動の正規入口をwindow style 0のVBScriptへ変更した。
- Changed:
  - `RemoteLauncher.vbs`: venvの`pythonw.exe`、次に`python.exe`、最後にPATHの`pythonw.exe`を選び、`WScript.Shell.Run(..., 0, False)`で完全非表示起動する入口を追加。
  - `RemoteLauncher.bat`: Pythonを直接常駐させず、非表示VBScriptへ即時委譲する互換入口へ変更。
  - `InstallAutoStart.bat`: Startup shortcutのtargetを`.bat`から`wscript.exe //B //Nologo RemoteLauncher.vbs`へ変更。
  - `tests/test_remote_launcher.py`: python.exe fallbackもwindow style 0、bat委譲、自動起動shortcutのwscript利用を回帰固定。
  - `docs/遠隔ランチャー.md`: 手動起動の正規入口を`RemoteLauncher.vbs`へ更新。
- Behavior impact: 通常起動・Windowsログオン時ともコマンドプロンプトを作らず、ランチャーはトレイとログだけで常駐する。明示診断用`RemoteLauncher_Console.bat`だけは従来どおりコンソールを表示する。
- Config / DB migration: DBなし。既存Startup shortcutは実機上でもwscript targetへ更新済み。
- Tests: focused `21 passed`、変更Pythonの`py_compile`成功、最終全体 `1151 passed / 2 skipped in 44.72s`。skipは既存`pyflakes`未導入のみ。
- Remaining: 現在起動中の旧console residentは一度終了し、`RemoteLauncher.vbs`または更新済み自動起動shortcutから起動し直す必要がある。
- Handoff: `docs/handoffs/2026-07-31_2148_codex_remote-launcher-hidden.md`
- Related ADR / decision: D-035。

## 2026-07-30 23:20 JST — Codex — 重複応答の誤検知分離と自然な一回再生成

- Status: completed
- Summary: 重複guardが古い複数turnの定型的な短い導入まで拾い、固定の診断謝罪を会話へ読み上げる問題を修正した。真の自己反復は発話前に止めたまま、一度だけ新内容へ再生成し、自然な終了なら沈黙できる。
- Changed:
  - `neuro_voice/dialogue/echo.py`: 比較元をassistant自己反復とuserオウム返しへ型分離し、後者は高い閾値・長さ条件を使う。短い定型導入だけでは反復確定せず、判定種別とscoreを本文なしで診断可能にした。
  - `neuro_voice/pipeline.py` / `neuro_voice/discord_bridge/bot.py`: Local/Discordとも比較対象を直近中心へ限定し、重複種別をevent/logへ記録。確定後のguardを即時閉じて同じ候補の二重flush/logも防ぐ。固定謝罪を廃止し、同一turn内の一回だけ新内容を再生成する。再生成も重複または`NO_REPLY`なら発話しない。
  - `neuro_voice/dialogue/repair.py`: 明示訂正だけは決定的に受理し、通常反復には一回再生成用の内部contextとsilence token正規化を提供する。
  - `tests/test_reply_echo.py` / `tests/test_conversation_repair.py`: 短い聞き返しの非誤検知、比較元種別、短いuser語の引用、固定謝罪廃止、再生成の沈黙選択を回帰固定。
- Behavior impact: 「同じ返答を繰り返しかけた」というシステム都合の台詞を通常会話で読まない。本当の直前返答ループはUI/TTS前に止まり、必要なturnは一度だけ別内容を返し、内容を足す必要がなければ自然に会話を閉じる。Local/Discord共通。
- Config / DB migration: なし。
- Tests: focused `46 passed`、変更Pythonの`py_compile`成功、最終全体 `1148 passed / 2 skipped in 55.96s`。skipは既存`pyflakes`未導入のみ。
- Remaining: 実GUIで重複再生成の追加約1 LLM latencyと、Discord groupで沈黙を選ぶ頻度を確認する。三文目以降の意味的反復は低遅延維持のため対象外。
- Handoff: `docs/handoffs/2026-07-30_2320_codex_repeat-recovery.md`
- Related ADR / decision: ADR-0002、D-034、R-043。

## 2026-07-30 22:50 JST — Codex — 終了相槌と短い導入をまたぐ重複発話の抑止

- Status: completed
- Summary: 「ほんとそれ。」のように会話を自然に閉じる同意表現がLLMへ進み、短い笑い・相槌だけを変えて直前の返答本文を再生成する問題を修正した。
- Changed:
  - `neuro_voice/dialogue/turn_closure.py`: `ほんとそれ / マジでそう / まさにそれ / それな`等の内容を追加しない口語同意を、AI発話直後は`SILENCE`として扱う。質問・訂正・依頼・Activity・実行確認の既存優先規則は維持。
  - `neuro_voice/dialogue/echo.py`: 最初の短い「あはは、やっぱり！」だけでstreamを解放せず、最大もう一文だけ待って実質本文を直前assistant発話・ユーザー発話と比較する。重複ならUI/TTS前に全体を抑止し、新内容なら保持分をまとめて解放。
  - `tests/test_turn_closure.py` / `tests/test_reply_echo.py`: 実例の口語終了、短い導入だけ変えた本文反復、短い導入に新内容が続く非誤検知を固定。
- Behavior impact: 「ほんとそれ。」へ同じ説明を言い換えて返さず、自然に会話を閉じる。終了判定を通らない経路でも、短い導入の後ろに隠れた直前本文の再発話を再生前に止める。Local/Discordは共通Plannerと共通Echo guardを使う。
- Config / DB migration: なし。
- Tests: focused `193 passed`、最終全体 `1143 passed / 2 skipped in 56.46s`、変更Pythonの`py_compile`成功。skipは既存`pyflakes`未導入のみ。
- Remaining: 未登録の口語同意や、二文を超えてから始まる意味的言い換えは実会話fixtureで追加評価する。重複guardは低遅延維持のため最大二文だけ保留する。
- Handoff: `docs/handoffs/2026-07-30_2250_codex_colloquial-closure-echo-guard.md`
- Related ADR / decision: ADR-0002、D-033、R-043。

## 2026-07-30 22:18 JST — Codex — 非迎合会話・自律研究producer・履歴可視化・発話metadata防漏

- Status: completed
- Summary: ユーザーへの過剰な好意・同意を抑える独立姿勢を応答設計へ追加し、LLM本文へ感情タグを要求する旧契約を廃止した。自律研究は既存キュー保守だけでなく、公開persona興味と一対一会話の事実知識不足から理由付き課題を作成する。こころ画面へ調査理由・状態・サニタイズ済み検索語を表示する履歴カードを追加した。
- Changed:
  - `neuro_voice/dialogue/response_director.py` / `memory/persona.py`: 非迎合・関係性質問の独立姿勢を追加。好意・称賛・親密さを実際の関係より盛らず、UnifiedExpressionPlanを表現metadataの正本とした。
  - `neuro_voice/utils/emotion.py` / `utils/textseg.py`: 未知の`[emotion: joyful]`等も本文/TTSから除去し、日本語中へ混ざる限定的な英語下書き語をstream境界込みで自然な日本語へ正規化。
  - `neuro_voice/research/service.py` / `mind/mind.py`: 公開persona興味producer、直接会話の知識gap producer、起動遅延・cooldown・quota・privacy・重複gate、worker診断を追加。event loop外では永続queueを保持しHeartbeatで再開。
  - `neuro_voice/pipeline.py` / `discord_bridge/bot.py`: 自発発話のON/OFFとschedulerの生存を分離。発話OFFでもHeartbeatは研究queue保守とgrounded producerを継続し、発話callbackだけを見送る。
  - `neuro_voice/ui/assets/index.html`: 「こころ」に自律研究・検索履歴を追加し、既存`get_research_history` APIから100件まで更新表示。
  - `config/config.yaml`: persona興味seedの起動遅延・cooldown・再試行設定を追加。
  - `tests/`: 非迎合方向、未知tag、英語混入、persona興味seed/cooldown、履歴UIを回帰固定。
- Behavior impact: 関係性の質問でも期待されそうな強い好意へ飛躍せず、根拠の範囲で答える。自律研究ONなら起動後のHeartbeatから公開persona興味を一件ずつ低優先度調査し、答えられなかった低リスクの事実質問も候補化する。自発発話をOFFにしても裏の研究は継続する。検索実行・失敗・見送りを画面で確認できる。
- Config / DB migration: 既存Research SQLite schemaをそのまま使用。新設定は`self_seed_enabled=true`、起動45秒、同種seed 180分cooldown、失敗時10分再試行。raw transcriptはseedへ渡さない。
- Tests: focused `175 passed`、会話prompt上限修正後のfocused `5 passed`、履歴診断追加後`34 passed`、全persona興味カテゴリ追加後`12 passed`、Heartbeat所有権を含む最終focused `26 passed`、最終全体 `1140 passed / 2 skipped in 57.51s`。変更Pythonの`py_compile`成功。skipは既存`pyflakes`未導入のみ。
- Remaining: GUI再起動後60〜90秒待ち、こころ履歴へpersona興味由来の一件が`調査中/確認済み/暫定/失敗`として出ることを実ネットワークで確認する。検索結果の内容品質は外部検索品質に依存し、Knowledgeは単一sourceなら暫定のまま。
- Handoff: `docs/handoffs/2026-07-30_2218_codex_independent-stance-autonomous-research.md`
- Related ADR / decision: ADR-0004、D-032、R-042。

## 2026-07-30 02:26 JST — Codex — 会話Move候補競争・反応学習・意味連想・統一表現計画

- Status: completed
- Summary: 一対一Local会話とDiscord group会話を主対象に、返答本文を複数LLM生成せず「どう返すか」のMove候補を競争させ、次のユーザー反応を小幅学習し、短い意味連想とvoice/avatar共通表現契約を既存Conversation Plannerへ統合した。AvatarはNo-op、配信コメントは将来protocolだけで未接続。
- Changed:
  - `neuro_voice/dialogue/selection_kernel.py` / `conversation_planner.py`: 直接回答・短い反応・意見・遊び・話題展開・質問をrelevance/continuity/social fit/noveltyで比較。訂正・支援・履歴想起はhard obligation。選択後のLLM呼出しは一度。
  - `neuro_voice/dialogue/reaction_learning.py` / `intelligence.py` / `adaptive_store.py`: 明示肯定/否定、反復指摘、笑い、継続、終了相槌を構造化し、Move重みを0.70〜1.30で小幅更新。group本文は保存しない。
  - `neuro_voice/dialogue/semantic_graph.py`: persona別の短いtopic nodeと`co_occurs` / `corrected_to` edgeを追加。directとDiscord runtime session scopeを分離。
  - `neuro_voice/dialogue/expression_plan.py` / `mind.py` / `pipeline.py` / `discord_bridge/bot.py`: voiceと将来avatarを同一response IDで表し、Local/Discordへ内部`expression_plan` eventを通知。Avatar実行は無効。
  - `neuro_voice/dialogue/audience.py`: 将来の配信コメントcluster入力の型/protocolだけを追加。reader未接続。
  - `config/config.yaml`: Selection KernelとSemantic Graphの有効設定を追加。
  - `tests/test_conversation_selection_and_expression.py`: Move優先順位、学習上限、direct/group graph境界、訂正edge、avatar No-op、Dialogue統合を回帰固定。
- Behavior impact: 通常質問は直接回答、短い相槌は短い反応、訂正は修復を優先する。Discord groupでは不要な追加質問へ発言権コストが入る。同じ返し方への明示的な不満や好評が、以後のMove選択へ弱く反映される。
- Config / DB migration: `dialogue.selection_kernel.enabled=true`、`dialogue.semantic_graph.enabled=true`、`max_nodes_per_user=120`。既存generic adaptive SQLite tableへ新record typeを追加するだけで物理schema migrationなし。
- Tests: 新規 `8 passed`、focused `223 passed`、最終全体 `1131 passed / 2 skipped`、変更Pythonの`py_compile`成功。skipは既存`pyflakes`未導入のみ。
- Remaining: 実GUI再起動後、8〜15turnの一対一会話と3人以上Discordで、Move分布・質問率・group floor・TTS表現を受入確認する。Avatar/配信commentは意図どおり未接続。
- Handoff: `docs/handoffs/2026-07-30_0226_codex_conversation-selection-expression.md`
- Related ADR / decision: ADR-0003、D-031、R-041。

## 2026-07-30 01:47 JST — Codex — 会話訂正・割り込み後の同一回答ループ修正

- Status: completed
- Summary: ユーザーがAI発話を遮って意味を訂正した直後、遮られた誤回答が保留文脈として再注入され、通常履歴へ未commitのため重複検知もすり抜け、全く同じ回答を生成する経路を修正した。短すぎる音声をWhisperが動画末尾文句へ誤認して会話を汚す経路も抑止した。
- Changed:
  - `neuro_voice/dialogue/repair.py`: 明示訂正を構造化し、訂正事実・内部文脈・安全な短いfallbackを生成。
  - `neuro_voice/memory/conversation.py`: 明示訂正時に無効な割り込み保留を破棄し、遮られたassistant本文は履歴でなく重複検知証拠として有界保持。
  - `neuro_voice/dialogue/kernel.py` / `conversation_planner.py` / `acts.py`: 訂正を検索や通常継続より優先し、拒否された解釈を再開しない契約を追加。
  - `neuro_voice/pipeline.py` / `neuro_voice/discord_bridge/bot.py`: Local/Discordの冒頭重複検知を通常履歴・割り込み本文・現在のユーザー発話へ拡張。抑止時に検索していないのに検索失敗と言うfallbackと、末尾から旧回答が漏れる経路を修正。
  - `neuro_voice/stt/hallucination.py`: 動画末尾定型句を文字列だけで禁止せず、発話時間の物理的不整合または弱い音声根拠がある場合だけ棄却。
  - `tests/test_conversation_repair.py`: 訂正、検索拒否、保留破棄、割り込み重複証拠、fallback、STT幻覚の回帰テストを追加。
- Behavior impact: 「AっていうのはBという意味」の訂正は、遮られた古い解釈を再開せず、Bを正本として短く修復する。同じ誤回答はLocal/Discordとも再生前に止まる。
- Config / DB migration: なし。
- Tests: focused `41 passed`、related `229 passed`、全体 `1123 passed / 2 skipped`。
- Remaining: 暗黙的で主語の省略が大きい訂正は実会話受入が必要。一般の知能・人格品質をこの修正だけで解決したとは扱わない。
- Handoff: `docs/handoffs/2026-07-30_0147_codex_conversation-repair-echo.md`
- Related ADR / decision: D-030。

## 2026-07-30 00:57 JST — Codex — 自発発話を相互対話へ統合

- Status: completed
- Summary: 自発発話を履歴外の独り言から、通常会話へcommitされるassistant turnへ変更した。沈黙時は現在文脈から未使用の根拠を一つだけ選び、同じ話題を使い切った場合は`DO_NOTHING`とする。過去open-threadの自動確認は既定OFF。
- Changed:
  - `neuro_voice/autonomy/engine.py`: focus選択・意味的重複抑止・質問budget・initiative moveを追加し、過去話題recallの人工的な優遇を削除。
  - `neuro_voice/memory/conversation.py`: 内部指示を履歴へ残さず、直近会話を保持して生成する`messages_for_autonomous_turn()`を追加。
  - `neuro_voice/pipeline.py` / `neuro_voice/discord_bridge/bot.py`: Local/Discordの自発出力を実際のassistant履歴へcommitし、次のユーザー発話が直前の質問・意見へ接続するよう統一。
  - `config/config.yaml`: `autonomy.open_thread_callbacks_enabled: false`を既定化。
  - `tests/test_autonomy.py` / `tests/test_autonomous_conversation_bridge.py`: focus消費、反復時の沈黙、質問上限、callback無効、履歴連続性を回帰固定。
- Behavior impact: 「さっき言ってた件どうなった？」の反復を標準経路から除外。自発時は短い意見・気づき、またはbudget内の具体的な質問を一手だけ出す。発話した内容をポッポ自身が次ターンで参照できる。
- Config / DB migration: DB変更なし。過去話題callbackが必要な場合のみ`autonomy.open_thread_callbacks_enabled: true`。
- Tests: 全体 `1116 passed / 2 skipped`。自発・heartbeat・履歴橋渡し `21 passed`。`py_compile`成功。
- Remaining: 実GUIで5〜10分の沈黙を含む会話を行い、同一focusの再発なし、質問後の返答接続、Local/Discordの発話密度を受入確認する。LLM表現品質そのものは引き続き実会話評価が必要。
- Handoff: `docs/handoffs/2026-07-30_0057_codex_mutual-autonomy.md`
- Related ADR / decision: D-029、ADR-0002。

## 2026-07-30 00:11 JST — Codex — 活動開始意図・体験根拠・Conversation Move契約

- Status: completed
- Summary: 活動名への言及を開始指示から分離し、記憶・自己経験の根拠状態と、毎ターン一つの主会話行為を既存Conversation Plannerへ統合した。Local/DiscordのTTS直前と最終履歴保存前に同じresponse_id付き契約を検証する。
- Changed:
  - `neuro_voice/dialogue/intent_plan.py`: 明示的な行動依頼がある時だけBehaviorDirective候補とし、履歴・意見・情報質問を除外。
  - `neuro_voice/dialogue/conversation_contract.py`: Conversation Move選択、質問可否、会話記憶根拠、自己経験主張を決定的に検証する共通契約を追加。
  - `neuro_voice/dialogue/conversation_planner.py` / `surface_realizer.py` / `intelligence.py`: move・source・response_id・recall groundingを既存Plannerへ統合。
  - `neuro_voice/mind/mind.py`: transcript/memory取得結果をgroundingとして渡し、Local/Discord共通検証APIを公開。
  - `neuro_voice/pipeline.py` / `neuro_voice/discord_bridge/bot.py`: TTS直前とcanonical reply確定時に同じ契約を適用。TTSチャンクをまたぐ主張を一文として検証し、Local生成終端のcancel・空文・TTS失敗でも待機し続けないようにした。
  - `neuro_voice/ui/assets/index.html`: provisional streamを検証済みfinal textで確定表示。
  - `tests/test_conversation_contract.py`ほか: 活動誤開始、根拠のない経験、履歴質問、stale response_id、Local/Discord共通意味論を回帰固定。
- Behavior impact: 「前にTRPGやったの覚えてる？」等では活動を開始しない。質問へ毎回質問を付けず、選択Moveが質問の時だけ質問する。実際に取得できた記録がなければ覚えているふりをせず、読書・視聴・プレイ等の未根拠な自己経験を音声・履歴へ確定しない。
- Config / DB migration: なし。
- Tests: 全体 `1111 passed / 2 skipped`。関連 `122 passed`、会話契約単体 `11 passed`。`py_compile`成功。
- Remaining: 実GUIで通常質問、過去会話質問、TRPG言及/開始、Local/Discord音声を受入し、保守的な経験検証が自然な言い方を損なわないか確認する。
- Handoff: `docs/handoffs/2026-07-30_0011_codex_conversation-move-grounding.md`
- Related ADR / decision: D-028、ADR-0002。

## 現在地（2026-07-29 実GUI 8文の根本原因修正後。引き継ぐ人はここから読む）

**この節は最新の記録と一緒に更新すること。古いまま残すくらいなら消すこと。**

> **【2026-08-01 06:10 更新】下の「冒頭ハッシュが毎回同じ」は解消した。**
> 実機で流し直したところ **7/8 が7/29と違う**書き出しになり、
> 1番と5番も別々になった（走行内の冒頭重複率 0.0）。
> 原因の帰属はできない（7/29以降に修正が5つ以上入っている）。
> 追うべき数字は **`ask_follow_up` 違反 3/8** へ移った。詳細は 06:10 の記録。
>
> 2026-07-30 00:30 の「ゲームプロファイルを会話で切り替える」は、
> 会話品質の調査とは**独立した機能追加**。

### いま追いかけている症状

「会話のテンポと受け答えのセンスがネウロ様/Evilに届かない。返答が毎回似ている」。

### 分かっていること（すべて計測済み。推測ではない）

23:59項目のあと、ユーザーが実GUIで8文を再走した。長さ計画は
`medium=5 / short=2 / long=1`まで変化したが、冒頭ハッシュは以前と8/8同じ、
#2/#3/#8の相槌も69/61/71文字を生成した。ログとDBを突き合わせ、次を確認した。

- 1番の「話しててもAI感が強い」が裸の`話してて`に部分一致し、
  `ONGOING_DIRECTIVE / DIALOGUE / 1800秒`を誤作成していた
- 誤Directiveの`WAITING_FOR_USER`がTurn Closureを迂回し、短い相槌を毎回LLM/TTSへ流した
- 通常semantic recallが同じ入力の過去assistant回答を複数件promptへ再注入していた
- 1番は誤った補助計画LLMと本文LLMが重なり、本文初トークン12.8秒。
  以降もDirective blockが約710→1251 tokenへ増えた

上記3経路は修正済み。通常文中の「話してても」は継続候補にならず、
一般DIALOGUEは相槌終了を迂回できず、通常想起では過去assistant文面を再注入しない。
明示的な履歴質問と、TRPG/共同思考の手番待ちは維持した。
さらに2026-07-30、活動名だけの言及と開始依頼を分離し、Plannerへ一つの
`conversation_move`と記憶根拠状態を追加した。Local/DiscordともTTS直前と履歴保存前に
response_id付き契約を検証し、未計画の末尾質問、根拠のない自己経験・記憶主張を確定しない。
自発発話は履歴外の独り言ではなく通常assistant turnへcommitし、同じ根拠を再利用せず、
候補を使い切れば沈黙する。過去open-threadの自動確認は既定OFF。
全体テストは1116 passed / 2 skipped。

`docs/CONVERSATION_AB_SCRIPT.md` の8文を**4回**流した。冒頭の12文字を
ハッシュ化して比べると、**4回とも同じ位置で同じ**だった。

| 条件 | 冒頭ハッシュ列 |
|---|---|
| Planner有効 1回目 | `a4b5 2497 2f4d 1d5e a4b5 a1c8 ab06 3cf3` |
| Planner有効 2回目 | 同上（8/8一致） |
| **Planner無効** | `… ab06 8884`（**7/8一致**） |
| プロンプト −743tok 後 | 同上（8/8一致） |

- `ConversationPlanner`（738tok）を丸ごと外しても書き出しは変わらない
- 常時ONのプロンプトを 3642 → 2899tok（−743 / 20%）にしても変わらない
- 会話履歴が違っても変わらない
- `llm.temperature = 0.7`、seed 指定なし。**確率的サンプリングを通してなお変わらない**
- 書き出し自体は**定型文ではなく、入力にきちんと応じている**（23:30の記録を参照）
- **サンプリング診断は決着済み**。OpenAI互換temp 0.7（options有/無）、
  temp 1.0、Ollama native temp 0.7の全経路で、同一入力3回がすべて3/3種類に
  ばらついた。モデル既定はtemp 1 / top_k 64 / top_p 0.95
- 直近4返答の冒頭をturn-localで渡す最小A/Bを実装し、同じ8文・同じseed列で自動比較した。
  冒頭重複率は **0.25→0.25で差なし**。質問率は0.50→0.25へ下がったが、
  長さのばらつきも39.6→17.5へ縮んだため、既定ONの根拠にはせず実験用OFFとした
- `response_length=long`は永久命令でなくsoft biasへ変更。一度だけの「詳しく」は
  UserModelへ永続化せず、相槌はshort、通常turnはmedium、明示詳細だけlongにできる
- 対話制御contextは代表turnで約 **1833→1281 token**。直近返答例1件を有効化しても
  約1374 token。候補score、棄却候補、二重persona数値等を再送しない

→ samplingは正常。具体的な直近返答を足すだけでは重複率は下がらなかった。
今回、実アプリ固有だったDirective誤作成と過去回答再注入は除去したが、
会話の自然さが改善したかは再起動後の同じ8文で再受入するまで断定しない。

### 次にやること（この順）

1. アプリを再起動し、既定OFFのまま同じ8文をテキスト入力。
   1番で`Directive created`が出ないこと、#2/#3/#8が沈黙または短い直接応答になることを確認
2. `planned_length_distribution`、opening hash、初トークン時間を変更前
   （初回12.8秒、冒頭8/8一致）と比較する
3. 普通の短い雑談、相槌、明示的な詳細質問を各3件試し、実音声長を確認する
4. `recent_reply_avoidance=true`は、実会話で明確な改善仮説がある時だけ再A/Bする。
   差が再現しなければコードごと削除候補
5. Mind全体の最大context（検索・視覚・transcript想起あり）を別途計測する

### やってはいけないこと

- **会話エンジン・名前付き行動の追加。** 対照実験が否定している
- **不具合を見つけるたびにプロンプトへ一文足すこと。** 同じ規則が7箇所に
  増えた実績がある。`tests/test_prompt_rules.py` が常時ON 3000tok を上限として
  固定しているので、まず超える

### 使う道具

| 目的 | 場所 |
|---|---|
| 比較用の台本と判定表 | `docs/CONVERSATION_AB_SCRIPT.md` |
| 計画が発話へ届いたかの計測 | `neuro_voice/dialogue/plan_metrics.py` → `logs/conversation_metrics.jsonl` |
| Planner有効/無効の実測 | `logs/metrics_planner_on.jsonl` / `logs/metrics_planner_off.jsonl` |
| プロンプト規則の棚卸しと実施結果 | `docs/PROMPT_RULE_AUDIT.md` |
| サンプリングの診断 | `tools/check_sampling.py` |
| 直近返答反復回避の自動A/B | `tools/check_reply_avoidance_ab.py` |

### 今日踏んだ落とし穴（同じ形を繰り返さないために）

- **較正していない物差しで測って、相手の欠陥として報告した。** `target_length`
  の帯が実測と数文字ずれており、6/8違反という嘘の数字を出した
- **対照を取らずに結論を出しかけた。** Planner無効側を取ったのは3回目の計測の後
- **計画された値をログに残していなかった**ので、違反を事後に読めなかった
- **`last_plan` はPlannerを止めても残る。** 対照側が存在しない計画を記録していた
- **テストが一度も実行しないコードに不具合が2件。** `compileall` では捕まらない。
  pyflakes を回帰へ入れて解決（`tests/test_no_undefined_names.py`）
- **診断器自身が実アプリと同じ条件ではなかった。** `check_sampling.py`が
  `reasoning_effort=none`を送らず、120 tokenを内部思考だけで使い切った空本文を
  「全部同じ」と誤判定した。アプリの`request_extra_body()`を正本に直し、
  空本文は判定不能とするguardを追加した

---

## 2026-07-29 23:15 JST — Codex — 実GUI 8文の継続誤検知・相槌長文化・過去回答自己模倣を修正

- Status: completed（自動回帰済み。修正後の実GUI 8文再走は未実施）
- Summary: 実GUI受入のログとpersona DBを照合し、単調さを強めていた実アプリ固有の3経路を修正した。「話しててもAI感が強い」を30分の継続依頼へ誤変換していた部分一致、一般DIALOGUEの手番待ちによるTurn Closure迂回、通常semantic recallによる過去assistant回答の再注入である。
- Changed:
  - `neuro_voice/dialogue/intent_plan.py`: 裸の「話してて／何か話して」は依頼として文末にある場合だけ継続候補
  - `neuro_voice/mind/mind.py`: `directive_waiting_for_user`を`needs_user_input`かつ`NARRATION / CO_THINKING`へ限定
  - `neuro_voice/mind/mind.py`: 同一ユーザー発話のtranscriptを一件へ畳み、通常想起では過去assistant文面を除外。明示的な会話履歴質問では維持
  - tests/docs: 実例・明示依頼・手番制・想起契約の回帰、CURRENT/CODEMAP/Decision/Riskを同期
- Behavior impact: 通常の意見や説明中の「話してて」が長時間Directiveを開始しない。一般会話後の「うんうん／なるほど／そうだね」はTurn Closureで沈黙可能。過去の会話は思考材料として残るが、古いAI回答の言い回しを通常turnの表現例にしない。
- Config / DB migration: なし。既存transcriptを削除しない。
- Tests: 最終対象110 passed、関連統合238 passed、全体1086 passed / 2 skipped（pyflakes未導入）。
- Remaining: アプリ再起動後、同じ8文で`Directive created`なし、相槌3件の沈黙、TTFT、opening hashを再計測する。検索・視覚込みMind最大contextは未計測。
- Handoff: `docs/handoffs/2026-07-29_2315_codex_conversation-recall-directive-fix.md`
- Related ADR / decision: D-027、R-028、R-030、R-038、憲法第2条・第11条・第13条・第19条

## 2026-07-29 23:59 JST — Codex — 反復回避A/B、長さ固定解除、対話context圧縮

- Status: completed（自動回帰・自動A/B完了。実GUI会話の受入は未実施）
- Summary: 指示された順に、(1)直近の実返答冒頭をturn-local材料にする最小機能と本文非保存A/B、(2)`target_length`の`long`固定解除、(3)Mind内の対話制御context圧縮を実施した。直近返答機能は冒頭重複率を改善しなかったため既定OFFの実験機能に留めた。長さ制御とcontext圧縮はコード・token実測・回帰で確認した。
- Changed:
  - `neuro_voice/dialogue/surface_realizer.py`: 既存`recent_replies`から冒頭・最初の一文だけをboundedな動的材料へ変換。追加LLM、新store、本文logなし
  - `neuro_voice/dialogue/intelligence.py`: 実験機能の設定読込と件数だけのsnapshotを追加。重複persona数値、Curiosity score、Dialogue Act等の二重promptを削減
  - `neuro_voice/mind/mind.py`: Privacy境界を渡し、実験機能を有効化してもグループpromptへ直近返答本文を入れない
  - `neuro_voice/dialogue/conversation_planner.py`: 現在turn優先のsoft length policyへ変更。候補score・棄却候補をLLMへ渡さず選択済み方針だけに圧縮
  - `neuro_voice/dialogue/user_model.py`: 一回限りの「詳しく／短く」を永続好みにしない。明示的な一般嗜好だけ保存
  - `neuro_voice/dialogue/plan_metrics.py`: `planned_length_distribution`追加
  - `tools/check_reply_avoidance_ab.py`: 同じ8文・同じseed列のOFF/ON比較。返答本文は保存・表示しない
  - `config/config.yaml`: `recent_reply_avoidance`設定を追加し、実測差なしのため既定OFF
  - tests/docs: A/B手順、token上限、長さsoft bias、Local/Discord共通prompt経路の回帰と文書同期
- Behavior impact: 一度「詳しく」と言った後でも短い相槌はshort、普通の短い質問はmediumへ戻る。通常対話の代表control contextは約1833→1281 token。直近返答例1件を実験的に有効化すると約1374 token。
- Config / DB migration: DB migrationなし。新設定`dialogue.conversation_generation.recent_reply_avoidance.*`は既定OFF。
- Tests: 自動A/Bは冒頭重複率0.25→0.25、質問率0.50→0.25、長さ標準偏差39.6→17.5。`py_compile`成功。対象34 passed、統合173 passed、最終全体1074 passed / 2 skipped（pyflakes未導入）。
- Remaining: アプリ再起動後、同じ8文・通常雑談・相槌・詳細依頼で実GUI受入。検索/視覚/想起ありのMind最大contextは未計測。
- Handoff: `docs/handoffs/2026-07-29_2359_codex_reply-diversity-length-context.md`
- Related ADR / decision: D-026、R-038、憲法第2条・第12条・第20条

## 2026-07-29 23:45 JST — Codex — sampling診断を実行し、診断器の偽陽性を修正

- Status: completed（診断確定。会話本体は未変更）
- Summary: Claudeの指示どおり`tools/check_sampling.py`を実行した。初回は全経路が空本文を返したにもかかわらず「全部同じ」と判定した。原因は実アプリが送る`reasoning_effort: none`を診断器が送らず、120 tokenを内部思考だけで使い切っていたこと。実アプリ同等条件へ直して再実行すると、4経路すべて同一入力3回が3/3種類にばらついた。サンプリングは正常であり、単調さの原因をtemperature/Ollama/OpenAI互換層から除外した。
- Changed:
  - `tools/check_sampling.py`: OpenAI互換へ`reasoning_effort: none`、nativeへ`think: false`を追加。空本文を判定不能にし、Windows CP932で診断全体が落ちない出力guardを追加
  - `docs/SHARED_CHANGELOG.md`: 「現在地」と次の一手を実測結果へ更新
  - `docs/KNOWN_RISKS_AND_DEBT.md`: 診断器と実経路の不一致による誤診断をR-037へ追記
- Behavior impact: アプリ会話本体への影響なし。診断器が実アプリと同じ思考OFF条件を使い、空本文をsampling不全と誤判定しなくなった。
- Config / DB migration: なし。
- Tests: `python tools/check_sampling.py`成功。4条件すべて3/3種類。`py_compile`成功。`tests/test_no_undefined_names.py`は1 passed / 2 skipped（pyflakes未導入）。
- Remaining: 直近の具体的な書き出しをturn-localな反復回避材料として渡す最小A/Bを設計し、同じ8文で測る。`target_length`固定と`Mind.build_context`縮小はその後。
- Handoff: `docs/handoffs/2026-07-29_2345_codex_sampling-diagnosis.md`
- Related ADR / decision: R-037、R-038、憲法第2条・第20条

## 2026-08-02 21:40 JST — Claude — Phase 6G: 移行の最終配線・障害復旧・記憶の重複排除（既定オフ）

- Status: completed（自動回帰済み。**実機未検証**。Phase 6 の残件）
- Summary: Phase 6F に残った4点だけを閉じた。新機能は足していない。①UI が関係値を**直接** legacy から読んでいた（`person_primary` で画面だけ古い値）。②作業の状態が**メモリにしか無く**、落ちると次に何をすべきか分からない。③人物単位で引くと**同じ話が2回** Planner へ渡る。④実機受入れ手順。
- Changed:
  - `neuro_voice/mind/mind.py`: `Mind.status()` の `_relationships.snapshot(f"speaker:{...}")` 直読みを `_speaker_relationship_snapshot()`（Resolver 経由）へ。話者一覧に `identity`（`person_id` と解決状態）を添える。**`relationship_status()` は互換のため残した。** `request_migration` に `resume` を追加し、**中断中は `resume` と `legacy_rollback` 以外を拒否**。
  - `neuro_voice/mind/store.py`: `migration_jobs` 表と `interrupt_stale_jobs()`。**起動IDが違う `RUNNING` は前回落ちた分**として中断印を付ける（時刻だけでは長い移行と落ちた移行を区別できない）。
  - `neuro_voice/mind/migration.py`: `JobStatus.INTERRUPTED_RECOVERABLE` / `PROCESS_ID` / `MigrationJob.row()` / `job_from_row()` / `MigrationJobRunner.recover()` / `.resume()` / `.cancel_recovered()` / `.blocked`。**1件ごとに保存**して、落ちてもどこまで進んだかが分かる。再開は `progress_cursor` の続きから——**完了済みを飛ばす。**
  - `neuro_voice/cognition/dedup.py`（新規）: `normalise_text()`（NFKC・空白・ASCII小文字・末尾句読点のみ。**語順も同義語も触らない**）/ `fingerprint()` / `deduplicate()`。**LLMを呼ばない。** **レコードを消さない**——渡す直前に絞るだけ。**`USER_STATEMENT` と `INFERENCE` は指紋が違う**ので統合されない。似ているだけのものは `possible_near_duplicates` に印を付けるだけで落とさない。
  - `neuro_voice/mind/episodes.py`: ranking の直前に `deduplicate()`。`RetrievalResult.dedup` で件数と落としたIDを返す。
  - `neuro_voice/ui/assets/index.html`: 中断した移行の表示と、**その時は「再開」「従来へ戻す」だけ**を出す。
  - `neuro_voice/cognition/trace.py`: `migration_job_persisted` / `migration_job_recovered` / `migration_job_resume_from_cursor` / `migration_job_recovery_action` / `memory_result_count_before_dedup` / `memory_id_duplicates_removed` / `memory_fingerprint_duplicates_removed` / `memory_result_count_after_dedup` / `duplicate_memory_ids`（**IDだけ**）/ `speaker_status_ui_source`。
  - `neuro_voice/diagnostics/wiring.py`: プローブ2件（`memory.dedup` / `migration.recovery`）。
  - `tests/test_migration_recovery.py`（61件）
- Behavior impact: **フラグを上げるまでの挙動は変わらない。** 記憶の重複排除だけは常時効く——ただし**同じ話が2回渡らなくなるだけ**で、返す件数の上限は同じ。
- Config / DB migration: 既存 `mind.db` に1表を追加。既存表は触らない。
- Tests: 全体 **2458 passed / 17 subtests**（+61）。配線診断の**要注意 0 件**。pyflakes clean。**故障注入2件で赤くなることを確認**——出典を無視した指紋にする／起動しただけで再開する。
- **自分のテストが自分のバグを2件見つけた**: ①指紋に `participant_ids` を入れていて、**Discord と Local で別の `speaker_id` に紐づいた同じ話が必ず別物**になり、重複排除が一件も効いていなかった（今回の目的そのものが未達だった）。②`migration.recovery` プローブが毎回一時ディレクトリと SQLite ファイルを作り、**点検全体が 291ms**（上限 250ms）に。3秒ごとに回る点検が会話の邪魔をしていた。両方とも in-memory DB とテストで固定。
- Remaining: **実機未検証。** 受入れ手順は handoff §8。**対象外として残す**（仕様どおり）: 過去 Conversation History の物理移行、過去 World State の人物単位移行、Local の authoritative 参加者一覧、近似意味による自動統合。
- Handoff: `docs/handoffs/2026-08-02_2140_claude_migration-recovery-phase6g.md`
- Related ADR / decision: 憲法第12条（privacy）、第13条（時間順序）、第17条（安全な失敗）、第20条（観測可能性）

## 2026-08-02 20:05 JST — Claude — Phase 6F: 移行を人が操作できるようにし、過去の記憶を人物単位で引く（既定オフ）

- Status: completed（自動回帰済み。**実機未検証**。Phase 6 の運用面）
- Summary: 移行の仕組みは Phase 6E で作ったが、**コードからしか動かせず**、`person_primary` にしても**過去の記憶は `speaker_id` のまま**だった——Discord と Local で同じ人と話しても記憶が繋がらない。今回はその2つを閉じた。新しい Identity 機能は足していない。
- Changed:
  - `neuro_voice/mind/migration.py`: `IdentityScope`（person_id → 確認済み speaker_id 群。**候補・競合・取り消し・置き換え済みは入れない**）/ `ALLOWED_TRANSITIONS`（飛び越し禁止の表）/ `TransitionVerdict` / `MigrationDryRunResult` / `MigrationJob` + `MigrationJobRunner`（**別スレッド・1つずつ**）/ `dry_run()` / `can_transition()` / `resync()`。
  - `neuro_voice/mind/episodes.py`: `retrieve(scope=..., scope_mode=...)` と `_across()`。**過去のレコードは書き換えない**——検索側で束ね、`memory_id` で重複を落とす。**人数が増えても読み込み量も返す件数も増やさない**（1つあたりの上限を割る）。
  - `neuro_voice/mind/mind.py`: `request_migration()`（UI からの唯一の入口。**遷移の可否も昇格条件もここでやり直す**）/ `migration_dry_run()` / `migration_job_runner()` / `identity_scope_for()` / `speaker_status_rows()` / `_memory_scope_mode()`（**関係値と記憶の段階を揃える**）。
  - `neuro_voice/ui/webview_app.py`: `get_migration_status()` / `run_migration_operation()`。**UI から DB を直接触らせない。**
  - `neuro_voice/ui/assets/index.html`: 既存の🩺オーバーレイに区画を1つ追加。**新しい管理画面は作っていない。** 危険な操作は確認ダイアログ（現在/変更後/対象/競合/部分的失敗/戻し方）。実行中はボタンを無効化し、押した瞬間にも無効化して**二重送信で2つ目のジョブを作らない**。話者一覧に `Legacy Speaker` / `Resolved Person` / `Unknown` / `Conflicted` / `Revoked` を表示。**Local は「推定話者数」と明記。**
  - `neuro_voice/cognition/trace.py`: `migration_ui_operation` / `migration_transition_*` / `migration_job_*` / `migration_promotion_gate` / `identity_scope_*` / `memory_retrieval_mode` / `legacy_memory_result_count` / `person_scope_result_count` / `deduplicated_memory_count` / `revoked_ids_excluded` / `speaker_status_resolution`。
  - `neuro_voice/diagnostics/wiring.py`: プローブ2件（`migration.transitions` / `migration.memory_scope`）。
  - `config/config.yaml`: `identity_migration.{ui_enabled,dry_run_enabled}`、`memory.{identity_scope_retrieval_enabled,identity_scope_shadow_read_enabled}`。**`dry_run_enabled` だけ既定 true**（何も変えないので）。
  - `tests/test_migration_ui.py`（58件）
- Behavior impact: **フラグを上げるまで挙動は変わらない。** `ui_enabled` を上げると🩺に区画が出る。記憶の人物単位検索は `person_primary` かつ `identity_scope_retrieval_enabled` の時だけ。
- Config / DB migration: DB変更なし。ロールバックはフラグを false に戻すだけ。
- Tests: 全体 **2399 passed / 17 subtests**（+58）。配線診断の**要注意 0 件**。pyflakes clean。**故障注入3件で赤くなることを確認**——飛び越しを許す／部分的失敗を無視して昇格する／取り消したリンクを検索範囲へ含める。
- **自分のテストが自分のバグを2件見つけた**: ①`person` モードでも legacy を別に読んでいて、**検索の読み込み量が予算の2倍**になっていた（40+40）。人物単位の結果に含まれているので二重に読む必要が無い。②`migration.transitions` プローブが `IdentityResolver` を渡さずに検証していて、本番の設定で常に赤くなっていた。
- Remaining: **実機未検証。** 有効化順は ①`ui_enabled`（見るだけ）→ ②DRY RUN → ③`shadow_read` → ④`dual_write` → ⑤`identity_scope_shadow_read_enabled`（記憶の差を測る）→ ⑥`person_primary` → ⑦`identity_scope_retrieval_enabled` → ⑧ロールバック確認。**過去の会話状態と世界状態は移していない**（仕様どおり）。**`Mind.speaker_status()` の直読みは UI 表示用に残っている**——`speaker_status_rows()` を足したが、既存の呼び出し側はまだ差し替えていない。
- Handoff: `docs/handoffs/2026-08-02_2005_claude_migration-ui-phase6f.md`
- Related ADR / decision: 憲法第5条（状態所有権）、第12条（privacy）、第17条（安全な失敗）、第20条（観測可能性）

## 2026-08-02 18:20 JST — Claude — Phase 6E: 関係値の主キーを person_id へ、**戻せる形で**移す仕組み（既定オフ）

- Status: completed（自動回帰済み。**実機未検証**。Phase 6 最後の作業）
- Summary: `speaker_id` は「どの声に似ているか」でしかない。声紋は揺れるので、**主キーとして使い続けると、声が外れた日に関係が別人のものになる。** かといって一括で書き換えるのはもっと危ない——間違えた時に戻せない。今回は**移行そのものを1つの機能として**作った。5段階のモード、読み書きの集約、移行記録、ロールバック。**既存データは1件も消していない。**
- Changed:
  - `docs/audits/2026-08-02_speaker_id_usage.md`（新規）: `speaker_id` を主キー・外部キーに使っている箇所の限定監査。**移すのは `RelationshipStore` の鍵だけ。** 記憶・会話状態・世界状態・目標・TTSの話者スロットは対象外。
  - `neuro_voice/mind/migration.py`（新規）: `MigrationMode`（`DISABLED` → `SHADOW_READ` → `DUAL_WRITE` → `PERSON_PRIMARY`、いつでも `LEGACY_ROLLBACK`）/ `ParticipantIdentityRef`（**`person_id` が決まらなくても `speaker_id` を失わない**）/ `RelationshipResolver`（読み書きを1箇所へ）/ `IdentityMigration` + `IdentityMigrationRecord`。**`CONFIRMED` なリンクだけを自動移行に使う。** 値が食い違う関係値は**平均せず競合として止める**（平均した値は誰の関係でもない）。
  - `neuro_voice/mind/relationship.py`: `import_state()` を追加。`get()` が写しを返すのは呼び出し側が共有状態を壊さないための作りなので**そのまま**にし、移行とロールバック専用の書き込み口を別に用意した。**普段の会話からは呼ばない**（上限が無い）。
  - `neuro_voice/mind/mind.py`: `participant_ref()` / `relationship_resolver()` / `set_migration_mode()` / `attach_identity_runtime()`。**鍵を作る場所（`_relationship_speaker_key`）が1つしかない**ので、そこへ Resolver を挟むだけで20箇所以上の呼び出し側を書き換えずに切り替えられる。
  - `neuro_voice/mind/internal.py`: `StateDelta` の適用も Resolver 経由へ。**片方だけ書けたら `clamp_reason` に残す**（成功扱いにしない）。
  - `neuro_voice/mind/store.py`: `identity_migrations` 表。**取り消した分も残す。**
  - `neuro_voice/cognition/trace.py`: `migration_mode` / `relationship_read_source` / `relationship_write_targets` / `shadow_read_difference`（**軸の数と最大値だけ**）/ `legacy_write_result` / `person_write_result` / `fallback_reason` / `rollback_applied` ほか。
  - `neuro_voice/diagnostics/wiring.py`: プローブ5件（`migration.modes` / `migration.fallback` / `migration.no_average` / `migration.reversible` / `migration.link_only`）。
  - `config/config.yaml`: `identity_migration.{enabled,mode,shadow_read_enabled,dual_write_enabled,person_primary_enabled}`。**すべて false / `disabled`。** 個別フラグと `mode` が食い違ったら**弱い方**を採る。
  - `tests/test_relationship_migration.py`（75件）
- Behavior impact: **フラグを上げるまで挙動は変わらない。** `disabled` の間は従来どおり `speaker_id`。上げても `shadow_read` は差を測るだけで会話に影響しない。
- Config / DB migration: 既存 `mind.db` に1表を追加。person 側の関係値は `person_relationships_<persona>.json`（既存ファイルとは**別**）。ロールバックは `mode: legacy_rollback` にするだけ——**person のデータを消す必要は無い。**
- Tests: 全体 **2340 passed / 17 subtests**（+75）。配線診断の**要注意 0 件**。pyflakes clean。
- **自分のプローブが自分のバグを4件見つけた**: ①既定値で埋まっている新規の関係値を「食い違い」と誤判定し、一対一の移行が1件目から通らなかった。②争った末に片方が `CONFLICTED` になると「候補は1人」に見えて、割れた識別子でも移行していた。③`RelationshipStore.get()` が写しを返すため、移行が**値を1つも書けていなかった**（テストは通っていた——テストも同じ写しを見ていたから）。④handoff の「残っている穴」を書いている最中に、`attach_identity_runtime()` を pipeline / Discord から**呼んでいない**ことに気づいた。Phase 6 で何度も踏んだ形。その場で配線し、呼ぶ側があることをテストで固定した。
- Remaining: **実機未検証。** 上げる順は ①`enabled: true` + `mode: shadow_read` + `shadow_read_enabled`（差を見るだけ）→ ②`dual_write` → ③`person_primary`。問題があれば `legacy_rollback`。**既存 `SpeakerRegistry` は残したまま**（声紋の管理層）。**過去の記憶・会話状態・世界状態は移していない**——必要な時に Resolver で人物を引く。Local 側の推定人数を確定人数として扱わない制約は Phase 6D のまま。
- Handoff: `docs/handoffs/2026-08-02_1820_claude_relationship-migration-phase6e.md`
- Related ADR / decision: 憲法第5条（状態所有権）、第12条（privacy）、第17条（安全な失敗）、第20条（観測可能性）

## 2026-08-02 16:45 JST — Claude — Phase 6D: 誰が居るか・誰なのか・挨拶を認知経路へ（既定オフ、`greet_on_join` は意味変更のみ）

- Status: completed（自動回帰済み。**実機未検証**）
- Summary: 1つの `speaker_id` が3つの違うことを同時に意味していた——どの接続から来たか、どの声に似ているか、誰として覚えているか。**同じ番号で扱うと、声紋が一度外れただけで関係値が別人へ移り、移った先が正しいか確かめる手がかりも残らない。** 今回はそこを分け、リンクを必ず取り消せるようにし、参加人数の「確定」と「推測」を別の欄に分けた。あわせて入室の挨拶を固定文の即時TTSから認知経路へ移した。**`greet_on_join` は削除も false 化もしていない**——意味を「挨拶を検討してよい」へ変えただけ。
- Changed:
  - `neuro_voice/cognition/identity.py`（新規）: `TransportIdentity`（接続が保証、Discord ID）/ `VoiceIdentity`（声紋の推定）/ `PersonIdentity`（関係値・記憶が参照する正本）の3層。`IdentityLink`（`CANDIDATE`/`CONFIRMED`/`CONFLICTED`/`REVOKED`/`SUPERSEDED`）と `IdentityResolver`。**一度の声紋一致では確定しない**（`CONFIRM_SUPPORT=3`、確信 .72 以上のみ根拠に数える）。**接続と声紋が食い違ったら接続を優先**し、声紋側を `CONFLICTED` にするだけで人物は付け替えない。`revoke` / `relink` は**物理削除せず**、`superseded_by` で履歴を辿れる。**過去の会話や記憶は移動しない**（移すと繋ぎ直しがまた誤りだった時に二重に壊れる）。
  - `neuro_voice/cognition/presence.py`（新規）: `PresenceRegistry` / `PresenceEntry`（`PRESENT`/`PROBABLY_PRESENT`/`UNKNOWN`/`LEFT`/`STALE`）/ `PresenceSummary`。**`LEFT` を確定できるのは接続だけ**——声が聞こえないだけなら `STALE` まで。`authoritative_transport_count` と `estimated_unique_person_count` を別の欄に持ち、**身元の分からない声が1つでもあれば人数を言い切らない**（`certain` が false）。
  - `neuro_voice/cognition/initiative.py`: `greeting_opportunity` / `farewell_opportunity` と `OpportunityType.GREET_PARTICIPANT` / `ACKNOWLEDGE_PRESENCE` / `FAREWELL`。**候補を作らない条件**を builder 側に置いた（誰か分からない・短時間の再接続・大人数の同時入室・直前に挨拶済み）。場の状況（誰かが話している・危険警告中）は `suppression_reason` 側で、**判定を2箇所に分けない**。`SpeakingConditions.warning_in_progress` を追加。
  - `neuro_voice/cognition/types.py` / `rollout.py`: `ActionType.GREET` / `ACKNOWLEDGE_PRESENCE` / `FAREWELL` を `OPTIONAL`（**話してもよい、であって話すべきではない**）で追加し、`PROACTIVE_ALLOWLIST` へ。**挨拶も Speech Gate を通る。**
  - `neuro_voice/discord_bridge/bot.py`: `_greet_voice_member` は `cognitive_greeting_enabled` が立っている時だけ候補を作って戻る（**二重挨拶の禁止**）。退出で `_note_farewell_opportunity`。`discord_direct` の音声で `_note_direct_voice_identity`——**接続が正本と分かっている音声だけ**を声紋の根拠に積む。同時入室を見分けるため、人数更新の**前**に入室時刻を記録。
  - `neuro_voice/cognition/runtime.py`: `resolve_identity` / `note_voice_sample` / `note_participant_joined` / `note_participant_left` / `note_local_voice` / `social_opportunities` / `revoke_identity_link` / `flush_identity` / `hydrate_identity`。出入りの候補は `evaluate()` で**他の候補と同じ列に並ぶ**。
  - `neuro_voice/mind/store.py`: `person_identities` / `identity_links` の2表。**取り消した分も書く。**
  - `neuro_voice/cognition/observation.py`: 既存プロンプトの `player_state.health_estimate` を `low`/`medium`/`high` へ落として `player:hp` にする（**新しい検出器は作らない**）。ゲーム中の画面・高確信の時だけ、TTL 8秒、**正本にはしない**（画面は読み違える）。
  - `neuro_voice/cognition/trace.py`: `presence_*` / `greeting_*` / `transport_identity` / `voice_identity` / `resolved_person_id` / `identity_*` / `authoritative_participant_count` / `estimated_participant_count` / `vision_fact_*`。**音声も声紋ベクトルも表示名も入らない。**
  - `neuro_voice/diagnostics/wiring.py`: プローブ9件（`identity.layers` / `identity.conflict` / `identity.reversible` / `presence.counts` / `presence.restart` / `greeting.cognitive` / `greeting.suppression` / `greeting.farewell` / `world.vision_health`）。
  - `config/config.yaml`: `discord.{cognitive_greeting,farewell_opportunity}_enabled`、`identity.{resolution,voice_transport_linking}_enabled`、`identity.reversible_links_enabled: true`、`presence.{registry,local_estimation}_enabled`、`world_state.extended_vision_fact_enabled`。**`greet_on_join: true` はそのまま。**
  - `tests/test_identity_presence.py`（106件）
- Behavior impact: **フラグを上げるまで挙動は変わらない。** `cognitive_greeting_enabled` を上げると、入室の挨拶が固定文の即時再生から候補生成へ変わる——相手が話している最中・危険警告中・大人数の同時入室・短時間の再接続では**黙る**。上げない限り従来の固定文のまま。
- Config / DB migration: 既存 `mind.db` に2表を追加（既存表は触らない）。ロールバックは新フラグを false に戻すだけ（`greet_on_join` は元から true）。
- Tests: 全体 **2265 passed / 17 subtests**（+106）。**故障注入6件で赤くなることを確認**——声紋1回で確定する／取り消しを物理削除にする／静かなだけで退出扱い／危険中でも挨拶／知らない人にも挨拶／メニュー画面の体力を事実にする。配線診断の**要注意 0 件**。pyflakes clean。**自分のプローブが自分の設計ミスを1件見つけた**——`PresenceSummary.certain` が「接続の一覧がある＝人数を言い切ってよい」になっていた。一覧は繋いでいる人を保証するが、部屋に他の誰かが居ないことまでは保証しない。修正済み。
- Remaining: **実機未検証。** 有効化順は指定どおり ①`presence.registry_enabled`（+Trace確認）→ ②`discord.cognitive_greeting_enabled` → ③`identity.resolution_enabled` → ④`identity.voice_transport_linking_enabled` → ⑤（`reversible_links_enabled` は元から true）→ ⑥`presence.local_estimation_enabled` → ⑦`world_state.extended_vision_fact_enabled` → ⑧通常利用。**Local 側には接続の一覧が無い**ので、人数は最後まで推測のまま。既存 `SpeakerRegistry` と `PersonIdentity` の統合は未着手（今は別々に動く）。
- Handoff: `docs/handoffs/2026-08-02_1645_claude_identity-presence-phase6d.md`
- Related ADR / decision: 憲法第5条（状態所有権）、第12条（privacy）、第13条（時間順序）、第17条（安全な失敗）、第19条（Local/Discord parity）、第20条（観測可能性）

## 2026-08-02 14:30 JST — Claude — Phase 6C: 入力元ごとの接続差を埋めた（Discord入退室・画面種別・KTANE、既定オフ）

- Status: completed（自動回帰済み。**実機未検証**）
- Summary: Phase 6B で「実イベントへ繋いだ」と言ったが、繋いだのは3経路だけだった。Discord の入退室は世界状態へ入らず、画面は1種類しか見ておらず、KTANE は世界状態を持っていなかった。**入力元ごとに差があると、片方で動く機能がもう片方では黙って死ぬ。** 今回はその3点だけを閉じた。新しい認知機能も新しい認識モデルも足していない。**この作業中に実バグを1件見つけて直した**（下記）。
- Changed:
  - `neuro_voice/cognition/world.py`: `observe_fact(authoritative=...)` を追加。**矛盾の余裕は推測のためにある**——Discordの接続やゲームのセッション状態は推測ではなく正本（第5条）なので、確信の大小で覆されないようにした。**この印が無いと退出が反映されない**（VCから出た人がずっと居ることになる、実バグ）。推測側の挙動は変えていない。
  - `neuro_voice/cognition/observation.py`: `from_discord_presence` / `from_ktane_event` を追加、`from_vision` を複数画面へ拡張。`normalise_scene` は `vision/service.py` のプロンプトが実際に出す `gameplay|menu|loading|error|unknown` へ落とし、**知らない言い方は `unknown`**（近そうな分類へ寄せない）。`WorldObservationAdapter` に `observe_discord_presence` / `reconcile_discord` / `observe_ktane_event` と画面遷移の検出を追加。
  - `neuro_voice/discord_bridge/bot.py`: `_refresh_voice_members` を唯一の接続点にして `note_voice_presence()` を呼ぶ。**入退室イベントを1件ずつ追うのではなく、現在のメンバー一覧で毎回合わせ直す**——切断中の出入りはイベントが来ないし、再接続直後に前回の参加状態を信じてはいけない。Bot は既存の除外をそのまま使う。**参加状態の変化から直接発話しない**（注意層まで）。
  - `neuro_voice/games/session.py`: 出来事の通知を追加（`bomb_started` / `module_detected` / `strike_recorded` / `bomb_ended`）。**すべて会話由来**——KTANE に画面認識は無く、既存実装は最初から発話駆動。爆弾の結果は言われるまで `unknown`（終わった≠解除できた）。
  - `neuro_voice/cognition/runtime.py`: 実経路D（Discord）とE（KTANE）の入口、`_sync_ktane_goal`（明示的な開始でのみ `GAME_OBJECTIVE` を立て、終了で閉じる）。`vision_scene_parity_enabled` が下りている間は Phase 6B と同じくゲーム中の画面だけ。
  - `neuro_voice/cognition/goals.py`: `GoalRecord.outcome` と `succeeded`。**`FAILED` 状態は足さない**——足すと状態を見ている全箇所が「COMPLETED だけ見ればよい」から変わる。終わったか（状態）とうまくいったか（結果）を分けた。
  - `neuro_voice/cognition/attention.py`: `PARTICIPANT_CHANGE`。**これ自体は発話の理由にならない。**
  - `neuro_voice/mind/mind.py`: `attach_game_observer` と人格切り替え時の付け直し。**付け直しを忘れると、人格を変えた瞬間に配線だけ静かに切れる。**
  - `neuro_voice/pipeline.py`: 実経路E を Local 側にも接続（第19条）。
  - `neuro_voice/cognition/trace.py`: `discord_participant_id` / `participant_presence_delta` / `vision_scene_type` / `vision_scene_transition` / `ktane_*` / `speech_source_type` / `suppression_reason`。
  - `neuro_voice/diagnostics/wiring.py`: プローブ7件（`world.discord_presence` / `world.presence_speech` / `world.scene_types` / `world.scene_transition` / `world.ktane` / `world.ktane_goal` / `world.ktane_fast_path`）。`world.scene_types` は**映像プロンプトとの食い違いを赤くする**——架空の分類を足しても実機では死んだままになるため。
  - `config/config.yaml`: `world_state.{discord_presence,vision_scene_parity,ktane_observation}_enabled`。**すべて false。**
  - `tests/test_observation_parity.py`（103件）
  - `tests/test_initiative_runtime.py`: 既存の不安定なテストを1件修正。合言葉が `1234` だったため、**12桁の16進乱数IDにたまたま含まれて**5000回に1回赤くなっていた（実測）。漏れではない。合言葉に `-` を混ぜて、識別子とは絶対に衝突しないようにした。**本物の漏れと見分けが付かない赤は、やがて誰も見なくなる。**
- Behavior impact: **フラグを上げるまで挙動は変わらない。** 上げた場合、VCの人数と誰が居るかが世界状態へ入り、画面の種類と変化が入り、爆弾の状態と目標が入る。**発話は増えない**——参加状態も画面も、注意層より先へは自分では進まない。既存の `discord.greet_on_join` は触っていない（下記）。
- Config / DB migration: DB変更なし（Phase 6B の表をそのまま使う）。ロールバックは3フラグを false に戻すだけ。
- Tests: 全体 **2159 passed / 17 subtests**（+103）。**故障注入5件で赤くなることを確認**——正本の印を無視する／架空の `scene_type` を足す／Bot を人間として登録する／高速経路に `ANSWER` を足す／KTANE の出来事を配らない。配線診断の**要注意 0 件**を維持。pyflakes は新規分すべて clean。
- Remaining: **実機未検証。** 順に上げる: ①`discord_presence_enabled` → VCに出入りして🩺の人数が合うこと、**Bot が数に入っていないこと**、②`vision_scene_parity_enabled` → メニューを開いて遷移が出ること、③`ktane_observation_enabled` → 「爆弾解除を始めよう」で目標が立ち、終了で閉じること。**要確認**: 既存の `discord.greet_on_join: true` は入室のたびに固定文で挨拶する。Phase 6C 仕様の「毎回join/leaveへ挨拶する実装は禁止」と衝突するが、**ユーザー設定なので勝手に既定を変えていない**。止めるなら false にするか、挨拶自体を Speech Gate 経由へ移す改修を別途行う。
- Handoff: `docs/handoffs/2026-08-02_1430_claude_observation-parity-phase6c.md`
- Related ADR / decision: 憲法第5条（状態所有権）、第12条（privacy）、第17条（安全な失敗）、第19条（Local/Discord parity）、第20条（観測可能性）

## 2026-08-02 12:07 JST — Claude — Phase 6B: 世界状態を**実イベントへ繋ぎ**、再起動を越えさせ、権限を実際に判定させた（既定オフ）

- Status: completed（自動回帰済み。**実機未検証**）
- Summary: Phase 6 は部品を全部作ったのに、**呼ぶ側が一つも無かった**。世界状態は永久に空のままで、`NextAction.permission` は固定の `AUTOMATIC`、Goal と Obligation は別々に持っていた。今回はそこだけを埋めた。**新しい認知機能は足していない。** 話者確認・映像解析・ゲームイベントという既に動いている3つの経路から観測を取り込み、SQLiteへ保存して復元し、Obligation と Goal を双方向に同期し、行動の種類ごとに許可を決めるようにした。復元では**一時的な事実を必ず古い扱いへ落とす**——再起動しただけで昨日のHPを今の状態として語り出すのは嘘になる。
- Changed:
  - `neuro_voice/cognition/observation.py`（新規）: `EntityObservation` / `FactObservation` / `ObservationIntake`（確信下限・重複イベントID・無変化・毎秒本数で落とす）/ `WorldObservationAdapter` / `ObservationResult`。入力元ごとの変換は `from_speaker` / `from_vision` / `from_game_event` の3つだけ。**意味の判断も記憶への保存も入れていない**——入れた瞬間に映像から発話が生まれる経路ができる。`SESSION_SCOPED_PREDICATES` に一時状態の述語を宣言。
  - `neuro_voice/pipeline.py`: 実経路A（`note_speaker_observation` を新設し、話者確認のコールバックから呼ぶ）、実経路B（`_on_game_observation` から `observe_vision`）、実経路C（同メソッドから `observe_game_event`）。`initiative()` は初回に `MemoryStore` を繋いで `hydrate()` する。`flush_world_state()` をターンの区切りで呼ぶ（**映像1フレームごとにSQLiteを叩かない**）。保存に失敗したら `world_state_persist_failed` を出す。
  - `neuro_voice/mind/store.py`: `world_entities` / `world_facts`（`session_scoped` 付き）/ `goals` / `goal_obligations` の4表を既存DBへ追加。**新しいDBは作らない。** `mark_session_facts_stale()` は起動時に一時的な事実へ古い印を付ける。
  - `neuro_voice/cognition/runtime.py`: `attach_store` / `observe_speaker|vision|game_event` / `flush_world` / `flush_goals` / `hydrate` / `link_obligation` / `sync_obligation_state` / `obligation_updates_for` / `latency_ms`。復元時、Entity は `VISIBLE` ではなく `INFERRED`、ACTIVE な目標は `PAUSED` へ落とす（**確かめるまで動き出さない**）。
  - `neuro_voice/cognition/goals.py`: `ActionCategory` 7種と `CATEGORY_POLICY`、`PermissionDecision`、`decide_permission`（**未知の行動と対象不明は確認側へ倒す**）。`NextAction.decision` を追加し、固定の `AUTOMATIC` を廃止。`OBLIGATION_TO_GOAL` / `GOAL_TO_OBLIGATION` と `GoalRecord.obligation_ids`。
  - `neuro_voice/cognition/trace.py`: 観測元・元イベントID・`observe_*` が呼ばれたか・Delta ID・保存の成否（**未実施 `None` と失敗 `False` を区別**）・目標と約束のID・許可の種類と結果と理由・通過段階。
  - `neuro_voice/diagnostics/wiring.py`: プローブ6件を追加（`world.producers` / `world.observation` / `world.intake` / `world.persistence` / `goals.obligation_bridge` / `goals.permission_decision`）。監視フラグに6B分を追加。
  - `config/config.yaml`: `world_state.{observation_wiring,speaker_observation,vision_observation,game_observation,persistence}_enabled`、`goals.{persistence,obligation_bridge}_enabled`、`permissions.enforcement_enabled`。**すべて false。**
  - `tests/test_world_wiring.py`（65件）/ `tests/test_world_wiring_trace.py`（27件）
- Behavior impact: **フラグを上げるまで挙動は変わらない。** 上げた場合、話者確認で参加者と「いま誰が話しているか」が入り、映像から場面、ゲームから状況が入る。会話の内容は変わらない（世界状態は行動候補への小さな補正にしか使われない）。保存はターンの区切りだけで、応答の待ち時間には入らない。
- Config / DB migration: 既存の `mind.db` に4表を追加（既存表は触らない）。フラグを上げなければ表は空のまま。ロールバックは全フラグを false に戻すだけ。
- Tests: 全体 **2056 passed / 17 subtests**。新規92件。**故障注入で赤くなることを確認済み**——producerの呼び出しを消す / 復元時に `PAUSED` へ落とさない / `EXTERNAL_WRITE` を自動許可へ戻す / `SESSION_SCOPED_PREDICATES` を空にする、の4つでテストと診断プローブが赤くなる。配線診断の**要注意 0 件**を維持。pyflakes は新規分すべて clean。
- Remaining: **実機未検証。** 下記の順で段階的に上げる。①`world_state.enabled` + `entity_tracking` + `fact_tracking` + `observation_wiring` + `speaker_observation` → 管理者メニュー🩺で `world.observation` が緑、`world` の件数が増えることを確認。②`vision_observation` / `game_observation` を1つずつ。③`persistence_enabled` → 再起動して `world_state_hydrated` が出て、**ゲーム途中の状況が現在の事実として戻らない**ことを確認。④`goals.enabled` + `persistence` + `obligation_bridge`。⑤`permissions.enforcement_enabled` は最後（**実際に止める**フラグなので、止めてよい行動を先に確認する）。映像は `scene_type` の1種類しか取り込んでいない——`people` / `objects` は当たり方を実機で見てから。
- Handoff: `docs/handoffs/2026-08-02_1207_claude_world-state-phase6b.md`
- Related ADR / decision: 憲法第2条（LLMが意味・コードが検証）、第5条（状態所有権）、第12条（privacy）、第17条（安全な失敗）、第19条（Local/Discord parity）、第20条（観測可能性）

## 2026-08-02 13:10 JST — Claude — Phase 6: いまどうなっているか・共有する目標・未完了の約束（既定オフ）

- Status: completed（自動回帰済み。**実機未検証**）
- Preflight: 7条件すべて問題なし（機会が Action Selector へ / 沈黙と比較 / Speech Gate / TTS直前再検証 / 期限切れ / 予算と重複抑制 / Closure後の同一話題）。修正不要
- 既存機能の分類:
  - **REUSE**: `WorkingMemory`（open_questions）、`GameProfileSessionManager`、`KtaneTables`、`RelationshipStore`、Phase 3 の `EpisodicMemory`、Phase 5 の `AttentionEvent` / `InitiativeOpportunity`
  - **EXTEND**: `CognitiveKernel`（`goal_bias`）、`InitiativeRuntime`、`CognitiveTrace`、診断
  - **NEW**: `cognition/world.py`、`cognition/goals.py`
  - **DUPLICATE / DEAD**: 無し
- Changed:
  - `neuro_voice/cognition/world.py`（新規）: `GroundedWorldState` / `WorldEntity` / `WorldFact` / `WorldStateDelta` / `WorldStateGate` / `Presence` / `ItemStatus`
  - `neuro_voice/cognition/goals.py`（新規）: `GoalRecord` / `GoalAdmissionGate` / `NextAction` / `permission_for` / `goal_bias` / `mark_completed` / `resume_candidates`
  - `neuro_voice/cognition/runtime.py`: 世界状態と目標を保持。`observe_entity` / `observe_fact` / `sweep_world` / `propose_goal` / `next_actions` / `goal_bias`
  - `neuro_voice/cognition/kernel.py`: `goal_bias` を通常候補と機会の**両方**へ
  - `neuro_voice/cognition/trace.py` / `neuro_voice/pipeline.py` / `neuro_voice/diagnostics/wiring.py` / `config/config.yaml`
  - `tests/test_world_state.py`（新規、76件）
- **記憶（過去）と状況（現在）を同じレコードへ入れていない。** 性質が違うため——記憶は増えていくが世界状態は古くなって消える。記憶は確信が上がるが世界状態は放っておくと落ちる。診断プローブで「鮮度は現在状況だけ、重要度は記憶だけ」を固定してある
- 設計上の要点:
  - **鮮度は述語ごと。** HPは8秒、「いまMinecraftをやっている」は1時間。一律にするとどちらかが必ずおかしくなる
  - **見失っただけで「無い」と言わない。** `Presence` を5段階（VISIBLE / INFERRED / RECENTLY_SEEN / CONFIRMED_ABSENT / UNKNOWN）で持ち、`sweep()` は消さずに落とす
  - **確信 .75 未満では既知の対象へ寄せない。** 「たぶんあの人」で統合すると別人の情報が混ざり、後から分離できない。種類が食い違う場合も統合しない
  - **矛盾は僅差では覆さない**（`CONTRADICTION_MARGIN = .15`）。代わりに古い方を UNCERTAIN にする
  - **勝手な長期目標を作れない。** 登録できる出どころは5つだけ（ユーザーが明示／承認／明示的な共同作業／自分が引き受けた約束／ゲームプロフィール）。`self_generated_desire` などは拒否。**持ち主のいない目標も拒否**——事実上ポッポ自身の目標になるため
  - **目標は命令ではない。** `goal_bias` は ±0.15 で頭打ち。**打ち切りの合図が出ていれば何も返さない**（目標が ACTIVE でも会話を続ける理由にならない）。ただし目標は破棄しない
  - **完了は証拠と確信（.8以上）が要る。** 早まると、まだ終わっていない作業の助言が止まる
  - **中断中に勝手に再開しない。** `resume_candidates` が関連する状況の時だけ拾う
  - **知らない行動は許可が要る側へ倒す。** 要許可10件（送信・削除・購入・ゲーム操作など）／自動6件
- Behavior impact: **既定では何も変わらない。** `world_state.*` と `goals.*` はすべて false
- Config / DB migration: なし（世界状態は RAM のみ。長期目標の永続化は未実装）
- Tests: 全体 **1964 passed / 17 subtests**、pyflakes clean
- レイテンシ: `world_state_snapshot_ms` 0.057 / `world_state_delta_ms` 0.006 / `entity_resolution_ms` 0.010 / `fact_validation_ms` 0.030 / `goal_matching_ms` 0.009 / `next_action_generation_ms` 0.016。**100ターン相当で 2.8ms、追加のLLM呼び出しは無し**
- 診断: **{ok: 28, off: 14, blocked: 2}、要注意 0 件**を維持。「状況」層6プローブを追加。全掃引 9.9ms
- Remaining:
  - **実機未検証**
  - **世界状態も目標も RAM のみ。** 再起動で消える（§16「再起動後も長期GoalとObligationを維持」は未達）
  - 既存 Obligation（`WorkingMemory.open_questions`）と `GoalRecord` を**繋いでいない**。`obligation_integration_enabled` は枠だけ
  - 世界状態を**作る側の配線が無い**（`observe_entity` / `observe_fact` を呼ぶ実経路が未接続）
  - 垂直スライス（§15）は部品として通るが、実経路では動かない
- Handoff: `docs/handoffs/2026-08-02_1310_claude_world-state-phase6.md`
- Related ADR / decision: 憲法第2条・第12条・第16条・第17条・第20条

## 2026-08-02 10:40 JST — Claude — 段階1を有効化＋残っていた穴を全部塞いだ（**診断の要注意ゼロ**）

- Status: completed（自動回帰済み。**実機未検証**）
- Summary: `initiative.enabled: true`（段階1のみ）。あわせて Phase 3〜5 で残していた5つの穴を塞いだ。**管理者メニューの要注意が 0 件になった**（26 ok / 10 off / 2 blocked）
- **① フラグ**: `initiative.enabled: true`。**行動は変わらない**——注意イベントを取り込み、機会を作り、トレースへ残すところまで。発話を決めるのは従来どおり autonomy（`speech_enabled` は false のまま）
- 塞いだ穴:
  1. **`timing_score` を実際に計算するようにした。** 既定 .5 のままで「価値はあるが今じゃない」を表現できていなかった。相手が話し終えて0.8秒未満は食い気味(.15)、1〜2.5秒がいちばん自然(.85)、45秒以上空いたら唐突(.35)、自分が直前に話していたら下げる。`opportunities_from_events(timing=...)` で機会へ届く
  2. **実人数を数えるようにした。** `is_group` の2値だと3人と5人の差が出ない。`Mind.participant_count()` が聞き手の一覧から数える
  3. **`target_scope` を種類から振り分けるようにした。** 全部 `GROUP` だと独り言が呼びかけと同じ重さになる。約束・呼びかけ=DIRECT / 実況=GROUP / 環境への一言=AMBIENT。**相手が特定できていなければ DIRECT にしない**（不明話者を特定人物として扱わない）
  4. **内面が Planner の口調へ届くようになった（Phase 4 からの黄色）。** `ConversationPlan.internal_state` と `_internal_state_line()` を追加し、`Mind.build_context` の**両方の分岐**から渡す。**生の数値は渡さない**——渡すとモデルが数字を根拠に語り始める（「今の私の苛立ちは0.4なので」）
  5. **Discord parity（第19条の借り3つ）。** `Mind.close_turn()` と `self_failure_pattern()` へ後始末を集約し、Local と Discord の両方が同じ1行を呼ぶ。Discord にも `initiative()` / `note_attention_event()` / `_proactive_gate()` を置いた
- Changed:
  - `neuro_voice/cognition/initiative.py`: `timing_score()` / `scope_for()` / `_SCOPE_BY_KIND`
  - `neuro_voice/cognition/runtime.py`: timing を評価へ、機会へ配布
  - `neuro_voice/dialogue/conversation_planner.py`: `internal_state` フィールドと `_internal_state_line()`
  - `neuro_voice/dialogue/intelligence.py` / `neuro_voice/mind/mind.py`: `internal_state` を Planner まで通す。`participant_count()` / `close_turn()` / `self_failure_pattern()`
  - `neuro_voice/discord_bridge/bot.py`: 注意層・イベント投入・自発発話の抑制・共通の後始末
  - `neuro_voice/pipeline.py`: 実人数と経過時間を評価へ、共通の失敗パターンを使用
  - `neuro_voice/diagnostics/wiring.py`: 「Discord」層（2）と `initiative.timing` / `initiative.scope`。`internal.planner` は**指示文になるかまで**見る形へ
  - `config/config.yaml`、`tests/test_phase5_gaps_closed.py`（新規、32件）
- Behavior impact: **段階1だけでは会話は変わらない。** 内面→Planner だけは `internal_state.*` が false のため空 dict が渡り、これも実質無効
- Tests: 全体 **1888 passed / 17 subtests**、pyflakes clean（既存の未使用importを除く）
- 診断: **{ok: 26, off: 10, blocked: 2}、要注意 0 件**。全掃引 9.5ms
- Remaining:
  - **実機未検証。** 起動して 🩺 →「注意イベントが実際に流れているか」を見るのが次
  - Discord の `_proactive_gate` は**抑制だけ**。Local のように Action Selector で候補を比べる形にはなっていない（`speech_enabled` を上げた時に差が出る）
  - Discord は `_cognitive_decide` を通っていない（通常応答は従来経路のまま）
- Handoff: `docs/handoffs/2026-08-02_1040_claude_gaps-closed.md`
- Related ADR / decision: 憲法第12条・第17条・第19条・第20条

## 2026-08-02 08:20 JST — Claude — Phase 5 完結: 内面・記憶・複数人・段階フラグ・必須12シナリオ

- Status: completed（自動回帰済み。**実機未検証**）
- Summary: Phase 5 の残り（§16〜§23）。内面と記憶を自発発話へ**小さな補正として**繋ぎ、複数人の場のコストを入れ、段階フラグを細かく分け、必須12シナリオを固定した
- Changed:
  - `neuro_voice/cognition/initiative.py`: `TargetScope`（DIRECT/GROUP/AMBIENT）と `social_cost_in_group()` / `apply_internal_state()` / `opportunities_from_memories()` / `MEMORY_INITIATIVE`。`InitiativeOpportunity` へ `target_participant_ids` / `target_scope` / `source_memory_ids`
  - `neuro_voice/cognition/runtime.py`: 段階フラグ5つ、記憶と内面と場の広さを `evaluate()` へ、レイテンシ名を仕様どおりへ
  - `neuro_voice/cognition/kernel.py`: `propose/decide` へ `now`
  - `neuro_voice/cognition/trace.py`: `speech_gate_result` / `cancel_reason`
  - `neuro_voice/diagnostics/wiring.py`: 「記憶をそのまま喋らないか」「内面が実況量を暴走させないか」「複数人の会話を邪魔しないか」の3プローブ。フラグ帯へ段階4つ
  - `config/config.yaml`: `attention_enabled` / `game_commentary_enabled` / `memory_initiative_enabled` / `idle_initiative_enabled` / `pre_speech_revalidation_enabled`
  - `tests/test_initiative_scenarios.py`（新規、52件）
- **シナリオテストが実物のバグを1つ見つけた**: `_with_opportunities` の期限判定が `time.monotonic()` を直接読んでいて、**呼び出し側と違う時計で作られた機会が全部「期限切れ」として静かに消えていた**。実機では偶然動くが、試験も再現も不可能な形。`propose/decide` へ `now` を通した
- 設計上の要点:
  - **内面は強制命令ではなく小さな補正。** 好奇心は**発見への一言**にだけ、遊び心は**安全な成功**にだけ効く。苛立ちは**増やす方向へ一切効かせない**（コストを上げるだけ）。上限 ±0.12 で、行動側の補正（±0.20）より小さくしてある
  - **記憶をそのまま喋らせない。** 使うのは約束・明示された好み・訂正・強い感情の4種だけ。関連度の下限（.35）で脈絡の無い披露を防ぐ。**過去の失敗は候補にしない**——あれは「思い出して話す」ためではなく「同じ手を打たない」ため
  - **複数人ほど割り込みは高くつく。** ただし**名指しで聞かれていれば上乗せしない**（「複数人だから」で黙るのはおかしい）
  - **段階を細かく分けた。** 実況・記憶起点・沈黙中の再開を個別に上げられる。`pre_speech_revalidation_enabled` だけ既定 true——止めると「相手が話し始めたのに喋り出す」が復活する
- Behavior impact: **既定では何も変わらない**
- Config / DB migration: なし
- Tests: 全体 **1856 passed / 17 subtests**、pyflakes clean
- レイテンシ（12イベントを溜めた状態）: `focus_ms` 0.13 / `opportunity_generation_ms` 0.32 / `deduplication_ms` 0.03 / `initiative_scoring_ms` 0.005 / `suppression_ms` 0.05 / `pre_speech_revalidation_ms` 0.003。**1回の評価 0.47ms、追加のLLM呼び出しは無し**。診断の全掃引 4.8ms
- Remaining:
  - **実機未検証。** `initiative.enabled` を上げたことがない
  - `timing_score` を実際に計算する側が無い（既定値のまま）
  - `participant_count` は `state.is_group` からの2値近似。実人数を数えていない
  - Discord 側は未着手（第19条の借りが3つ）
  - 内面→Planner の `DISCONNECTED` は Phase 4 から未解消（**残る唯一の黄色**）
- Handoff: `docs/handoffs/2026-08-02_0820_claude_initiative-complete-phase5-4.md`
- Related ADR / decision: 憲法第2条・第16条・第17条・第20条

## 2026-08-02 05:50 JST — Claude — Phase 5 第三弾: 実経路への配線と**迂回の修正**（既定オフ）

- Status: completed（自動回帰済み。**実機未検証**）
- Summary: 第一弾・第二弾で作った部品を実経路へ繋ぎ、**Phase 5 の監査で見つけた迂回を塞いだ**。管理者メニューの「自発発話が認知層を通っているか」が `DISCONNECTED` → `OK` になった
- **迂回の修正**（この弾の本題）:
  - これまで `respond_text(internal_event=True)` が `_cognitive_decide` を丸ごと飛ばしていて、**autonomy発話とゲーム実況は沈黙の決定も記憶の補正も内面の補正も掛からずに喋っていた**
  - かといって内部プロンプトを `_cognitive_decide` へ渡すと、**自分が自分に話しかけた**ことになる（内部文から相手の感情を推定し、内部文で記憶を引く）
  - そこで**別の入口** `_proactive_decide()` を作った。渡すのは「自分へ向けられていない出来事」（`SILENCE_TIMEOUT` / `VISUAL_CHANGE`）で、**内部プロンプトは Action Selector に一切触れない**
  - 決まった決定を `respond_text(proactive_decision=...)` で持ち込み、`_execute_or_stay_silent` と ActionOutcome の経路に乗せる
- Changed:
  - `neuro_voice/cognition/runtime.py`（新規）: `InitiativeRuntime`。注意状態・間引き・注目対象・予算・警告の連打抑制を1つに束ねる。`counters` で**「配線したが一度も動いていない」を見分けられる**
  - `neuro_voice/pipeline.py`: `initiative()` / `note_attention_event()` / `_speaking_conditions()` / `_proactive_decide()` / `proactive_speech_allowed()`。`respond_text` へ `proactive_decision`
  - 実イベントの投入3箇所: ユーザー発話開始（`_on_speech_start`）/ ゲームイベント（`_on_game_event`）/ 沈黙（自発ループ）。**呼び出し側で重要度を判断しない**——間引きは `EventIntake` の仕事で、判断が散らばると場所ごとに閾値が変わる
  - 呼び出し元2箇所を新入口へ: `_run_local_autonomous_turn` と `_game_companion_loop`
  - `neuro_voice/diagnostics/wiring.py`: `attention.autonomy_bypass` を「入口・持ち込み・呼び出し元の3点」を見る形へ書き換え。`initiative.runtime`（イベントが実際に流れているか）と `initiative.speech_flag`（上げても効かない依存）を追加。`run_all(cfg, mind, pipeline)`
  - `neuro_voice/cognition/trace.py`: `initiative_summary`（**なぜ黙ったかも残す**——「機会は出ていたが全部抑制された」と「機会が1つも無かった」は違う）
  - `config/config.yaml`: `initiative.*`（既定 false）
  - `tests/test_initiative_runtime.py`（新規、24件）
- 安全側:
  - `initiative.speech_enabled` は **`cognition.enabled` が false だと効かない**。認知層が止まっているのに自発発話だけ通すと、沈黙の決定も記憶の補正も掛からない発話が復活する。フラグ帯に依存を出してある
  - `_proactive_decide` が落ちたら**黙る**。自発発話は落ちた時に喋る方が危ない
  - TTS投入直前に `runtime.confirm()` + Speech Gate の二重確認。**取り消した発話を後から再生しない**
- Behavior impact: **既定では何も変わらない。** `initiative.enabled` が false の間は `note_attention_event` が即 False を返し、`_proactive_decide` が `(None, None)` を返すので、従来どおり autonomy が発話を決める
- Config / DB migration: なし
- Tests: 全体 **1804 passed / 17 subtests**、pyflakes clean。診断の全掃引 5.2ms
- **既存テストを1件直した**: `test_the_pipeline_gate_returns_early_for_silence` が「最初の `_execute_or_stay_silent` の直後に return があるか」だけを見ていて、コメント中の言及に引っかかった。**呼び出しの数だけ確かめる**形へ変更——最初の1つだけ見ていると、後から足された経路が打ち切らないまま通る
- Remaining:
  - **実機未検証。** `initiative.enabled` を上げたことがない
  - `note_attention_event` の投入は3箇所のみ（直接質問・未完了の約束・記憶の関連は未接続）
  - Discord 側は未着手（第19条の借りが3つ）
  - 内面→Planner の `DISCONNECTED` は Phase 4 から未解消
- Handoff: `docs/handoffs/2026-08-02_0550_claude_initiative-wiring-phase5-3.md`
- Related ADR / decision: 憲法第17条・第19条・第20条

## 2026-08-02 03:40 JST — Claude — Phase 5 第二弾: 発話機会・予算・Speech Gate・ゲームイベント分類

- Status: completed（自動回帰済み。**実経路へは未接続**——第一弾と同じ理由）
- Summary: 自分から話す**機会**を作り、価値とコストで評価し、**通常会話と同じ候補の列へ並べる**層。作った時点では話さない
- Changed:
  - `neuro_voice/cognition/initiative.py`（新規）: `InitiativeOpportunity` / `OpportunityType`（9種）/ `SpeakingConditions` / `suppression_reason` / `InitiativeBudget` / `WarnThrottle` / `GameEventCategory`（8種）/ `classify_game_event` / `opportunities_from_events` / `merge_opportunities` / `revalidate`
  - `neuro_voice/cognition/types.py`: `ActionType` へ6種追加（COMMENT / REACT / REMIND / RESUME_OBLIGATION / LIGHT_FOLLOW_UP / SHARE_MEMORY、すべて `OPTIONAL`）。`ActionCandidate` と `ActionDecision` へ**出どころ**（source_type / source_event_ids / topic_id / participant_ids / expires_at）
  - `neuro_voice/cognition/kernel.py`: `propose(..., opportunities)` で同じ列へ並べる。**沈黙が無ければ必ず足す**。機会にも記憶と内面の補正を同じように掛ける
  - `neuro_voice/cognition/rollout.py`: `SpeechSource.PROACTIVE_OPPORTUNITY` と `PROACTIVE_ALLOWLIST`。決定との一致・期限・相手の発話・Closure抑制・確信度を検証
  - `neuro_voice/diagnostics/wiring.py`: 「注意」層へ3プローブ追加（沈黙が候補にあるか / 時間が理由になっていないか / Speech Gate）
  - `tests/test_initiative.py`（新規、70件）
- 設計上の要点:
  - **自発発話専用の Action Selector を作らない。** 別系統にすると通常会話とどちらを優先するか決める場所が増えて必ず食い違う
  - **沈黙を常に候補へ入れる。** 機会があることと話すべきことは別。外すと「機会＝発話」になる
  - **時間は発話の理由にならない。** 沈黙の閾値イベントは、未完了の約束がある時にだけ機会になる
  - **危険は自発発話の経路に載せない。** `PROACTIVE_ALLOWLIST` に `WARN` も `ANSWER` も入れていない。危険を予算と社会的コストで扱うのは優先順位を間違えている
  - **予算は回数だけでなく重さも数える。** 回数だけだと長い実況を連発しても引っかからない
  - **統合は最新を土台にする。** 古い方を土台にすると「敵が出た」で止まって「もう避けた」が落ちる
- **実装中に見つけた2件**:
  1. **2つの点数系が別の目盛りだった。** `initiative_score` は −1〜1 に正規化、`CognitiveKernel._score` の総点は 0.8〜1.6。**同じ列へ並べても自発発話の候補が一度も勝てなかった**。`INITIATIVE_BASELINE = .85`（沈黙の既定点のすぐ下）で目盛りを合わせた
  2. **画面が変わっただけで `ANSWER` が最有力になっていた。** 誰も何も聞いていないのに。`_UNADDRESSED_EVENTS`（VISUAL_CHANGE / TOOL_RESULT / ACTION_COMPLETED / ACTION_FAILED / SESSION_*）では通常の会話候補を出さないようにした
- Behavior impact: **無し。** 実経路へ繋いでいない。診断に3つ増えるだけ
- Config / DB migration: なし
- Tests: 全体 **1780 passed / 17 subtests**、pyflakes clean。診断の全掃引 4.9ms
- Remaining:
  - 実経路へ未接続（`AttentionEvent` を作る側も、機会を Action Selector へ渡す側も無い）
  - **自発発話の迂回（`internal_event=True`）は未修正。** 直すのは第三弾（実経路への配線）と同時
  - 機能フラグ未実装（第三弾で `initiative.enabled` を入れる）
  - `InitiativeBudget` / `WarnThrottle` を実際に持ち回る場所が無い
- Handoff: `docs/handoffs/2026-08-02_0340_claude_initiative-phase5-2.md`
- Related ADR / decision: 憲法第2条・第17条・第20条

## 2026-08-02 01:15 JST — Claude — Phase 5 第一弾: 継続的注意（注意状態・イベント正規化・Focus Manager）

- Status: completed（自動回帰済み。**実経路へは未接続**——意図的）
- Preflight: 7条件すべて問題なし。修正不要だった
  - 内面→Action Selector（−0.2）/ participant分離（A=.520 B=.500）/ 短期感情の減衰（.5→.052）/ 想起の引き金（挨拶=なし・参照=past_reference）/ Speech Gate（通常回答✗・警告✓・無根拠legacy✗）/ 沈黙後の同一話題抑制
- 既存自発発話機能の分類:
  - **REUSE**: `AutonomousActionSystem`（イベント・候補生成・`UtilityScorer`・heartbeat・`should_speak`）、`autonomy/types.py` の32イベント型、`ContinuationController`、`spontaneous_suppressed`、ゲーム実況の `_game_companion_loop` と抑制器、`note_directive_environment_event`
  - **EXTEND**: `CognitiveTrace`、診断プローブ
  - **NEW**: `cognition/attention.py`
  - **UNSAFE_BYPASS**: **`respond_text(internal_event=True)` が `_cognitive_decide` を飛ばす**（`pipeline.py:1568`）。autonomy発話（`internal_event_kind="autonomy"`）とゲーム実況（`:3074`）がここを通る。**沈黙の決定も記憶の補正も内面の補正も掛からずに発話へ行く**
  - **DEAD_OR_DISCONNECTED**: 内面→Planner（Phase 4 から継続）
  - **DUPLICATE**: 無し。`AutonomousActionSystem` と認知カーネルは責務が重なるが、前者は「いつ動くか」、後者は「何をするか」で分かれている
- Changed:
  - `neuro_voice/cognition/attention.py`（新規）: `AttentionEvent`（14種）/ `EventIntake`（間引き）/ `ContinuousAttentionState` / `FocusManager` / `FocusKind`（7種）
  - **優先順位は点数ではなく段（`TIERS`）**。危険はどれだけ面白いゲームイベントが積み上がっても押しのけられない——「たまたま点が高かった」で選ばれる形にしない
  - **間引き**: `deduplication_key` + 重要度の下限 + 確信度の下限。同じ変化が毎秒10回届いても1件。**ただし危険と直接質問は落とさない**（取りこぼす方が高くつく）
  - **ヒステリシス**: 乗り換えに差（`SWITCH_MARGIN=.05`）を要求し、乗り換えた直後（`MIN_DWELL=1.5s`）は動かさない。**危険は両方を無視して割り込む**
  - `neuro_voice/diagnostics/wiring.py`: 「注意」層の3プローブを追加。うち1つが**上の迂回を `DISCONNECTED` として管理者メニューへ出す**
  - `tests/test_attention.py`（新規、39件）
- **テストが自分の設計ミスを見つけた**: 最初は「現状維持へ加点(.12)」と「乗り換えに差を要求(.15)」の両方を入れていたが、**加点だけで全部決まっていて差の判定は一度も通っていなかった**（点数は重みの合計で割るので現実的な差は .0〜.2）。同じ目的の仕組みが2つあると片方が死んでいることに気づけないので、**差の判定1つに統一**した。値は点数の実際の広がりから決め直した
- `ContinuousAttentionState.initiatives_within` が**未来の記録まで数えていた**のも修正（時計が巻き戻ると連発の抑制が効かなくなる）
- Behavior impact: **無し。** 実経路へ繋いでいない。診断に2つ増えるだけ
- **意図的にやっていないこと**: 迂回の修正。内部プロンプトを「ユーザー発話」として `_cognitive_decide` へ渡す形になってしまうため、**発話機会の評価（次の弾）と同時でないと直せない**。いまは画面に出して見えるようにしてある
- Config / DB migration: なし
- Tests: 全体 **1710 passed / 17 subtests**、pyflakes clean
- Remaining:
  - 実経路へ未接続（`AttentionEvent` を作る側も、`FocusDecision` を使う側も無い）
  - `pending_opportunities` は候補の入れ物であって、**発話機会の評価はまだ無い**
  - `recent_initiative_history` を実際に埋める経路も未接続
- Handoff: `docs/handoffs/2026-08-02_0115_claude_attention-phase5-1.md`
- Related ADR / decision: 憲法第16条（GPU競合）・第20条・第12条

## 2026-08-01 23:30 JST — Claude — 管理者メニュー: 配線診断（GUIに🩺タブ、読み取り専用）

- Status: completed（自動回帰済み。**実機のGUI表示は未検証**）
- Summary: 「実装がある」と「効いている」が別物であることが Phase 4 の監査ではっきりしたので、**その場で一往復させて確かめる**画面を作った。設定を読むだけの点検にしていないのは、それでは `irritation` が常に 0.0 だった件を見つけられないため（実装もテストも設定も揃ったまま、値だけが届いていなかった）
- 判定の作り: 各プローブが (1) **目印の値を入口へ入れる**（合成データ）→ (2) 本番と同じ経路を通す → (3) **出口で目印が読めるか**を見る。読めなければ `BROKEN`
- 状態は6つ。**`OFF`（止めてある）と `BROKEN`（壊れている）を混ぜない** — 混ぜると「赤が多いのはフラグを上げていないからだろう」で本物の故障が埋もれる:
  - `OK` 生きている / `OFF` 止めてある / `BLOCKED` ポッポ未起動で確認不可 / `BROKEN` フラグは入っているのに値が届かない / `DISCONNECTED` **実装はあるが読む側が無い** / `ERROR` 点検自体が失敗
- Changed:
  - `neuro_voice/diagnostics/wiring.py`（新規）: 23個のプローブ（認知カーネル5・記憶5・内面7・会話2・ゲーム4）。`Probe` は「壊れると何が起きるか」（`impact`）を必ず持つ——「動いているか」だけだと赤くなった時に優先順位を決められない
  - フラグ表示は**依存関係つき**。上位が off で効かないものは `ineffective` として色を変える（フラグを上げて「効かない」と悩む時間がいちばんもったいない）
  - `neuro_voice/diagnostics/__init__.py`（新規）
  - `neuro_voice/ui/webview_app.py`: `Api.get_diagnostics()` と `_latest_trace()`。**読み取り専用**（ユーザー選択）
  - `neuro_voice/ui/assets/index.html`: 🩺ボタンと診断オーバーレイ。健康度リング・状態別の集計ピル・要注意欄・フラグ帯・層ごとの一覧・トレース最新行。**開いている間だけ3秒ごとに自動更新**（閉じたら止める）
  - `neuro_voice/cognition/reflection.py`: `derive_conversation_strategy` の `min_support` を引数デフォルトから外した。**定義時に束縛されていて `MIN_SUPPORT` を変えても効かなかった**——「定数を直したのに挙動が変わらない」を診断テストが炙り出した
  - `tests/test_diagnostics_wiring.py`（新規、28件）
- **診断で見つかった現状**: `DISCONNECTED` 1件 —「内面が Planner の表現へ渡るか」。`expression_constraints()` は形を返すが `conversation_planner.py` が読んでいない。Phase 4 の handoff に書いた穴が、画面上でも赤く出るようになった
- テストの方針: **全部緑になるテストは書かない。** わざと壊した時に赤くなることを書く（`irritation` の退行・関係性の軸の欠落・記憶が点数を動かさなくなる・挨拶が保存され始める・感情が警告へ届く・沈黙が罰になる・終了合図が再び隠される・1件で仮説を作る・根拠のない操作指示が通る、の9通り）
  - **測る側が壊れているのに測られる側の欠陥として報告した**のをこの作業中に3回やっている。だから診断を入れるなら診断自体を先に点検する
- Behavior impact: 会話経路には一切影響しない。**点検は実際の記憶・関係値・感情を書き換えない**（テストで固定）
- Config / DB migration: なし
- Tests: 全体 **1671 passed / 17 subtests**、pyflakes clean、`node --check` でJS構文確認
- レイテンシ: 初回 269ms（import分）、以降 **1.2ms**。3秒ごとのポーリングでも会話の邪魔にならない
- Remaining:
  - **実機のGUIで開いたことがない。** pywebview 上での見た目は未確認
  - プローブは23個。STT/VAD/TTS/Discord の経路は未カバー（ハードウェアが要るため合成データで一往復できない）
  - `needs_runtime` のプローブは `memory.store` の1つだけ。実DBを見る点検はまだ薄い
- Handoff: `docs/handoffs/2026-08-01_2330_claude_wiring-diagnostics.md`
- Related ADR / decision: 憲法第20条（観測可能性）・第17条（安全な失敗）・第12条

## 2026-08-01 22:10 JST — Claude — 認知カーネル Phase 4: 持続する内面（既定オフ）／**切れていた配線を2つ発見**

- Status: completed（自動回帰済み。**実機未検証**）
- Summary: 毎ターン初期化された人格に戻らないよう、感情・距離感・好みが緩やかに変化する層を入れた。ただし**今回いちばんの成果は新機能ではなく、監査で見つかった2件の死んだ配線**
- **Phase 3 の preflight で3件の欠陥が出たので、先に直した**（すべてテストで固定）:
  1. `retrieval_trigger` が `unresolved_obligation` を先に見ていたため、**義務が1つ残っているだけで「もういいよ」が見えなくなっていた**。終了の合図を先に見る順へ変更
  2. `_failure_patterns_for` が引き金の名前だけで決めていたため、上の件と重なると**過去の失敗を1件も引けなかった**。引き金は「なぜ探すか」、パターンは「何を探すか」で別物なので、状態から決めるようにした
  3. Reflection の閾値を「新しい行ができた時だけ」数えていたため、**同じ失敗の繰り返し（＝いちばんよくある形）が永久に閾値へ届かなかった**。`REINFORCE` も1回の観測として数える
- **監査で見つかった死んだ配線**（Phase 4 の本体）:
  1. `CognitiveState.irritation` は `affect_state["irritation"]` を読んでいたが、`TemporalSelf.snapshot()` は感情を `"affect"` の下に入れて返し、`AffectState` の軸名は `frustration`。**名前も階層も食い違っていたので、値は常に 0.0 だった** — 苛立ち時の抑制も冗談の罰も一度も効いていない
  2. `RelationshipState` 17軸のうち、行動選択が読めるのは `trust` と `comfort` の2つだけ。残り15軸は計算され、減衰し、保存され、**プロンプト文字列にしかなっていなかった**
- 既存機能の分類:
  - **REUSE**: `RelationshipStore`（17軸・per-speaker・減衰・イベント上限）、`TemporalSelf.AffectState`（声の表情づけ）、`PersonalityEngine.TRAITS`、`cognition/trace.py` の `StateLimit` / `STATE_LIMITS`、`CognitiveState`、Phase 3 の `stance()`
  - **EXTEND**: `CognitiveState`（感情の読み方を修正＋5軸を追加）、`CognitiveKernel._score`（`state_adjustment`）、`CognitiveTrace`、`RelationshipStore`（`apply_state_delta`）
  - **NEW**: `cognition/internal_state.py`、`mind/internal.py`
  - **DEAD_OR_DISCONNECTED**: 上の2件。`RelationshipStore.apply_events` も内省LLM経由でしか呼ばれておらず、`ActionOutcome` から関係を動かす道が無かった（`apply_state_delta` で追加）
- Changed:
  - `neuro_voice/cognition/internal_state.py`（新規）: `UnifiedInternalState` / `StateDelta` / `StateDeltaGate` / `PreferenceState` / `appraise` / `decay` / `action_bias` / `expression_constraints`
  - `neuro_voice/mind/internal.py`（新規）: `InternalStateService`。**短期感情はRAMのみ**（再起動で消えてよい）
  - `neuro_voice/cognition/state.py`: 感情の入れ子と別名を吸収。`caution` / `tension` / `playfulness` / `familiarity` / `self_confidence` を追加
  - `neuro_voice/mind/relationship.py`: `apply_state_delta`（LLMイベント以外の入口。不明話者では書かない）
  - `neuro_voice/cognition/kernel.py` / `trace.py`、`neuro_voice/mind/mind.py`、`neuro_voice/pipeline.py`、`config/config.yaml`
  - `tests/test_internal_state.py`（新規、51件）、`tests/test_episodic_memory.py`（+3件）
- 三層分離: 短期感情（RAM・半減期4〜30分）／セッションの構え（RAM）／長期の関係・好み（既存DB・JSON）。**短期の出来事で長期の値を動かせない**ことをゲートで保証（`short_term_cannot_move_long_term`）
- StateDelta の制限: 型検証 → confidence ≥ .55 → 三層の整合 → 根拠IDの有無 → 慣性（**1イベントで帯を2つ跨がない**）→ 既存 `STATE_LIMITS` の上限 → 重複（event_id × target × dimension）
- Action Selector への影響: `irritation` → CONTINUE に罰・短い了承に加点 / `caution` `confidence` → 断定を抑え確認へ加点 / `curiosity` → **質問は増やさず**話題継続へ加点 / `amusement`+`playfulness`+`comfort`+苛立ちなし → 軽口へ加点。**±0.20 で頭打ち**
- **安全側（今回いちばん大事）**: `WARN` と `REMAIN_SILENT` は `PROTECTED_ACTIONS` として**感情が一切触れない**。嫌いな相手への警告を弱めるのと、沈黙を不機嫌の表明に使うのは、どちらもやってはいけない。敵意に対しても `irritation` は上げず `caution` だけ上げる（腹を立てるのと距離を取るのは違う）。すべてテストで固定
- Behavior impact: **既定では何も変わらない**。`internal_state.*` の5つとも false
- Config / DB migration: 新規DBなし。長期の関係は既存 `relationships_*.json` のまま
- Tests: 全体 **1643 passed / 17 subtests**、pyflakes clean
- レイテンシ: `state_snapshot_ms` 0.21 / `event_appraisal_ms` <0.01 / `state_delta_ms` 0.02 / `state_apply_ms` 0.02 / `state_to_action_bias_ms` 0.02 / `state_to_planner_ms` 0.01。**1ターン合計 0.23ms。追加のLLM呼び出しは無い**
- Remaining:
  - **実機未検証。** `cognition.enabled` 自体も実機で有効化したことがない
  - Planner の `internal_state` を**実際に読む側が未実装**。`planner_view()` は形を返すが、`conversation_planner.py` へは繋いでいない
  - `PreferenceState` は RAM のみ。永続化していない（Phase 3 のエピソードから毎回導ける形にはしてある）
  - Discord 側は未着手（Local のみ）。第19条の parity は Phase 3 と合わせて2つ分の借り
  - 人格の三層分離（Core Persona / Learned Tendencies / Current Expression）は**設計として分けただけ**で、Core Persona を守るコード上の防壁は未実装
- Handoff: `docs/handoffs/2026-08-01_2210_claude_internal-state-phase4.md`
- Related ADR / decision: 憲法第2条・第5条・第12条・第17条・第20条

## 2026-08-01 19:40 JST — Claude — 認知カーネル Phase 3: エピソード記憶・想起・Reflection（既定オフ）

- Status: completed（自動回帰済み。**実機未検証**）
- Summary: 経験を残して次の行動に使う閉ループを最小限で通した。**新しい記憶基盤は作っていない** — 保存先は既存の `mind_*.db` の `memories` テーブルのままで、足りないメタデータの列を後付けした
- Preflight（新規実装の前に確認）: cognition有効時に通常発話が認知経路を通る / `REMAIN_SILENT` で発話要求0件 / `ANSWER` で1件 / `ActionOutcome` が状態へ戻る。**4件とも問題なし**。修正は不要だった
- 既存機能の分類:
  - **REUSE**: `MemoryStore`（SQLite・embedding・importance・access_count）、`LocalEmbedder`、`PrivacyManager`、`RelationshipStore`、`TemporalSelf`、`Mind._reflect` のアイドル待ち機構
  - **EXTEND**: `memories` テーブル（列を10個追加）、`MemoryStore.prune`（保護対象は物理削除せず `archived` へ）、`CognitiveKernel._score`（記憶による補正）、`CognitiveTrace`（記憶の項目）
  - **NEW**: `cognition/episodic.py`、`cognition/recall.py`、`cognition/reflection.py`、`mind/episodes.py`
- Changed:
  - `neuro_voice/cognition/episodic.py`（新規）: `EpisodicMemory` / `MemoryCandidate` / `MemoryWriteGate`。判断は `STORE / SKIP / HOLD / REINFORCE / UPDATE / CONTRADICT / SUPERSEDE` の7つ。**`SKIP`（要らない）と `HOLD`（まだ確信が無いので仮説として置く）を分けてある** — 低確信の推論を恒久記憶にすると、後から本人が否定しても同じ強さで競合する
  - 矛盾の検出に `AXES`（軸）を導入。「短い方がいい」と「詳しい方がいい」は**文字としてほとんど重ならない**ので、文字列類似度では絶対に検出できない。軸（response_length / conversation_style / humor / proactivity）と、語の**周辺16文字**から読む向きで判定する
  - 重複判定は Jaccard だけでなく包含も見る。「一問一答は嫌い」と「一問一答は嫌いなんだよね」は同じことを言っているのに、語尾5文字で Jaccard が .64 まで落ちる
  - `neuro_voice/cognition/recall.py`（新規）: 想起の**引き金**（過去への言及・未完了の約束・打ち切りの合図・固有名詞・`RETRIEVE_MEMORY`）と順位付け、そして `MemoryInfluence`。**記憶をプロンプトへ足すだけでは不十分**なので、行動候補の点数として効かせる
  - `neuro_voice/cognition/reflection.py`（新規）: `CONVERSATION_STRATEGY` **1種類のみ**。`MIN_SUPPORT=3` 件そろわないと作らない。確信は `min(.75, .25+.10*support)` で頭打ち — 積み上げだけで1.0になると明示的な否定で覆せなくなる
  - `neuro_voice/mind/episodes.py`（新規）: 判断層とSQLiteを繋ぐ薄い層。判断とIOを混ぜると「なぜ覚えなかったか」を確かめるのにDBが要るようになる
  - `neuro_voice/mind/store.py`: `memories` へ加算的マイグレーション（`ALTER TABLE ADD COLUMN` のみ、10列）。`reflections` テーブル新設。`add_episode` / `episodes` / `reinforce_episode` / `mark_episode` / `upgrade_episode` / `save_reflection` / `reflections`
  - `neuro_voice/mind/mind.py`: `recall_for_action` / `observe_episodes` / `record_self_failure` / `consolidate_memory` / `memory_snapshot`。`record_turn` から候補生成、`_reflect` のアイドル後に仮説生成
  - `neuro_voice/pipeline.py`: `_cognitive_decide` で想起 → `CognitiveKernel(memory_influence=...)`。`_cognitive_outcome` で自分の失敗を記録
  - `neuro_voice/cognition/kernel.py` / `trace.py` / `__init__.py`、`config/config.yaml`
  - `tests/test_episodic_memory.py`（新規、46件）
- 保存条件: 明示された好み / 訂正 / 約束・未完了 / 強い感情 / 自分の失敗の5種のみ。**挨拶・相づち・2文字以下・確信0.55未満のASR・重複・機微情報（restricted/secret/**high**）は保存しない**
- 検索条件: 引き金がある時だけ。上限3件。Planner へ渡すのは `{memory_id, type, summary, confidence, relevance}` の4件まで。**生の会話履歴は渡さない**
- Action Selector への影響: 過去の失敗 → 該当行動へペナルティ（`FAILURE_TO_ACTION`）/ 未完了の約束 → `CONTINUE_PREVIOUS_TOPIC` へ加点 / 明示的な好み → `ASK_CLARIFICATION` へ小さな減点。**1件あたり ±0.30 で頭打ち**。記憶が安全警告を押しのけないことをテストで固定
- Behavior impact: **既定では何も変わらない**。`memory.episodic_enabled` / `retrieval_enabled` / `reflection_enabled` の3つとも false
- Config / DB migration: 列を足すだけなので**古いDBをそのまま開ける**（テストで固定）。ロールバックは列が残るだけで無害
- Tests: 全体 **1589 passed / 17 subtests**、pyflakes clean
- レイテンシ（200エピソード + 5失敗のDBで実測）: 挨拶ターンの引き金判定 **0.03ms**（DBを開かない）/ 想起 合計 **8.8ms**（取得2.4 + 順位付け2.8）/ Planner用の組み立て **0.02ms** / 書き込み **11ms**（応答完了後）/ 仮説生成 **6.0ms**（会話が止まっている間のみ）
- Remaining:
  - **実機未検証。** `cognition.enabled` 自体も実機で有効化したことがない
  - 想起は文字ベース。既存の `LocalEmbedder` を再利用した意味検索へ差し替えられるが、今回はやっていない（`rank()` の `semantic_relevance` を差し替えるだけで入る）
  - `AXES` は4軸のみ。ここに無い話題は矛盾判定に掛からない（**分からないものを矛盾と決めつけない**方を選んだ）
  - Discord 側の配線は未着手（Local のみ）。第19条の parity は次回
  - 内省LLM（`Mind._apply_reflection`）が出す記憶候補は、まだ旧経路で `_store_memory` へ直行している。**ゲートを通っていない**
- Handoff: `docs/handoffs/2026-08-01_1940_claude_episodic-memory-phase3.md`
- Related ADR / decision: 憲法第2条・第5条・第12条・第17条・第20条

## 2026-08-01 17:05 JST — Claude — ボタンの文字を日本語で受け取る（「長押し」「押す」が通らなかった）

- Status: completed（自動回帰済み。実機未検証）
- Summary: 「長押しとか押すとかの表現が通じず、push / hold と英語表記でないと伝わらない場面が結構あった」との報告。原因は2つあり、**どちらもこちらの作りの問題**だった
  1. 聞き返しを「ボタンに書いてある文字（Abort / Detonate / Hold / Press）」と**英語で列挙**していた。聞き方が答え方を決めてしまい、利用者が「英語で言わないと通らない」と学習した
  2. 2026-08-01 14:30 に「押す」を語彙から外していた。動詞と同じ形なので「ボタンを押すね」で `label=press` と誤読するのを恐れたため。**この直し方が間違っていた**——衝突する語を捨てると、いちばん自然な言い方だけが通らないという歪んだ挙動になる
- Changed:
  - `neuro_voice/games/ktane/parse.py`: `parse_button_label()` を新設。語彙を「**動詞と紛れない言い方**」（abort / 中止 / 中断 / detonate / 起爆 / 爆破 / hold / ホールド / press / プレス / **push** / **プッシュ**）と「**動詞としても普通に出る言い方**」（**長押し** / 押しっぱなし / 押し続け / **押す** / 押せ）に分け、後者は**文字を読み上げている場面でだけ**採る
  - 場面の判定は2つ。(a) 「〜って書いてある」「文字は」「ラベルは」「表示されてる」などの合図、(b) **直前にこちらが文字を尋ねた**（`slots["asked_label"]`）。尋ねた直後なら発話まるごとが答えなので「長押し」の一言で通る
  - 「押したよ」「長押ししてみた」は**押した報告**であって文字ではない。尋ねた直後でも採らない（採ると押し終わった後に別の指示が確定する）
  - `neuro_voice/games/ktane/expert.py`: `_solve_button` を `parse_button_label` へ委譲。聞き返しを「中止 / 爆破 / 長押し / 押す のどれか。英語なら Abort / Detonate / Hold / Press」へ変更（**日本語を先に置く**）
  - `neuro_voice/games/ktane/rulebook.py`: ボタンの規則へ対応表（中止=Abort、爆破/起爆=Detonate、長押し=Hold、押す/プッシュ=Press）と「**英語で言い直させない**」を追記。ポッポ自身が読む側の手当て
  - `tests/test_ktane_button_label.py`（新規、29件）
- Behavior impact: 「白で長押しって書いてある」「（色を答えた後に）長押し」が通る。英語での言い方は**すべて維持**——増やしただけで減らしていない
- 安全側は変えていない: 尋ねてもいないのに「ボタンを押すね」で `label` を確定させない（確定すると色だけの状態で誤指示が出て即ミスになる）。テストで固定
- Config / DB migration: なし
- Tests: 全体 **1543 passed / 17 subtests**、pyflakes（新規・変更分）clean
- Remaining:
  - `expert.py:346` に無関係な既存 lint（プレースホルダの無い f-string）。今回の変更範囲外なので触っていない
  - 他モジュールにも同種の「英語でしか通らない語」が残っていないかは未点検（心理戦のボタン語は実物が英語表記なので対象外）
- Handoff: `docs/handoffs/2026-08-01_1705_claude_ktane-natural-wording.md`
- Related ADR / decision: 憲法第1条・第2条

## 2026-08-01 16:10 JST — Claude — 記号は「決まった呼び名」ではなく**形の特徴**で照合する

- Status: completed（自動回帰済み。実機未検証）
- Summary: 「決まったキーワードを毎回言うのは難しい。人によっても言い方は変わる」との指摘。**そのとおりで、呼び名の一致を求める作りが間違いだった**。「丸に線」と「円の中に横棒」は文字列としては別物だが、**形としては同じ**。そこを見るようにした
- Changed:
  - `neuro_voice/games/ktane/symbols.py`: `FEATURES`（丸・線・三本・星・稲妻・しっぽ・逆・曲・四角・三角・十字・はてな・数字・アルファベット・プサイ・オメガ・水滴 など28群）と `features()`。照合は**特徴の重なりを先に見て**、文字列一致は保険。**言われた特徴が全部含まれていれば、説明が短くても当たり**とする（「ぎざぎざ」だけで稲妻に届く）
  - `SymbolVocabulary`（新規）: **言われ方を覚えていく**。形の言葉が1つも入っていない呼び名（「ポッポ印」）は寄せようがないので聞き返し、答えが来たら紐づける。次からはその言い方で通る。ただし**特徴が同じものは覚えない**（「稲妻」と「いなずま」——既に照合できるものを溜めても表が膨らむだけ）
  - `neuro_voice/games/ktane/expert.py`: `vocabulary` と `pending_symbol_alias`。聞き返した直後に答えが来たら学習する
- **知らない言葉は捨てる**。勝手に近い形へ寄せない。「なんかすごい形」は特徴が0なので照合しない——**曖昧なものを確定させると隣の記号を押させて即ミス**になる
- 例（すべてテストで固定）:

  | 言い方 | 届く記号 |
  |---|---|
  | 丸に線 / 円の中に横棒 / まるにぼう / 丸くて線が入ってるやつ | 丸に線 |
  | ぎざぎざの線 / 雷みたいなの | 稲妻 |
  | Cが裏返ってる / 反対向きのシー | 逆さのC |
  | しずくみたいな形 / 涙のかたち | 水滴 |

- Tests: 全体 **1510 passed / 17 subtests**、pyflakes 3 passed
- Remaining:
  - 新しい言い方が届かない時は `FEATURES` に**行を1つ足すだけ**で対応できる
  - 覚えた言い方は**セッション限り**。保存は未実装
  - 表そのもの（どの記号がどの列か）は依然として利用者が教える
- Handoff: `docs/handoffs/2026-08-01_1705_claude_ktane-natural-wording.md`
- Related ADR / decision: 憲法第2条

## 2026-08-01 15:20 JST — Claude — **爆発した原因**: 答えを持っていないのにポッポが操作を指示していた

- Status: completed（自動回帰済み。実機未検証）
- Summary: 実機ログで爆発。決定的だったのは **「一番上の『プサイ』のボタンを押して」**——**キーパッドの表は空で、コードは何ひとつ答えていない**。ポッポが自分で作った指示だった。他にも「一番下の白をカットして」「『Push』でも色が青なら押しっぱなし」など、**規則ではなくLLMの創作が操作指示として出ていた**
- **見落としていた穴**: 2026-08-01 02:00 に入れた検算は「**コードの答えがある時**」しか働かない。答えが無いターンは `UNCLEAR` で素通りする。つまり**解けなかった時こそ何でも言える**状態だった。取り返しがつかない側に穴が空いていた
- Changed:
  - `neuro_voice/games/ktane/verify.py`: `contains_instruction()` — 「切って/押して/離して/カットして/周波数を3.xxx/〜で送信/の順に押」を検出。**仮定と質問は拾わない**（「切ってもいいのかな？」まで止めると会話にならない）。`ungrounded_refusal()`
  - `neuro_voice/games/ktane/expert.py`: `pending_check` が無いのに操作を指示していたら**差し替える**。`_missing_hint()` で「何が足りないか」を添える
  - `neuro_voice/games/session.py`: プロンプトへ「**規則が載っていないモジュールについて、切る線・押すボタン・離すタイミングを言わない。推測で操作を指示すると爆発する**」
  - `tests/test_ktane_verify.py`: +13件（指示の検出／仮定は止めない／答えが無い指示を差し替える／足りないものを言う／根拠のある指示は通す）
- Tests: 全体 **1492 passed / 17 subtests**、pyflakes 3 passed
- **設計として残る問題**: 表が空のモジュール（キーパッド・心理戦・迷路）では、ポッポは**手伝えない**。断る以外にできることが無い。会話としては物足りないが、間違った指示より確実にましである
- Remaining:
  - キーパッドの表を口で教える経路は入れた（14:30）。**まず6列教えるところから**
  - 心理戦・迷路は表待ちのまま
  - 「あ、ごめん！勘違いで変なこと言っちゃった」の繰り返しがログに残っている。混乱ループは未着手
- Related ADR / decision: 憲法第2条。**コードが答えを持たない領域で、LLMに事実を作らせない**

## 2026-08-01 14:30 JST — Claude — 爆弾解除: 同じ質問の繰り返しと、キーパッドの記号認識

- Status: completed（自動回帰済み。実機未検証）
- Summary: 実機ログで (1) 「ボタンに書いてある文字（Abort /…」が繰り返し返る (2) 「キーパッドの表をまだ取り込んでいない」が**6回連続**、の2つが出ていた
- **(1) 同じ質問の繰り返し — 原因はモジュール入力を溜めていなかったこと**。爆弾の情報（シリアル・電池）は `Edgework` へ溜めるのに、**色や文字は毎回その発話だけから読み直していた**。だから「色は白」と言った次のターンでまた色を聞く。`KtaneExpert.slots` を足し、モジュールごとに聞けたことを溜めるようにした（モジュールが変わったら捨てる——赤い配線の話がボタンの色として残らないように）
  - ボタン: 色と文字が別ターンで来ても組み立てる
  - 配線: 「赤と青」「あと赤と黒」と分けて言われても組み立て、**残り本数だけ**を聞く
- **(2) キーパッド — 足りなかったのは表だけではなかった**。**記号を言葉で受け取る仕組み**が無かった。キーパッドの記号には公式の読み方が無く、プレイヤーがその場で呼び名を作るので、こちらが正解の名前を持てない
  - `neuro_voice/games/ktane/symbols.py`（新規）: 呼び名の照合。飾り語（「みたいなやつ」）を落とし、**中の語は保つ**（削ると別の記号と衝突する）。近い候補が2つあれば**曖昧として聞き返す**——1位を勝手に採ると隣の記号を押させて即ミス。「分からない」「紛らわしい」「確定」の3つに分けるのは、返す言葉が変わるため
  - **表を口で教えられるようにした**: 「キーパッドの1列目は 丸に線、稲妻、…」で1列ぶん覚える。JSONを手で書かなくてよい。`tools/save_keypad_table.py` でログから拾って保存
  - 表が空でも**突き放さない**。断るのではなく手順を言う（6回同じ拒否を返していた）
  - **語順が入れ替わった言い方（「6にしっぽ」）は狙わない**。拾おうと閾値を緩めると別の記号を掴む。テストにその判断を明記した
- Changed: `ktane/symbols.py`（新規）/ `ktane/expert.py`（slots・教え込み）/ `ktane/parse.py`（`parse_symbols` の上限を引数へ——表は1列7個あり、4件固定だと7個目が黙って消えていた）/ `ktane/modules.py` / `tools/save_keypad_table.py`（新規）/ `tests/test_ktane_keypad.py`（新規20件）
- Tests: 全体 **1473 passed / 17 subtests**、pyflakes 3 passed
- Remaining:
  - 心理戦と迷路は従来どおり表待ち（口で教えるには量が多すぎる）
  - 教えた表は**保存するまでセッション限り**。再起動で消える
- Related ADR / decision: 憲法第2条（曖昧なものを確定させない）

## 2026-08-01 13:20 JST — Claude — `adaptive` が名ばかりだった。その場の空気で無言／短い了承を選ぶ

- Status: completed（自動回帰済み。実機未検証）
- Summary: 「どちらも正解だから、その場で判断させられないか」との指摘。**そのとおりで、`adaptive` は名ばかりだった**——両者を拮抗させたうえで加点が0なので、実質は点数のノイズによるコイントスだった。拮抗させることと判断させることは別
- **何が違えばどちらへ寄るのか**を手がかりごとに固定した（正なら「わかった」、負なら無言）:

  | 手がかり | 寄る先 | なぜ |
  |---|---|---|
  | 話している途中で止められた | わかった | 黙ると固まったように聞こえる。聞こえて止めたことが伝わらない |
  | 相手が苛立っている | わかった | 無言は拗ねたようにも取れる |
  | まだ距離がある相手 | わかった | 沈黙が冷たさに読める。親しいほど無言でよい |
  | 聞いている人がいる（group） | わかった | 配信・通話で無音が続くのは事故に見える |
  | 「もういいよ、それで」 | わかった | 打ち切りではなく**同意**。無視しない |
  | こちらが何も話していない場面 | 無言 | 受ける相手がいない |

- Changed:
  - `neuro_voice/cognition/rollout.py`: `adaptive_closure_bias(state) -> (傾き, 理由)`。`closure_bias()` は adaptive の時だけ状態を読む。**明示的な方針（silent_preferred / brief_ack_preferred）を選んだ時はそちらが優先**——聞き比べたい時に勝手に揺れては困る
  - `neuro_voice/cognition/state.py`: `closure_is_affirmative` / `is_group`
  - `neuro_voice/cognition/kernel.py`: 傾きの**理由を候補へ残す**。「なぜ黙ったか」が読めないと調整のしようがない
  - `neuro_voice/mind/mind.py`: `_AFFIRMATIVE_CLOSURE`（「それでいい」型）と `world_state["group"]`
  - `tests/test_cognition_rollout.py`: +10件
- **上限は ±0.15 のまま**。全部の手がかりが同じ方向を向いても、安全警告や相手の明示的な質問を押しのけない
- Tests: 全体 **1453 passed / 17 subtests**、pyflakes 3 passed
- Remaining: 各手がかりの重み（0.08〜0.12）は**実機で聞いてから調整する前提の初期値**。数字の根拠はまだ無い
- Related ADR / decision: 憲法第20条。「振る舞いをコードへ埋めない」——これは断定形の規則ではなく傾きで、明示的な方針で上書きできる

## 2026-08-01 12:40 JST — Claude — Phase 2.6: 段階的な有効化・発話元の識別・高速経路のAllowlist

- Status: completed（自動回帰済み。**既定値は変えていない**——`enabled: false` / `rollout_mode: disabled`）
- Summary: 認知層を実機で試せる状態にした。ただし**いきなり全環境で常時ONにしない**。あわせて、認知層を通らない発話経路（ゲーム警告・自発発話・相槌）に出どころを付け、明示的に許可されたものだけ通す
- **有効化のしかた**: `cognition.enabled: true` + `rollout_mode: test_session`。**止め方は `enabled` の1つ**——どのモードでも false なら効かない。`rollout_mode` は disabled / test_session / profile_allowlist / enabled
- Changed:
  - `neuro_voice/cognition/rollout.py`（新規）: `RolloutMode` / `SpeechSource`(6) / `BypassReason`(5) / `SpeechRequest` / `check_speech()` / `FAST_PATH_ALLOWLIST` / `ClosurePolicy` / `spontaneous_suppressed()` / `BRIEF_ACK_CONSTRAINTS`
  - `neuro_voice/cognition/types.py`: `ActionType.BRIEF_ACKNOWLEDGE`（共感とは別物。「わかった」で終える）
  - `neuro_voice/cognition/kernel.py`: 終了シグナル時は**無言と短い了承を両方候補に出す**。`closure_bias` は**小さな加点（0.15）**で、両者をほぼ拮抗させたうえで方針が決める。片方を構造的に強くすると設定を切り替えても結果が動かない
  - `neuro_voice/cognition/bridge.py` / `plan_metrics.py`: `brief_ack`（1文以内・質問なし・30字以内で計測）
  - `neuro_voice/pipeline.py`: `cognition_active()` / `start_cognition_test_session()` / `log_cognition_status()` / `spontaneous_speech_suppressed()`。`_speech_allowed_now()` が `SpeechRequest` を作って `check_speech()` を通す
  - `config/config.yaml`: `rollout_mode` / `profiles` / `closure_response_policy` / `silence_quiet_seconds`
  - `tests/test_cognition_rollout.py`（新規31件）
- **高速経路で許可したもの**: `GAME_WARNING_FAST_PATH` × `WARN` × 確信度0.6以上のみ。**ANSWER / ASK_CLARIFICATION / CONTINUE / JOKE / CHALLENGE / ACKNOWLEDGE は拒否**。相槌は行動を伴わない短い反応のみ。`LEGACY_PATH` は**理由の無いものを通さない**
- **沈黙直後の蒸し返し防止**: 時間だけで決めず**話題も見る**。同じ話題なら時間が経っていても止め、別件なら待ち時間（既定20秒）だけで通す。時間だけだと、黙ると決めた直後に別件で話しかけるのまで止まる
- Tests: 全体 **1443 passed / 17 subtests**、pyflakes 3 passed
- **既定値は本番有効へ変えていない**。テストで固定してある（`test_the_default_config_is_not_switched_on`）
- Remaining:
  - **実機での主観評価はユーザーが行う。** 無言と短い了承のどちらが自然かは、こちらでは判断しない
  - 自発発話の欲求モデルとの統合は後のPhase（今回は識別と抑制のみ）
  - Discord 側の `SpeechRequest` 化は未着手（`_speech_allowed_now` は入っている）
- Related ADR / decision: 憲法第5条・第12条・第17条・第19条

## 2026-08-01 11:30 JST — Claude — Phase 2.5: **決定を実行契約にする**。沈黙が本当に沈黙になった

- Status: completed（自動回帰済み。実機未検証。`cognition.enabled: false` なら挙動不変）
- Summary: `REMAIN_SILENT` が最高得点で選ばれても実際には喋っていた。決定が**参考情報**にすぎず、後続経路が default allow で走っていたため。**default deny** へ変え、発話が明示的に許可された時だけ Planner / Realizer / 応答LLM / TTS を呼ぶようにした
- **発話を復活させていた経路**（監査で特定）: (a) `respond_text` が決定と無関係に生成へ進む、(b) TTSキューへの投入が5箇所あり決定を見ていない、(c) 空応答時の再生成 `repeated_reply_fallback`、(d) Discord の `_speak` が独自に喋る
- **拘束の仕組み**:
  - `SpeechPolicy`（REQUIRED / OPTIONAL / FORBIDDEN）を `ActionType` ごとに定義。**知らない行動は FORBIDDEN へ倒す**
  - `ActionExecutionGate.plan()` が speech / silent / internal の3経路を決める
  - `respond_text` は生成前に `_execute_or_stay_silent()` で**早期 return**。空文字も作らず、生成もしない
  - `_speech_allowed_now()` を**最終出口1箇所**（`_speak_loop` / Discord `_speak`）に置き、fallbackや古い非同期タスクが復活させないようにした
  - 矛盾した決定（REMAIN_SILENT + 発話する補助行動）は**禁止側へ倒し、トレースへ警告を残す**
- **Turn Closure の分離**: 沈黙は「現在ターンを閉じる」だけ。**セッションも話題も閉じない**。打ち切りの合図（end_signal > .5）が出た時だけ話題を休止し、中断発話を破棄する。義務は `obligations_to_release()` が**関連するものだけ**選ぶ——一括削除しない
- Changed:
  - `neuro_voice/cognition/types.py`: `SpeechPolicy` / `speech_policy()` / `ExecutionStatus`(6値) / `ActionDecision.turn_id`・`contradictory` / `ActionOutcome` に呼び出し記録9項目
  - `neuro_voice/cognition/executor.py`（新規）: `ActionExecutionGate` / `ExecutionPlan` / `obligations_to_release`
  - `neuro_voice/pipeline.py`: `_execute_or_stay_silent()` / `_speech_allowed_now()` / `_cancel_response()`
  - `neuro_voice/discord_bridge/bot.py`: `_speech_allowed_now()`（第19条）
  - `tests/test_silent_execution.py`（新規28件）
- **`SILENT_COMPLETED` を失敗として扱わない**。`ActionOutcome.completed` が3種の完了を等しく扱う
- Tests: 全体 **1411 passed / 17 subtests**、pyflakes 3 passed
- **沈黙ターンの実測**: `decision_to_closure` **0.0014ms** / `cancel_pending_output` **0.0020ms** / `total_silent_turn` **0.0034ms**。通常ANSWERはここにLLM（実測1〜3秒）とTTSが乗る
- Remaining:
  - `cognition.enabled: true` での実機確認。**沈黙が「無視された」と感じられないか**は数字では分からない
  - 自発発話・ゲーム警告の高速経路は認知層を通らないまま（意図どおり。ただしWARN以外が混ざらないかは未検証）
- Related ADR / decision: 憲法第2条・第5条・第17条・第19条

## 2026-08-01 10:20 JST — Claude — 認知カーネル Phase 2: 行動差の検証・トレース・因果接続（既定オフのまま）

- Status: completed（自動回帰済み。実機未検証。`cognition.enabled: false` なら挙動不変）
- Summary: 機能追加ではなく**証明**が目的。「内部状態が実際に行動選択を変え、行動結果が次の内部状態へ戻っている」ことをテストで固定した
- **監査で見つけた未使用の型**: `InformationType` が定義・エクスポートだけで一度も使われていなかった。`CognitiveState.summary()` で `distress_source=inference` として使用——**推定を本人の申告として保存しない**（原則B）ことを型で示す
- Changed:
  - `neuro_voice/cognition/trace.py`（新規）: `CognitiveTrace` / `TraceWriter`（1行1JSON、既定オフ）と `StateLimit` / `limited_change`。**記録と制限を同じファイルに置いた**——別々にすると片方だけ直して食い違う
  - `neuro_voice/cognition/state.py`: `frustration` / `engagement` / `end_signal` / `current_topic` / `ambiguity` を追加
  - `neuro_voice/cognition/kernel.py`: `_continuity_fit()` を追加し3系統を**点数成分として**接続。`_supporting()` で補助行動を**1つだけ**。`apply_outcome` が `limited_change` 経由で上限付きの差分を返す
  - `neuro_voice/cognition/types.py`: `ActionDecision.supporting_action`
  - `neuro_voice/mind/mind.py`: `_user_affect()`（distress / frustration / engagement / end_signal）と曖昧さの判定
  - `neuro_voice/pipeline.py`: 5区間のレイテンシ計測とトレース書き出し
  - `config/config.yaml`: `cognition.trace.*`（既定オフ）
  - `tests/test_cognitive_scenarios.py`（新規22件）
- **実際に行動差を生んだ状態**（点数成分として確認）:

  | 状態 | 効く先 | 成分 |
  |---|---|---|
  | `user_distress` / `frustration` | ACKNOWLEDGE_EMOTION↑・MAKE_LIGHT_JOKE↓ | `affect_fit` |
  | `comfort` / `trust` / `irritation` | JOKE・CHALLENGE_ASSUMPTION | `relationship_fit` |
  | `end_signal` / `unresolved_obligations` / `interrupted_response` | CONTINUE・RESUME↓、REMAIN_SILENT↑ | `conversation_coherence` |

- **状態変化の上限**（`STATE_LIMITS` の1箇所）: trust は **上げ +0.01 / 下げ −0.02** と非対称——積み上げた関係が一度の出来事で飛ばないように。irritation は baseline 0.05 へ毎ターン 0.02 ずつ戻る。**安全に関わる判断はこれらの値に左右させない**
- **レイテンシ（実測、LLM時間を除く）**: 候補生成 **0.025ms** / 選択 **0.007ms** / 結果反映 **0.012ms**。合計 **0.05ms未満**。DBアクセスも記憶の全件取得も増やしていない（`cognitive_state` は既存 snapshot の読み取りのみ）
- Tests: 全体 **1383 passed / 17 subtests**
- Remaining:
  - `cognition.enabled: true` での実機確認
  - `REMAIN_SILENT` が選ばれても**まだ発話は止まらない**（`turn_closure` と絡むため別作業）。**Phase 2の残る最大の穴**
  - Discord 未配線
- Related ADR / decision: 憲法第2条・第5条・第12条・第17条

## 2026-08-01 08:50 JST — Claude — 接続時に見つけた2件の反映先を修正

- Status: completed（自動回帰済み。実機未検証）
- Summary: 08:20の接続作業で見つかった「範囲外の改善点」2件を修正した
- **(1) bridge の shape が計測できなかった**: `clarify_reference` / `acknowledge_then_answer` / `urgent_warning` / `resume` は `plan_metrics._SHAPE_CHECKS` に無く、**反映されたかどうかを事後に読めない**（unverifiable落ち）状態だった。判定規則を追加——clarify_reference=質問が1つ以上 / acknowledge_then_answer=定型相槌で始めない / urgent_warning=**2文以内かつ90字以内**（最初OR条件で書いて6文でも通る緩さだった。自分のテストで捕まえて修正）/ resume=質問で締めない。**`playful_twist` は意図して unverifiable のまま**——遊び心は意味であって事実ではなく、規則で判定できるふりをしない（第2条）
- **(2) social_mode が裸のラベルだった**: `supportive` と書いてもモデルには意図が届かない。`kernel.prompt()` に `_SOCIAL_MODE_HINTS` を追加し、**bridgeが設定する4値（supportive / urgent / playful / candid）だけ**短い日本語の指示へ変換。既存の `one_to_one` / `group` / `close` は場のラベルであって指示ではないので触らない。**未知の値には指示を捏造しない**
- Changed:
  - `neuro_voice/dialogue/plan_metrics.py`: `_SHAPE_CHECKS` +4
  - `neuro_voice/dialogue/kernel.py`: `_SOCIAL_MODE_HINTS` と `prompt()` の1行
  - `tests/test_cognitive_bridge.py`: +4件（bridged shape が全て計測可能または既知 / supportive が指示になる / 未知の social_mode に指示を捏造しない / urgent_warning の長短判定）
- Behavior impact: `cognition.enabled: false` のままなら不変。true のとき、共感・警告などの意図がプロンプトの指示文として届き、反映率が `plan_metrics` で測れる
- Tests: 全体 **1361 passed / 17 subtests**
- Related ADR / decision: 憲法第2条・第20条

## 2026-08-01 08:20 JST — Claude(Fable役) — ActionType を TurnFrame へ接続（指示書どおり）

- Status: completed（自動回帰済み。実機未検証。`cognition.enabled: false` のままなら挙動不変）
- Summary: 指示書 `docs/handoffs/2026-08-01_0800_fable_instruction_action-to-frame.md` に従い、認知カーネルの `ActionDecision` を `TurnFrame` の `response_goal` / `response_shape` / `social_mode` へ反映した。書き換え口は `guarded_transition` のみ、`transition_reason` 付き
- Changed:
  - `neuro_voice/cognition/bridge.py`（新規）: `ACTION_TO_FRAME`（8行動）と `frame_changes()`
  - `neuro_voice/mind/mind.py`: `apply_cognitive_decision()`
  - `neuro_voice/pipeline.py`: 決定直後に1呼び出し
  - `tests/test_cognitive_bridge.py`（新規14件）
- Tests: 全体 **1357 passed / 17 subtests**、pyflakes 3 passed
- 指示書からの逸脱: なし
- Remaining: `cognition.enabled: true` での実機確認。`REMAIN_SILENT` の発話停止は範囲外のまま
- Handoff: 指示書に同じ

## 2026-08-01 07:30 JST — Claude — 認知カーネルMVP。**文章を作る前に「何をするか」を決める層**（既定オフ）

- Status: completed（自動回帰済み。実機未検証。**`cognition.enabled: false` で既存動作のまま**）
- Summary: これまでの経路は、入力を受けたらすぐ Conversation Planner が「どう話すか」を決めていた。**話す以外の選択肢——黙る・聞き返す・警告する・中断した話を再開する・覚えるだけ——が構造として無かった**。そのため感情や関係性の値は、結局のところ口調にしか影響していなかった。閉ループを最小限で通した
- **再利用したもの（新設していない）**: `dialogue/events.py` の `ConversationEvent`（アダプタで包む）、`dialogue/selection_kernel.py`（会話手の点数付けは既存）、`dialogue/kernel.py` の `TurnFrame`（義務・参照解決・検索可否）、`ConversationPlanner` / `SurfaceRealizer`、`WorkingMemory`、`Relationships`、`TemporalSelf`
- Changed:
  - `neuro_voice/cognition/types.py`（新規）: `CognitiveEvent` / `EventType` / `InformationType` / `ActionType`(12種) / `ActionCandidate` / `ActionDecision` / `ActionOutcome`。**ログに本文を入れない**（第12条）
  - `neuro_voice/cognition/state.py`（新規）: `CognitiveState`。**読み取り専用の統合ビューで、値は持たない**。持ち主は各モジュールのまま（第5条）。キー名をプロパティで1箇所へ寄せ、選択側がモジュール固有の名前を知らないようにした
  - `neuro_voice/cognition/kernel.py`（新規）: `propose → decide → apply_outcome`。**重みは `WEIGHTS` の1箇所だけ**。危険・聞き取り失敗・割り込みは**規則で候補を作る**（LLMに渡さない）。候補は2〜5個
  - `neuro_voice/mind/mind.py`: `cognitive_state()` / `record_cognitive_action()`。既存の snapshot を並べるだけ
  - `neuro_voice/pipeline.py`: `respond_text` の本経路へ `_cognitive_decide()` と `_cognitive_outcome()` を配線。**反射（VAD・割り込み・TTS停止）は通していない**——待たせたら割り込みが効かなくなる。認知層で落ちても旧経路で続行する（第17条）
  - `config/config.yaml`: `cognition.enabled`（既定 **false**）/ `cognition.weights`
  - `tests/test_cognitive_kernel.py`（新規26件）
- **内部状態が「点数」として効いていること**（口調ではなく）:
  - `user_distress > .35` → `ACKNOWLEDGE_EMOTION` を候補に追加し、`ANSWER` 単独に負の補正
  - `input_confidence < .55` → `ANSWER` を**条件として塞ぐ**（点数の綾で通らないように）
  - `trust` が低い → `CHALLENGE_ASSUMPTION` にペナルティ
  - `irritation` が高い → 冗談にペナルティ
  - 直近に質問していたら `ASK_CLARIFICATION` にペナルティ（連投を口調でなく点数で抑える）
  - `interrupted_response` が空なら `RESUME_INTERRUPTED_RESPONSE` に高いコスト
- **関係性は一度の出来事で動かさない**: `apply_outcome` が返す差分は上限 0.02。書き込みの持ち主は `Relationships` のまま
- **自己保存欲求は実装していない**。中断発話の保持と義務の退避のみ（システム継続性として）
- Tests: 全体 **1343 passed / 17 subtests**。7シナリオ（通常質問／感情相談／ASR低信頼／割り込み／ゲーム危険／関係性の緩やかな変化／LLM交換可能性）を網羅。LLM非依存は**importとASTで**検証（説明文の文字列ではなく）
- Remaining:
  - **`cognition.enabled: true` での実機確認**。現状は決定をログとイベントに出すだけで、`ActionDecision` を Conversation Planner の入力へ**反映**してはいない（次の一歩）
  - `ActionType` → `response_goal` / `response_shape` / `social_mode` の対応付け
  - Discord 側は未配線（Local のみ。parity は次段）
- Related ADR / decision: 憲法第2条・第5条・第12条・第17条

## 2026-08-01 06:10 JST — Claude — 実機で 7/8 の書き出しが割れた。**「1と5が同じ」は解消**

- Status: completed（実機で受入。**原因の帰属は未確定**）
- Summary: 台本8文を実機で流し直した（turn 1920〜1927）。**7/29の冒頭ハッシュと 7/8 が違う**

  | # | 7/29 | 今回 | |
  |---|---|---|---|
  | 1 | `a4b5` | `80da` | 変わった |
  | 2 | `2497` | `9d17` | 変わった |
  | 3 | `2f4d` | `be59` | 変わった |
  | 4 | `1d5e` | `9cbf` | 変わった |
  | 5 | `a4b5` | `5eb1` | 変わった |
  | 6 | `a1c8` | `d666` | 変わった |
  | 7 | `ab06` | `8c14` | 変わった |
  | 8 | `3cf3` | `3cf3` | 同じ |

- **1番と5番が別のハッシュになった**（`80da` / `5eb1`）。ユーザーの最初の訴え「1と5がほぼ一緒」はここで消えている
- 他の指標: 走行内の冒頭重複率 **0.0**、長さ 平均88.6 / ばらつき **30.3**、質問率 0.375、shape分布は5種類に分散
- **原因の帰属はできない。** 7/29以降に入ったものが多すぎる——Codexの修正3件（継続誤検知・相槌長文化・過去回答再注入）、プロンプト −743tok、`num_ctx` 8192→16384、連体詞の部分一致修正、Codexの `conversation_move` 周り。**1番だけは連体詞の修正が直接効いたと言える**（「聞き返せ」の命令が消えた位置）。残りは混ざっている。切り分け直すことは可能だが、良くなっている状態を崩してまでやる価値は薄い
- **まだ残っている数字**: `ask_follow_up` 違反 **3/8**。計画が「質問しない」と決めたターンでポッポが質問している。書き出しの多様性とは別の問題として、測れる形で残っている
- Changed: なし（計測のみ）
- Remaining:
  - `ask_follow_up` 3/8 の追跡。計画の値はログに残っているので事後に読める
  - 8番だけ一致した件。入力が「そうだね」と最小なので、偶然か本当に安定なのかは分からない。単独では追わない
- Related ADR / decision: 憲法第20条

## 2026-08-01 05:40 JST — Claude — 連体詞の部分一致で、ただの感想が「聞き返せ」になっていた（1件確定・修正）

- Status: completed（自動回帰済み。実機未検証）
- Summary: 4条件の切り分けが出そろった。**どれも実機の100%を説明しない**

  | 条件 | 詰まり |
  |---|---:|
  | まっさらから毎回 | 0% |
  | Kernelを足す | **12%（1/8）** |
  | 想起を1件混ぜる | 31〜44% |
  | 会話履歴を引き継ぐ | 0% |

- **ただしKernelアームで詰まった1件が本物のバグだった。** 詰まったのは台本1番だけで、そこは `shape=clarify_reference` / `goal="Ask one concrete clarification question; do not search or guess."` が渡る位置
- **原因**: `_REFERENCE` が「今の」「前の」「さっきの」を**裸で**並べていた。台本1番「…**今の**AIって話しててもAI感が強いんだよね」——ただの感想——の「今のAI」が会話参照として当たり、Kernelが「聞き返せ」を**このターンの権威ある決定**として渡していた。**ポッポがあそこで必ず質問を返すのはそのため**で、同じ入力なら毎回同じ命令になるので書き出しも固まる
- **2026-07-29にCodexが直した「話してても」の部分一致と同じ形**。連体詞は後ろの名詞まで見ないと指示語かどうか決まらない
- Changed:
  - `neuro_voice/dialogue/kernel.py`: 「今の/前の/さっきの」は**話の単位を表す語が続くか、そこで句が切れる時だけ**参照とみなす（`話|こと|やつ|件|内容|続き|返事|答え|回答|質問|説明`）。「それ/あれ/これ/その話…」はそのまま
  - `tests/test_conversation_kernel.py`: +10件（連体詞＋普通名詞は参照にしない／本物の参照は落とさない／台本1番が `conversational` になる）
  - `tools/check_opening_diversity.py`: `kernel_arm` を追加
- Behavior impact: 「今のAIって〜」「前のパソコンより速い」のような**普通の感想で聞き返さなくなる**。「さっきの話の続き」等の本物の参照は従来どおり
- Tests: 全体 **1317 passed / 17 subtests**
- **残っている謎**: 実機の8/8のうち、説明がついたのは1件だけ。**アプリの外からの再現は限界**——私の再構成と実アプリの差分がもう列挙できない。次は外から作り直すのをやめ、**実機で台本を流して現在の冒頭ハッシュを取り直す**こと。7/29以降にCodexの修正3件＋この修正＋num_ctx倍増が入っているので、**そもそもまだ8/8なのかを確かめるところから**
- Related ADR / decision: 憲法第2条・第5条・第20条

## 2026-08-01 05:00 JST — Claude — **原因は Conversation Kernel の `response_goal` / `response_shape`**

- Status: 機序は直接実行で確認済み。**プロンプト全体での確認（Kernelアーム）はこれから**。コードは変更していない
- Summary: 想起44% / 会話履歴引き継ぎ**0%** — どちらも実機の8/8を説明しなかった。実アプリだけが持つ最大のブロック `[CONVERSATION KERNEL - authoritative decision for this turn]`（**約4300tok。persona 1861 より大きい**）が、この経路には一度も入っていなかった。`dialogue.prompt_context` ではなく `mind._conversation_kernel_context` が作るため
- **直接実行して確かめた**（`ConversationKernel.build` を叩いただけ）:

  | 入力 | response_shape | response_goal |
  |---|---|---|
  | うんうん | `brief` | Respond naturally to this turn. |
  | 〜必要ある？ | `answer_first` | Answer the question directly before adding one useful thought. |
  | AI感が強いんだよね | `clarify_reference` | **Ask one concrete clarification question; do not search or guess.** |

  `kernel.py:536-565` を見れば分かるとおり、**goal と shape はユーザーの発話だけで決まる完全に決定的な値**。同じ入力なら毎回同じ一文が「権威ある決定」として渡る。Plannerの `primary_style=playful` のような抽象語と違い、**具体的な英語の命令文**なので効きが桁違い。**Plannerを外しても、プロンプトを743tok削っても、温度を上げても変わらなかったことと完全に辻褄が合う**
- **副次的な発見**: 台本1番「今のAIって話しててもAI感が強いんだよね」は**ただの感想**なのに `clarify_reference`（＝聞き返せ）になる。ポッポがあそこで質問を返すのは、そう命令されているから。`RESOLVE_REFERENCE` の判定が広すぎる疑いがある
- Changed:
  - `tools/check_opening_diversity.py`: `kernel_arm` を追加。`ConversationKernel` を直接使って実アプリと同じブロックを足し、詰まる割合を測る。3条件（想起 / 履歴引き継ぎ / **Kernel**）を並べて表示
- Behavior impact: なし（計測のみ）
- Remaining:
  1. `python tools/check_opening_diversity.py` で Kernelアームを実行。**60%以上なら確定**
  2. 確定したら**直し方を提案してから**着手する。Kernelは第5条の状態所有者で、obligations・検索可否・参照解決という実在の判断を持っている。**消すのではなく、`response_goal` の固定文が表現まで縛らないようにする**方向。設計に関わるので勝手に変えない
  3. `clarify_reference` の誤判定は別件として切り分ける
- **今日の反省（まとめ）**: persona → Planner → プロンプト量 → 温度 → 想起 → 履歴、と6つ疑って全部外した。**最大のブロックを一度も見ていなかった**のが理由。`プロンプト内訳` のログに `CONVERSATION KERNEL - author=4685` と毎ターン出ていたのに、読んでいなかった。**手持ちのログを先に読むこと**
- Related ADR / decision: 憲法第2条・第5条・第20条

## 2026-08-01 04:20 JST — Claude — 計測条件が実機と違っていた。**同じセッションで繰り返した**のが実機

- Status: completed（計測条件の修正まで。実行はこれから）
- Summary: 想起を1件混ぜるアームは **38%**（6/16）。上がってはいるが実機の 8/8＝100% を説明しない。**計測条件そのものが実機と違っていた**
- **見落とし**: 実機の受入は「同じ8文を、**同じセッションの中で**続けて4回流す」だった。2回目以降のプロンプトには、**直前の会話履歴に一字一句同じ質問とその答えが残っている**。私のツールは毎回まっさらから始めていた。想起として1件混ぜるのと、履歴に実物が残っているのとでは強さが違う（インコンテキストの逐語的な先例は、そのまま写される）
- Changed:
  - `tools/check_opening_diversity.py`: `run_once` が会話履歴を返し、次の走行へ**引き継げる**ようにした。`same_session_arm` を追加——実機とまったく同じ条件で台本を繰り返し、1周目と同じ書き出しになった割合を周ごとに出す
  - 3条件を並べて表示する: まっさらから毎回（0%）/ 想起を1件（38%）/ **会話履歴を引き継ぐ（← 実機と同じ）**
- Behavior impact: なし（計測のみ）
- Remaining:
  1. `python tools/check_opening_diversity.py` を再実行する
  2. **履歴引き継ぎが70%以上なら原因確定**。直し方は「同じ入力を繰り返した時に、直前の自分の答えを見せない」——温度でもプロンプトの量でもPlannerでもない
  3. それでも説明できなければ、`Mind.build_context` の残りのブロックを1つずつ足す
- **今日の反省（3回目）**: 較正していない物差しで測って結論を出しかけた。今回は「まっさらから始める」という条件差に気づかないまま、persona・Planner・プロンプト量・温度を順に疑って全部外した。**測る前に、実機で何が起きたのかを正確に再現できているか確かめること**
- Related ADR / decision: 憲法第20条。R-037へ再発として追記したい

## 2026-08-01 03:50 JST — Claude — 温度は原因ではなかった。**症状はこの経路では再現しない**

- Status: completed（計測完了・切り分けの次段を用意。**設定は何も変えていない**）
- Summary: `tools/check_opening_diversity.py` の実測結果。**温度0.7でも 2.75/3種類、詰まった位置は 0/8**。温度1.0でも 2.88/3 でほぼ同じ

  | 温度 | 位置ごとの種類 | 平均 | 詰まった位置 |
  |---|---|---:|---|
  | 0.7 | 3 3 3 3 2 2 3 3 | 2.75 | 0/8 |
  | 1.0 | 3 3 3 3 2 3 3 3 | 2.88 | 0/8 |

- **これは温度の否定であると同時に、これまでの見立て全部の否定でもある。** この経路（persona + 対話制御 + 履歴）は**ちゃんと散っている**。ところが実アプリは4回とも 8/8 同じだった。つまり原因は persona でも Planner でもプロンプトの量でも温度でもなく、**実アプリだけが足しているもの＝`Mind.build_context`**（記憶の想起・会話ログの参照・関係性・時刻）にある
- **自分のツールの判定文が間違っていた**: 「温度を上げてもばらつきが増えなかった → プロンプトの量が原因」と出力していたが、**既に2.75種類散っている時にこの結論は導けない**。較正していない物差しで測って相手の欠陥として報告した、今日3度目の同じ失敗。判定を「この経路では再現しない」へ直した
- **次の一手（最有力の仮説）**: 意味検索が**同じ入力に対する過去の自分の返答**を引いてきてプロンプトへ入れると、モデルはそれを写す。写せば当然、毎回同じになる。Codexが2026-07-29 23:15に同種の経路を1つ塞いでいるが、`ranked[:3]` の記憶側は残っている
- Changed:
  - `tools/check_opening_diversity.py`: 判定文を修正。**[想起あり]のアームを追加**——1回目の返答を2回目以降のプロンプトへ混ぜ、書き出しをなぞる割合を測る。50%以上なぞれば機序が確定する
- Behavior impact: なし（計測のみ）
- Config / DB migration: なし。**`llm.temperature` は 0.7 のまま**（上げる根拠が出なかった）
- Remaining:
  1. `python tools/check_opening_diversity.py` を再実行し、**[想起あり]の割合**を見る
  2. 高ければ `Mind.build_context` の想起内容を疑う（何を渡しているか。過去の自分の返答が入っていないか）
  3. 低ければ、`build_context` の他のブロックを1つずつ足して切り分ける
- Related ADR / decision: 憲法第20条。R-037（較正していない物差し）へ再発として追記したい

## 2026-08-01 03:20 JST — Claude — 「同じ入力に同じ返答」へ、温度という唯一未検証のつまみで当たる

- Status: completed（配線・計測ツール・テストまで。**温度の値はまだ変えていない**。測ってから決める）
- Summary: これまでに潰した仮説——Planner(738tok)を外す／常時ONを3642→2899tok／会話履歴を変える／直近返答の書き出しを渡す——**どれも冒頭ハッシュを動かさなかった**。一方 `check_sampling.py` では裸のプロンプト3回が3種類に割れる。**サンプリングは壊れておらず、巨大なプロンプトが分布を尖らせている**
- **見落としていたもの**: `llm.temperature = 0.7`。温度は分布の尖り方を直接決める量で、**0.7は「さらに尖らせる」向き**。Gemma 4 自身の推奨は 1.0（`/api/show` でも temp 1 / top_k 64 / top_p 0.95）。根拠なく置かれたまま今日まで一度も検証していなかった。**「ばらつき」の話をしていたのに、ばらつきのつまみだけ触っていなかった**
- Changed:
  - `tools/check_opening_diversity.py`（新規）: 台本8文を温度ごとに複数回流し、**位置ごとに何種類の書き出しが出たか**を数える。実測の症状（毎回1種類）をそのまま測る指標。**seedは指定しない**（実運用と同じ確率的サンプリング）。書き出しだけ見るので `num_predict=48`。本文は保存も表示もしない（第12条）
  - `neuro_voice/llm/openai_compat.py` / `factory.py`: `llm.top_p` / `llm.top_k` / `llm.min_p` を追加。**未設定なら送らない**——中途半端な値を勝手に送るとモデル自身が想定している組み合わせを崩すし、温度だけ動かして様子を見る時に他が固定されていないと切り分けができない
  - `config/config.yaml`: `llm.temperature` に経緯と判断材料をコメントで残した。`top_p`/`top_k`/`min_p` はコメントアウトのまま置いた
  - `tests/test_llm_request_body.py`: +4件（未設定なら送らない／設定した分だけ送る／Ollama以外へ送らない）
- Behavior impact: **現時点では変化なし**（つまみを増やしただけで値は据え置き）
- Config / DB migration: なし
- Tests: 全体 **1307 passed / 17 subtests**
- Remaining（この順）:
  1. `python tools/check_opening_diversity.py` を実行する（8文 × 3回 × 温度2種類。数分）
  2. **高い温度で種類が増えたら** `llm.temperature` をその値へ。ただし**ばらつきと的確さは引き換え**なので、実機で受け答えの質が落ちていないか必ず確かめる。数字だけで決めない
  3. **種類が1.0に張り付いたままなら**、温度では動かない。`Mind.build_context` の4700〜5800tok を削る方へ回す。プロンプトの量が分布を決めている、という読みになる
- Related ADR / decision: 憲法第20条

## 2026-08-01 02:40 JST — Claude — num_ctx を 8192 → 16384（VRAMを実測してから上げた）

- Status: completed（設定変更。**Ollamaの再起動が必要**。実機での効果確認はこれから）
- Summary: 実ログで**毎ターン最大24件の履歴が文脈長のため捨てられていた**。`文脈長のため古い会話を24件除外 (見積8651tok / 予算6912tok / ctx=8192)`。systemブロックだけで5000〜6000tokあるため、8192では会話に1000tok強しか残らない。**少し前の話を覚えていない直接の原因**
- **上げる前にVRAMを実測した**（`tools/check_context_length.py` を新規作成）:

  | num_ctx | 合計 | VRAM | 判定 |
  |---:|---:|---:|---|
  | 8192 | 7,599MB | 7,599MB | 全部GPU |
  | 16384 | 7,615MB | 7,615MB | 全部GPU（**+16MBのみ**） |

  倍にしてもほとんど増えないのは、`OLLAMA_KV_CACHE_TYPE=q8_0` と `OLLAMA_FLASH_ATTENTION=1` が効いているため。**この2行を消すと前提が崩れる**（載りきらなくなるとOllamaは層をCPUへ追い出し、応答が数倍遅くなる）
- Changed:
  - `config/config.yaml`: `llm.num_ctx: 16384`。実測値と前提条件をコメントで残した
  - `SetupOllamaEnv.bat`: `OLLAMA_CONTEXT_LENGTH 16384`。**config だけ変えても効かない**（サーバ側の上限で頭打ち）
  - `config/config.yaml` `trim_slack_ratio`: 値は 0.0 のまま。ただし**前提が変わった**ことを追記——予算が約15000tokになり、実測7000〜8600tokのプロンプトは**今度は収まる**。収まっている限り切り詰め自体が起きないので、上げても下げても効果は出ない。履歴が再び予算を超え始めてから考えること
  - `tools/check_context_length.py`（新規）、`tools/check_sampling.py`: `options.num_ctx` を16384へ（「アプリと同一経路」の看板を保つため）
- Behavior impact: **会話履歴の切り詰めが止まる**。少し前の話を覚えていられる。KTANEの規則ブロックも履歴を押し出さずに載る
- Config / DB migration: `SetupOllamaEnv.bat` を実行し直し、**Ollamaを再起動**（タスクトレイ → Quit → 起動）してから本体を起動する
- Tests: 全体 **1286 passed / 17 subtests**（コード変更なし。設定とツールのみ）
- **期待値は正直に**: これは記憶と継続性に効く。**「同じ入力に同じ返答」には効かない**——プロンプトを743tok削っても冒頭ハッシュが動かなかったのと同じ理由。別の問題として両方やる価値がある
- Remaining: 再起動後、`文脈長のため古い会話を…除外` がログから消えるか確認する
- Related ADR / decision: 憲法第16条（GPU競合）・第20条

## 2026-08-01 02:00 JST — Claude — ポッポ自身にマニュアルを読ませ、コードが検算する形へ（第2条の分担へ戻す）

- Status: completed（自動回帰済み。実機未検証）
- Summary: これまでは判断をすべてコードが決め、確定した一文を `ActivityOutcome` でそのまま返していた。正しいが**ポッポのLLMを一度も通っておらず**、声だけポッポで言葉は表引きの結果だった。規則をプロンプトへ渡してポッポが自分で考え、その答えを**発話の前に**コードの判定と突き合わせ、食い違った時だけ差し替える形にした。憲法第2条「LLMが意味を考える / コードが事実・整合性を検証する」の分担へ戻る
- **なぜ検算が要るか**: 実測で二値の指示ですら25%破られている。爆弾では外すと爆発する。ただし**判定できない時は否定しない**——読めなかっただけで正しいかもしれないのに差し替えるのは、それ自体が誤り。判定は `AGREE / DISAGREE / UNCLEAR` の3値
- **間違った指示は耳に届かない**: セッション中は全文を保持してから発話する経路（`activity_guarded`）に乗せてあるので、差し替えは発話前に済む
- Changed:
  - `neuro_voice/games/ktane/rulebook.py`（新規）: 8モジュールの規則を日本語の短い表として持つ。**扱っているモジュールの分だけ**渡す（1つ200〜350tok。全部載せると `num_ctx` を食い潰す）
  - `neuro_voice/games/ktane/verify.py`（新規）: 発話から結論を取り出して突き合わせる。「2本目か3本目」のように**2つ以上出てきたら判定不能**にする。差し替え文は「んー……ちょっと待って」と自分で気づいた形にし、黙って別のことを言わない
  - `neuro_voice/games/ktane/expert.py`: `pending_check` / `verify()` / `rule_prompt()` / `agreement_counts`（一致率を印象でなく数で見る）
  - `neuro_voice/games/session.py`: 答えが出ても**固定文を返さない**。`pending_check` へ置いて会話側へ渡す。`grounded_context` へ規則と「自分で読んで、当てはまる行を選ぶ」を追加。`check_pending()` / `verify_reply()`
  - `neuro_voice/mind/mind.py`: `game_profile_check_pending()` / `game_profile_verify_reply()`
  - `neuro_voice/pipeline.py`: `_verified_defusal_reply()` をTTS直前へ。**本文はログへ残さない**（第12条）。食い違ったという事実だけ
  - `neuro_voice/discord_bridge/bot.py`: 検算が要るターンは Discord でも全文を保持してから話す（既定では文単位で流すため。第19条）
  - `tests/test_ktane_verify.py`（新規37件）、`tests/test_ktane_conversation.py`（旧設計を固定していた4件を新しい意図へ）
- Behavior impact: 爆弾解除の返答がポッポの言葉になる。正しさは変わらない（食い違えば差し替わる）。検算が要るターンは全文を待つぶん、発話開始が1〜2秒遅れる
- Config / DB migration: なし
- Tests: 全体 **1286 passed / 17 subtests**
- Remaining:
  - 実機で一致率を見る。`agreement_counts` と `ktane_verified` イベントで観測できる
  - **一致率が低ければ規則テキストの書き方を疑う**。モデルを責める前に、渡し方を見ること
  - 表が要る3モジュール（キーパッド・心理戦・迷路）は `docs/KTANE_TABLES.md` のまま未取り込み
- Handoff: `docs/handoffs/2026-07-31_2340_claude_ktane-rules.md`（追記）
- Related ADR / decision: 憲法第2条・第12条・第19条

## 2026-08-01 01:00 JST — Claude — 「ボタンの色と、書いてある文字を教えて」しか返らなくなる不具合

- Status: completed（自動回帰済み。実機未検証）
- Summary: 実機で、何を言っても同じ一文しか返らなくなった。**`current_module` は言い直すまで残る**ので、一度「ボタン」と言われると以後の発話がすべてボタンの判定へ入る。そこが**手がかりゼロの発話にも必ず質問を返していた**ため、「シリアルはA1B2C3」と言っても「ボタンの色と、書いてある文字を教えて」が返り続けた
- Changed:
  - `neuro_voice/games/ktane/expert.py` `_solve_button`: 色も文字も無い発話は `None` を返して会話側へ渡す。片方だけ分かっているときは**足りない方だけ**聞く（「分かっている方まで聞き直さない」）
  - `neuro_voice/games/ktane/expert.py`: 「押す」をボタンの文字の別名から**削除**。普通の動詞として頻出するので、「青いボタンを押すね」で `label=press` と誤読し、**条件を1つ飛ばして間違った指示を確定させる**危険があった
  - `neuro_voice/games/ktane/expert.py`: `_without_repeating_itself()`。**同じ質問を2回続けて返さない**。2度目は黙って会話側へ渡す。今回はボタンで露出したが、モジュールが増えれば同じ壊れ方は再発しうるので、経路の出口に一箇所だけ置いた
  - `tests/test_ktane_conversation.py`: +5件（手がかり無しの発話に質問を返さない／同じ一文を2回返さない／足りない方だけ聞く／動詞の「押す」を文字と取り違えない／両方そろえば解ける）
- Behavior impact: セッション中に脱線しても会話が普通に続く。ボタンの判定は変わらない
- Config / DB migration: なし
- Tests: 全体 **1249 passed / 17 subtests**
- Handoff: `docs/handoffs/2026-07-31_2340_claude_ktane-rules.md`（追記）
- Related ADR / decision: 憲法第2条・第17条

## 2026-08-01 00:30 JST — Claude — 記憶モジュールの履歴取得と、残り3モジュールの解法

- Status: completed（自動回帰済み。**キーパッド・心理戦・迷路は表の取り込みが要る**）
- Summary: (1) 記憶モジュールで押した結果を発話から拾えるようにした。(2) キーパッド・心理戦・迷路の**解法をコードに実装**し、表は `manual/tables/*.json` から読む形にした
- **記憶モジュール**: 指示を出した側は位置か数字の**片方を知っている**（「左から3番目を押して」なら位置は既知）。控えておき、次の発話で足りない方だけを受け取る。「2だった」の一言で `3番目の「2」` として履歴へ入る。「次は画面が2」は次の段の申告なので押下結果と取り違えない。これで**1段目から5段目まで発話だけで通る**
- **残り3モジュール**: 解法はコードにある（キーパッド=4記号を全部含む列を探して並び順、心理戦=2段の表引き、迷路=通路グラフの幅優先探索）。**表だけが空**
- **なぜ表を空で出したか**: キーパッド6列×7記号、心理戦は画面の語28件＋語ごとの順序付きリスト28本、迷路は9面×6×6。**記憶から書き起こすと一箇所間違えても誰も気づかない。気づくのは爆発した時**。公式マニュアル（bombmanual.com、無料・日本語）を見ながら写す作業として分離した。手順は `docs/KTANE_TABLES.md`（30分ほど）
- Changed:
  - `neuro_voice/games/ktane/expert.py`: `pending_memory_position` / `pending_memory_label` と `_complete_memory_press`。3モジュールの入力組み立て（心理戦のボタン一覧、迷路の丸・現在地・出口を発話をまたいで保持）。**表が無いモジュールは名前が出た時点で断る**（座標を聞き出してから「やっぱり分からない」と言わない）
  - `neuro_voice/games/ktane/tables.py`（新規）: JSONの読み込みと検証。**壊れたJSONで会話ごと落とさない**（第17条）。`THEY'RE`/`THEYRE` の表記ゆれを吸収
  - `neuro_voice/games/ktane/modules.py`: `solve_keypad` / `solve_whos_on_first` / `solve_maze`。迷路は壁ではなく**通れる辺**で持ち、逆向きはコードが補う
  - `neuro_voice/games/ktane/parse.py`: `parse_symbols`（**呼び名は利用者のもの。中の助詞まで削ると「しっぽ付きの6」が「しっぽ付き6」になり表と一致しなくなる**ので前後だけ削る）、`parse_words`、`parse_coordinates`（「左から2、上から3」でも「2,3」でも受け、6×6の外は捨てる）
  - `neuro_voice/games/session.py`: プロファイルのフォルダから表を読み込む
  - `game_profiles/.../manual/tables/{keypad,whos_on_first,maze}.json`（新規・空）、`docs/KTANE_TABLES.md`（新規）
  - `tests/test_ktane_tables.py`（新規30件）、`tests/test_ktane_conversation.py`（+6件）
- Behavior impact: 記憶モジュールが会話だけで最後まで解ける。3モジュールは表を入れれば解ける／入れるまでは断る
- Config / DB migration: なし。表を書き換えたら**ポッポの再起動**（セッション開始時に読む）
- Tests: 全体 **1244 passed / 17 subtests**
- Remaining:
  - **表3つの取り込み。`docs/KTANE_TABLES.md` の手順。AIに記憶で埋めさせないこと**
  - 埋めたら `test_the_shipped_files_are_valid_but_empty` は落ちる。**それは正常**なので「取り込み済み」を確かめる形へ書き換える
  - Needy系（換気・コンデンサ・つまみ）は未着手
- Handoff: `docs/handoffs/2026-07-31_2340_claude_ktane-rules.md`（追記）
- Related ADR / decision: 憲法第2条・第17条

## 2026-07-31 23:40 JST — Claude — 爆弾解除の規則をローカルに実装（**マニュアルが1行も無かった**）

- Status: completed（自動回帰済み。実機未検証）
- Summary: 「爆弾解除モードにして」も版・検証コードも通るようになったのに、**指示が全くできなかった**。ログとフォルダを確認したところ、`game_profiles/keep_talking_and_nobody_explodes/` は**合計8KB、中身は役割の説明と版番号と検証コードだけで、規則が1行も入っていなかった**。ポッポは「マニュアル担当」を名乗りながら手元に何も持っておらず、質問を返すことしかできなかった
- **判断はコードが行い、モデルは確定した答えを言うだけにした**（第2条）。12Bのモデルに「赤が2本以上でシリアル末尾が奇数なら最後の赤」を解かせると間違える。爆弾では間違いが爆発になる
- Changed:
  - `neuro_voice/games/ktane/edgework.py`（新規）: シリアル・電池・インジケーター・ポート・ミス数。**未確認は既定値で埋めず `None` のまま**（知らない電池数を「2個未満」と扱うと誤った線を切らせる）
  - `neuro_voice/games/ktane/modules.py`（新規）: **配線・ボタン・サイモン・記憶・モールス信号・複雑な配線・順番に配線・パスワード**の判定。答え／不足している情報／規則が無いの3値を返す
  - `neuro_voice/games/ktane/parse.py`（新規）: 話し言葉から状態を読む。「赤、青、赤の3本」「上から白黒白黄」「シリアルはA7B3C1」。**読めない断片は捨てる**（シリアルが6桁前後でなければ採用しない。誤った末尾の数字はそのまま誤った指示になる）
  - `neuro_voice/games/ktane/expert.py`（新規）: 爆弾1つぶんの記憶。1発話で全部そろわないので状態を持つ。記憶モジュールの押下履歴、モールスの読み取り文字も継ぐ
  - `neuro_voice/games/session.py`: 進行中は `_defusal_outcome()` を先に通し、**答えが確定したらLLMを通さず `ActivityOutcome` で直接返す**。セッション開始のたびに `KtaneExpert` を作り直す（前の爆弾のシリアルを持ち越さない）。`grounded_context` に爆弾の現状と「規則があるモジュール一覧」「判断は済んでいるので条件を考え直すな」を追加
  - `game_profiles/.../manual/sources.json`: 実装済み／未実装モジュールを明記
  - `tests/test_ktane_rules.py`（新規63件）、`tests/test_ktane_conversation.py`（新規27件）
- **実装していないモジュール**: キーパッド（記号を音声で特定できない）、心理戦（対応表が大きく、記憶だけでは正確性を保証できない）、迷路（格子の絵が要る）、Needy系。これらは `unknown_module` で「手元に規則が無い」と言う。**推測で答えない。間違った指示は「分からない」よりはるかに悪い**
- Behavior impact: セッション中に配線などを説明すると、確定した指示が直接返る（LLMを経由しない）。情報が足りない時は不足を1つだけ尋ねる。規則の無いモジュールは断る
- Config / DB migration: なし
- Tests: 全体 **1208 passed / 17 subtests**（pyflakes 3件を含む）。複雑な配線は16通りすべてを踏む
- Remaining:
  - 実機での動作確認。特にSTTの誤変換で色やシリアルが化けた時の挙動
  - キーパッド・心理戦・迷路を埋める場合は、**公式マニュアルを見ながらデータとして起こすこと。記憶で書き足さない**
  - `record_memory_press` は今のところ手動呼び出し。記憶モジュールの押下結果を発話から拾う経路は未実装
- Handoff: `docs/handoffs/2026-07-31_2340_claude_ktane-rules.md`
- Related ADR / decision: 憲法第2条（LLMが意味・コードが検証）・第17条

## 2026-07-30 01:10 JST — Claude — 爆弾解除モードにしても版・検証コードを答えられなかった

- Status: completed（自動回帰済み。実機未検証）
- Summary: 「爆弾解除モードにして」の直後に「マニュアルは何版？検証コードは？」と聞いても答えられなかった。原因は `grounded_context()` が **`manual_expert` かつセッション未開始なら `None` を返していた**こと。ポッポの手元には何の情報も無く、しかもKTANE選択中はウェブ検索も止まるので答えようがなかった。**「始めよう」と言うまで版すら答えられないのは、利用者から見えない前提条件**だった
- **もう1件、値の二重管理を解消**: 版`1-ja`と検証コード`122`が `profile.json` / `manual/sources.json` / **`session.py` のリテラル**の3箇所にあった。`profile.json` を直しても発話は古い値を言い続ける状態。`GameProfile` が正本を読むようにした
- Changed:
  - `neuro_voice/games/profiles.py`: `GameProfile.manual_version` / `manual_verification_code` を `profile.json` の `manual` から読む。`manual_reference` プロパティ（値が無ければ空文字。中身の無い参照文を作らない）
  - `neuro_voice/games/session.py`: 未開始の `manual_expert` では `None` ではなく**短い参照だけ**返す（プロファイル名／役割／版・検証コード／「まだ始まっていないので手順の指示はしない。ただし今分かることは普通に答える」）。約90tok、KTANEが選ばれている時だけ。セッション中の文面もリテラルをやめてプロファイル由来へ
  - `tests/test_game_profile_switch.py`: +4件（開始前に版が答えられる／未開始と明示される／`profile.json` を直せば発話も変わる／マニュアルが無ければ言及しない）
- Behavior impact: 爆弾解除モードを選んだだけの状態でも、版・検証コード・役割を答えられる。手順の指示はセッション開始後のまま
- Config / DB migration: なし
- Tests: 全体 **1118 passed / 17 subtests**
- Remaining: 実機で「爆弾解除モードにして」→「マニュアルは何版？」を確認
- Handoff: `docs/handoffs/2026-07-30_0030_claude_game-profile-voice-switch.md`（追記）
- Related ADR / decision: 憲法第2条（事実は正本を1つに）・第17条

## 2026-07-30 00:30 JST — Claude — ゲームプロファイルを会話で切り替える（会話品質の調査とは独立）

- Status: completed（自動回帰済み。実機での音声入力は未検証）
- Summary: `GameProfileSessionManager.handle_final_input` は冒頭で「選択中がKTANEでなければ何もしない」と戻っていたため、**Minecraftのまま「爆弾解除を始めよう」と言っても何も起きなかった**。モードを変えるためだけに `config.yaml` を手で編集させる作りは音声アシスタントとして不自然なので、会話で切り替わるようにした
- 使い方: 「爆弾解除モードにして」→切り替え / **「爆弾解除を始めよう」→切り替えてそのままセッション開始** / 「マイクラに戻して」→戻す（爆弾解除セッションは終了）/ 「今どのゲームモード？」→現在の状態
- **線引き（この実装で一番大事なところ）**: 切り替わるのは**「切り替えて」と読める言い方**と**プロファイルの別名**が同じ発話に両方そろった時だけ。「静かにして」（別名なし）、「マイクラの話なんだけどさ」（意図なし）、「爆弾解除って難しそうだよね」（同）では**何も起きない**。切り替えの語だけあって別名が無い時に聞き返すのは、`ゲーム|モード|プロファイル` が同じ文にある場合に限る（そうしないと「静かにして」で使えるゲームの一覧を読み上げ始める）。**登録のないゲーム名には勝手に切り替えず、使えるものを答える**
- Changed:
  - `neuro_voice/games/profiles.py`: `alias_items()`（長い別名が先。「keep talking」で途中一致して終わらないため）/ `labels()`
  - `neuro_voice/games/session.py`: `_SWITCH` / `_GAME_WORD` / `_WHICH`、`switch_to()`、`_switch_outcome()`、`_start()` の切り出し。**冒頭でKTANE以外を弾くのをやめた**。切り替え時は進行中セッションを終了する（別のゲームへ移ったのに爆弾解除が生き残ると、以後の発話が持ち主のいない状態へ入る。第5条）
  - `neuro_voice/pipeline.py`: `_apply_switched_game_profile()`。`reason` が `game_profile_switched` で始まる時に `refresh_video_observation()` を呼ぶ。**設定値だけ変えても、起動時に始まったMinecraftのキャプチャは動いたまま残る**。映像更新の失敗で会話は止めない（第17条）
  - `tests/test_game_profile_switch.py`（新規25件。**半分は誤爆しないことのテスト**）、`tests/test_game_profiles.py`（旧挙動を固定していたテストを新しい意図へ）、`game_profiles/README.md`
- Behavior impact: `video.game_profile` が**会話で書き換わる**。`config.yaml` へ書き戻すので次回起動でも同じモード。映像を使わないプロファイルへ移ると画面キャプチャがその場で止まる（再起動不要）
- Config / DB migration: なし（`video.game_profile` の値が実行時に変わるようになっただけ）
- Local/Discord parity: Discordの画面共有は**開始時に config を読む**ため、設定への書き戻しで両方に効く。ローカルだけ、すでに走っているキャプチャの付け替えが要る
- Tests: 全体 **1114 passed / 17 subtests**（pyflakes 3件を含む）
- Remaining:
  - 実機で音声から言った時、STTの揺れ（「爆弾解除」→「爆断解除」等）で別名に当たらない可能性がある。対処は `profile.json` の `aliases` に読みの揺れを足すこと。**切り替えのコードは触らなくてよい**
  - 新しいゲームを足す時も `game_profiles/<id>/profile.json` を置くだけ
- Handoff: `docs/handoffs/2026-07-30_0030_claude_game-profile-voice-switch.md`
- Related ADR / decision: 憲法第5条・第17条・第19条

## 2026-07-29 23:30 JST — Claude — 削減しても書き出しは動かず。**診断が誤りだったので訂正する**（次はサンプリングの検証）

- Status: completed（計測・訂正・診断ツールの用意まで。**次の一手はまだ打っていない**）
- Summary: −743tok の状態で台本を再走（turn 1748〜1755）。**冒頭ハッシュは4回目も 8/8 同じ**（`a4b5 / 2497 / 2f4d / 1d5e / a4b5 / a1c8 / ab06 / 3cf3`）。プロンプト削減は書き出しを1文字も動かさなかった
- **【重要】これまでの診断は誤りだった。訂正する。**
  - `data/dialogue_neuro.json` の `recent_replies` で実際の書き出しを確認したところ、**定型文ではなかった**。入力2「うんうん」→「『うんうん』って言われると、なんだか言葉…」、入力3「なるほど」→「『なるほど』って一言だけで、なんだか深い…」、入力6「ランダムに失敗させれば」→「あはは、それいい！わざとドジを踏むって…」。**一つ一つ、その入力にきちんと応じている**
  - したがって「モデルは規則の山に応答していてユーザーを見ていない」という21:50の見立ては**間違い**。ポッポは聞いている
  - 本当の症状は「**同じ入力には必ず同じ返答が返る**」。1番「AI感が強い」と5番「またAIが『なるほど』って言ってる」は、モデルから見れば同じ刺激（AIっぽい喋り方への指摘）なので、両方に「あはは、それ言われるとちょっとだけドキッ…」を返している
  - つまり「計画が言葉に届かない」のではなく、**ある入力から言葉へ至る道が1本しかない**
- **次に確かめること（最優先。これより先へ進まないこと）**: `llm.temperature = 0.7`、seed 指定なしで、**4回 × 8ターン = 32サンプル、プロンプトが743tok違っても書き出しが1度も変わらない**。これは温度0.7の挙動ではなく**温度0の挙動**。サンプリングが効いていない疑いがある。効いていないなら、プロンプトをいくら削っても何も変わらない
- Changed:
  - `tools/check_sampling.py`（新規）: 同じ一文を3回送るだけの診断。会話履歴もペルソナも使わず、アプリを起動したまま実行できる。「アプリと同一経路(temp 0.7 + options)」「optionsなし」「temp 1.0」「Ollamaネイティブ」の4通りを比較し、モデル自身の既定パラメータ（`/api/show`）も表示する
- Behavior impact: なし（診断ツールの追加のみ）
- Tests: コード本体は変更していないため未実行（直前の全体回帰 1023 passed が有効）
- Remaining（**この順で**）:
  1. `python tools/check_sampling.py` を実行する。読み方はスクリプト末尾が出力する
     - **どれも「全部同じ」** → サンプリングが効いていない。プロンプトの問題ではない。`llm/openai_compat.py` の `extra_body["options"]` が OpenAI互換層で温度を落としていないか、モデル側の既定に `seed` が無いかを見る
     - **temp=1.0 だけ割れる** → 0.7 がこのモデル（`gemma4:12b-it-qat`。Gemma公式推奨は temp 1.0 / top_k 64 / top_p 0.95）には低すぎる。`config/config.yaml` の `llm.temperature` で直る
     - **どれも割れる** → サンプリングは正常。原因は入力側。その時の一手は「**直近N件の書き出しを本文のままプロンプトへ渡し、別の入り方にさせる**」。スタイル名のような抽象語ではなく具体的で検証可能な制約なので、埋もれても効きやすい。`recent_replies` に既に材料がある
  2. `target_length` がプロファイル `interaction_policy.response_length` で `long` に固定される件（`conversation_planner.py:382-388`。未着手）
  3. `Mind.build_context` の4700〜5800tok（プロンプト側に残る最大の余地。**ただし1が片付くまで手を出さないこと**）
- **やってはいけないこと**: 会話エンジン・名前付き行動の追加。2026-07-29の対照実験が明確に否定している
- Handoff: `docs/handoffs/2026-07-29_2330_claude_diagnosis-corrected.md`
- Related ADR / decision: 憲法第2条・第12条・第20条

## 2026-07-29 22:40 JST — Claude — 規則をプロンプトからコードへ移す（棚卸しのA・Dを実施、常時ON −743tok）

- Status: completed（自動回帰済み。実機での8文再走はこれから）
- Summary: 対照実験で「返答を決めているのは常時ONのプロンプト」と分かったので、`docs/PROMPT_RULE_AUDIT.md` の残り候補を実施した。**常時ON 3177 → 2899 tok**（棚卸し開始時の3642からは **−743tok / 20%**）。内訳: persona 1916 / planner 547 / surface 436。**ユーザーから persona 変更の明示的な許可を得ている**
- 分類の基準は憲法第2条（LLMが意味 / コードが事実）。`response_shape=` が発話に混ざったか、返答がユーザー発話の繰り返しか——**文字列を見れば分かる事実**はモデルへ頼まない
- Changed:
  - `neuro_voice/dialogue/leakage.py`（新規）: `has_internal_leak` / `leak_markers` / `strip_internal_leak`。**意図的に狭い**——ASCIIの項目名、角括弧の内部タグ、自前のセクション見出しだけを見る。誤検出は実発話を黙らせるので漏れより悪い。文単位で落とすので1行の漏れで回答全体を捨てない（第17条）
  - `neuro_voice/pipeline.py` `_speak_loop` / `neuro_voice/discord_bridge/bot.py` `_speak`: 合成直前に検査（第19条）。ログへ残すのは検出した印だけで周辺の文は残さない（第12条）
  - `neuro_voice/memory/persona.py`: 内部語の**列挙を削除**（数え上げても漏れていた）。「オウム返しはしない」を削除
  - `neuro_voice/dialogue/echo.py`: `is_echo` / `ReplyEchoGuard` が複数の照合元を受け付ける。`pipeline._reply_echo_guard` が直前のユーザー発話も渡す（「何も新しいことを言っていない」は自分の繰り返しと相手の繰り返しの2通り）
  - `neuro_voice/dialogue/surface_realizer.py`: 「毎回『なるほど』…から始めない」を**常時ONから条件付きへ降格**。`ConversationCritic` が実際に検出した時だけ `feedback_prompt` が同じことを言う
  - `neuro_voice/dialogue/conversation_planner.py`: 人格モジュールの数値ダンプを削除（`SurfaceRealizer` が同じ数値を日本語指示へ変換済みで二重だった）。話題候補を上位2件・score なしへ
  - `tests/test_leakage.py`（新規28件。**半分は誤検出のテスト**）、`tests/test_reply_echo.py`（+6）、`tests/test_prompt_rules.py`（移設後の状態へ。上限 3400→**3000** tok）、`tests/test_conversation_generation.py`
  - `docs/PROMPT_RULE_AUDIT.md`: 実施結果を追記
- Behavior impact: 内部値が発話へ混ざった場合に**その文だけ落ちる**（従来は読み上げていた）。ユーザー発話のオウム返しが発話前に止まる。プロンプトが短くなった
- Config / DB migration: なし
- Tests: 全体 **1023 passed / 11 subtests**
- **効果の見積もりは正直に**: プロンプト評価は 0.244ms/tok なので −278tok ≒ 68ms。**テンポへの効果はこの程度しかない**。本題は「規則の山」を減らすこと
- Remaining:
  - **8文を流して冒頭ハッシュ `a4b5 / 2497 / 2f4d / 1d5e / a4b5 / a1c8 / ab06` が割れるか見る。** 割れなければ −743tok では足りず、次は `Mind.build_context` の4700〜5800tok
  - `target_length` がプロファイルで `long` に固定される件（未着手）
  - `docs/CODEMAP` へ `dialogue/leakage.py` を追記（未実施）
- Handoff: `docs/handoffs/2026-07-29_2240_claude_prompt-rules-to-code.md`
- Related ADR / decision: R-038、憲法第2条・第12条・第17条・第19条

## 2026-07-29 21:50 JST — Claude — 対照実験の決着: Plannerは書き出しをほとんど動かしていない

- Status: completed（対照取得・記録・計測バグ修正まで完了。プロンプト削減はこれから）
- Summary: `dialogue.conversation_generation.enabled: false` で同じ台本を流した。**冒頭ハッシュが有効側と 7/8 で一致**（`a4b5 / 2497 / 2f4d / 1d5e / a4b5 / a1c8 / ab06` が一致、8番だけ `3cf3`→`8884`）。さらに**無効側では1番と5番が冒頭・語尾・文字数すべて一致**（`a4b5 / 988d / 71字`）——**別々の入力に同一の返答**を返した。`temperature 0.7` / seed なしで、である
- **結論**: `ConversationPlanner` の738tokは書き出しをほぼ動かしていない。**会話エンジンを足す方向は誤り**。返答を決めているのは常時ONのプロンプトであって、計画でも履歴でもユーザーの言葉の細部でもない
- **数字の見立て**: systemブロック 5000〜6000tok に対し `num_ctx = 8192`、`context_reserve_tokens = auto`（max_tokens 1024 + 余裕）。**履歴に残る予算は1000tok強しかない**。モデルは「ユーザーに」ではなく「規則の山に」応答している状態
- **自分の計測バグを1件修正**: `last_plan` はPlannerを止めてもプロファイルに残るため、対照側の8ターンが**存在しない計画に対する `proposal_path / reflective / long` と `target_length` 違反**を記録していた。対照が計画を捏造しては比較にならない
- Changed:
  - `neuro_voice/dialogue/intelligence.py`: `_record_plan_metrics` でPlanner無効時は `plan = {}` にする
  - `tests/test_plan_metrics_control_arm.py`（新規3件）
  - `config/config.yaml`: `conversation_generation.enabled` を **`true` へ復帰**（結果をコメントで併記）
  - `logs/metrics_planner_off.jsonl` / `logs/metrics_planner_on.jsonl`: 両側を退避
- Behavior impact: 計測のみ。会話の挙動は変えていない
- Config / DB migration: `enabled` は実験前の `true` に戻っている
- Tests: 全体 **986 passed / 11 subtests**
- Remaining:
  - **常時ONのプロンプトを削る**。`docs/PROMPT_RULE_AUDIT.md` の残り候補 + `Mind.build_context` の5000tokブロック。削った状態で同じ8文を流し、冒頭ハッシュが割れるかで効果を判定する（**判定基準がようやく手に入った**）
  - `target_length` がプロファイル `interaction_policy.response_length` で `long` に固定される件
  - **やらないこと**: 会話エンジン・名前付き行動の追加。対照実験がこれを否定した
- Handoff: `docs/handoffs/2026-07-29_2035_claude_plan-adherence-first-run.md`（追記部）
- Related ADR / decision: 憲法第2条・第12条・第20条

## 2026-07-29 21:10 JST — Claude — 2回目の計測: 計画は言葉に一切届いていない（コード変更なし・記録のみ）

- Status: completed（計測と記録のみ。**コードは変更していない**）
- Summary: 較正後に同じ台本をもう一度流した（turn 1732〜1739、再起動なし）。**冒頭ハッシュ列が2回とも8/8で完全一致**した。shape も style もすべて違うのに書き出しが同じ。3番は語尾ハッシュと文字数まで一致しており、ほぼ同一の発話。`llm.temperature = 0.7` / seed なしで、**確率的サンプリングを通してなお一致する**。run2 の履歴には run1 の8往復が入っているので、履歴が違っても書き出しが動かない。→ **書き出しは実質ユーザー入力だけの関数で、計画は言葉に届いていない**
- **前回の主張の訂正**: 「Plannerは十分に振れている」は **shape/style については正しいが、`target_length` と `ask_follow_up` については誤り**だった。`planned_length` は8ターンとも **`long` 固定**（`conversation_planner.py:382-388` がプロファイルの `interaction_policy.response_length` を見ており、`data/dialogue_neuro.json` の値が `long`。**ターンごとに選ばれていない**）。`planned_question` も8ターンとも `False`
- 較正後の違反: `target_length` 5/8（計画=long ≥100字に対し 69/61/81/74/71字）、`ask_follow_up` 2/8、`primary_style` 1/8。`adherence_mean 0.698` / `strict_mean 0.396` / `checkable_ratio_mean 0.563`
- **伝達が失敗する場所の見立て**: 計画ブロックは `insert_before_user_turn` でユーザー発話の直前に入っており位置は悪くない。`_STYLE_INSTRUCTIONS` / `_SHAPE_INSTRUCTIONS` の文面も互いに十分違う。それでも効かない。**systemブロックだけで5000〜6000tok、`num_ctx` は 8192**。計画738tokは抽象的な内部パラメータの羅列としてその中に埋もれている。`follow_up=no` という二値の指示ですら25%破られている以上、**この規模のプロンプトでは指示そのものが従われていない**
- Changed:
  - `config/config.yaml`: `dialogue.conversation_generation.enabled` を **`false` へ一時変更**（対照実験のため。経緯をコメントで併記。**計測後 true へ戻す**）
  - `logs/metrics_planner_on.jsonl`: Planner有効側16ターンを退避（無効側と混ざらないように）
  - `docs/handoffs/2026-07-29_2035_claude_plan-adherence-first-run.md`: 2回目の結果を追記
- Behavior impact: **Planner / SurfaceRealizer / Critic が無効**。対照を取り終えたら戻す
- Tests: コードは変更していないため未実行（直前の全体回帰 983 passed が有効）
- Remaining:
  - **Planner無効側（`dialogue.conversation_generation.enabled: false`）の対照をまだ一度も取っていない。** 無効でも同じ8つの冒頭ハッシュが出るならPlannerは何も寄与していない。再起動1回と8メッセージで済むので**次はこれを先に取る**
  - その後は**プロンプトを削る**方向。**Plannerを増やしてはいけない**
  - `target_length` がプロファイル固定で毎ターン同じになる件（Planner側の実問題）
- Handoff: `docs/handoffs/2026-07-29_2035_claude_plan-adherence-first-run.md`（追記部）
- Related ADR / decision: 憲法第2条・第12条・第20条

## 2026-07-29 20:40 JST — Claude — Plan Adherence 初回計測の結論と、自作の物差しの較正

- Status: completed（自動回帰済み。較正後の再計測は実機待ち）
- Summary: ユーザーが `docs/CONVERSATION_AB_SCRIPT.md` のA〜Fを打ち込み、「1と5の応答内容はほぼ一緒」と報告。その8ターンのログを読み、**原因は (b) 伝達**だと確定した。同時に、**自分の計測が1つ壊れていた**ので較正した。
- **切り分けの結論**: shape 5種類 / style 5種類、`shape_streak = 1` / `style_streak = 1` ——**Plannerは十分に振れている**。よって「Plannerが毎回同じ判断をしている」は否定。それでも **ターン1（proposal_path / playful）とターン5（react_expand / opinionated）の冒頭ハッシュが同じ `a4b5`**。計画がまるで違うのに書き出しが一致した。**Plannerを増やしても効かない。直すのは計画を本文へ届ける経路**
- **自分の誤り**: 初回の `target_length` 6/8 違反は**信用してはいけない数字**だった。実測の返答は61〜135文字なのに旧帯は `short=(0,70)` / `long=(110,∞)`。**71文字はshortを1文字超え、106文字はlongを4文字下回った**だけで「計画無視」と判定していた。さらに**計画された長さの値をログへ残しておらず**、事後にどの帯だったのか確認すらできなかった。同じ日にnum_ctx掃引の平均で嘘の数字を出したのと同型の失敗（**測る側が壊れているのに、測られる側の欠陥として報告した**）
- Changed:
  - `neuro_voice/dialogue/plan_metrics.py`: `_LENGTH_BANDS` を `short=(0,95)` / `medium=(45,220)` / `long=(100,10000)` へ較正。日本語の話し言葉は1文25〜35文字、personaは1〜3文を求める——**推測ではなく文数**を根拠に引き直し、帯を意図的に重ねた（境界での数文字差は不服従ではない）
  - `neuro_voice/dialogue/plan_metrics.py`: `TurnRecord.planned_length` / `planned_question` を追加し `record_for()` と `snapshot()` へ配線。`planned_question` は「未指定」と「質問しない計画」を区別するため `bool | None`
  - `tests/test_plan_metrics.py`: 実測値71/106文字が違反にならないこと、計画値が記録されること、未指定が `False` に潰れないこと（+5件、計54件）
- Behavior impact: 計測のみ。会話の挙動は変えていない。LLM呼び出しの増加もゼロ
- Config / DB migration: なし。ただし**既存の `logs/conversation_metrics.jsonl` は旧帯で書かれている**ので混ぜて読まないこと。再計測分から `planned_length` 列が入る
- Tests: 全体 **983 passed / 11 subtests**
- Remaining:
  - 較正後にA〜Fをもう一度流し、`target_length` を信用できる数字にする。**`ask_follow_up` 3/8違反 と ターン1/5の冒頭衝突は帯の較正の影響を受けない**ので、この2件の結論は再計測を待たずに有効
  - 次は item 7（Nターンのソフトペナルティ）**ではなく伝達側**を疑う: `conversation_planner.py` の計画ブロックの表現と、`pipeline.py` の `insert_before_user_turn` 適用位置
- Handoff: `docs/handoffs/2026-07-29_2035_claude_plan-adherence-first-run.md`
- Related ADR / decision: R-037（テストが実行しないコード）に加え、**「較正されていない物差し」も同じ危険**として次回R更新時に追記したい。憲法第2条・第12条・第20条

## 2026-07-29 20:30 JST — Claude — 計画反映率の計測、テキスト入力が無応答だった不具合、未定義名の検出

- Status: completed（自動回帰済み、実機での計測はこれから）
- Summary: (1) `ConversationPlanner` の判断が実際の発話へ届いているかを測る **Plan Adherence** を実装した。(2) その比較テストをテキスト入力で行おうとして、**テキスト入力が一度も応答しない不具合**を発見・修正した。(3) 同じ形の不具合を静的に潰すため pyflakes を回帰へ入れ、さらに2件見つけて直した。
- **テキスト入力の不具合**: `_respond_from_text` が `transcript=transcript` を渡していたが、この名前は音声経路にしか存在せず、**打ち込んだメッセージは毎回 `NameError` で無応答**だった。2026-07-26のASR不確実性の作業で入り込み、音声中心の利用のため今まで表面化しなかった。同経路には沈黙ターンのDirective通知も欠けていたので併せて追加
- Changed:
  - `neuro_voice/dialogue/plan_metrics.py`（新規）: `evaluate` / `record_for` / `DiversityWindow`。`ask_follow_up` / `target_length` / `minimum_moves` / `response_shape` / `primary_style` を完成した本文と突き合わせ、規則で判定できないものは `unverifiable` として**黙って「達成」に数えない**。`adherence`（検証できた範囲）と `strict`（判定不能を未達）と `checkable_ratio` を**すべて併記**する
  - `neuro_voice/dialogue/intelligence.py`: 計画と最終応答が揃う唯一の場所（`commit`隣）へフック。**`plan` はここでは dict** なので、オブジェクト前提で書くと全項目が空になるところだった（書く前に確認して両対応にした）
  - `neuro_voice/mind/mind.py` `neuro_voice/pipeline.py`: `last_plan_metrics` の公開と、`logs/conversation_metrics.jsonl` への書き出し。**発話本文は記録しない**（第12条）。冒頭・語尾は2バイトのハッシュのみ
  - `neuro_voice/pipeline.py`: `transcript=None` へ修正。沈黙ターンのDirective通知を追加
  - `neuro_voice/discord_bridge/bot.py`: `Any` の import 漏れ（型注釈のみのため実行時は無害だったが潜在）
  - `neuro_voice/audio/loopback.py`: `list_output_devices` の**二重定義**。34行目の旧実装は136行目に隠れて一度も実行されていなかった（編集しても効果が出ない罠）。経緯をコメントに残して削除
  - `config/config.yaml`: `conversation.metrics.*`
  - `tests/test_plan_metrics.py`（新規49件）、`tests/test_no_undefined_names.py`（新規3件）
  - `docs/CONVERSATION_AB_SCRIPT.md`（新規）: Planner有無の比較台本
- Behavior impact:
  - **テキスト入力が動くようになった**（これまで無応答）
  - 会話の判断ロジックには一切触れていない。計測とログのみ。追加のLLM呼び出しはゼロ
- Config / DB migration: `conversation.metrics.*`（新規）。**GUI再起動が必要**
- Tests: 全体 975 passed / 3 skipped / 11 subtests。**pyflakes を回帰に組み込んだ**ので、未定義名と二重定義は今後CIで落ちる
- 反省: 今日の障害2件（`keep_alive`、`transcript`）はどちらも**テストが一度も実行しないコード**にあった。`compileall` では捕まらない。pyflakesはコードを読むだけで両方を検出する。R-037へ追記
- Remaining:
  - `docs/CONVERSATION_AB_SCRIPT.md` に従い、Planner有効/無効で同じ8文を流して比較する
  - 結果次第で item 7（Nターンのソフトペナルティ）へ進む。`DiversityWindow` が入力をすでに出している
- Handoff: `docs/CONVERSATION_AB_SCRIPT.md`
- Related ADR / decision: R-037、憲法第2条・第12条・第20条

## 2026-07-28 07:00 JST — Claude — プロンプト規則の棚卸し（提案。コード変更なし）

- Status: completed（調査と提案のみ。**コードは1行も変更していない**）
- Summary: R-038に対する棚卸しを実施した。常時ONのプロンプトは実測 **3840tok**（persona 2296 / ConversationPlanner 738 / SurfaceRealizer 608 / TimeAwareness 127 / 確信度 71）で、これに `Mind.build_context` の4700〜5800tokが加わる。各規則を「コードで検証できる / 判断が要る / 重複・矛盾」で分類し、**−804tok（常時ONの約21%）の削減候補**を洗い出した。personaはユーザーの創作物なので提案に留めている。
- **見つかった重複・矛盾**:
  - **「内部の分析・候補・scoreを出力するな」が7箇所・約300tok**。`『The user is asking』『Drafting response』『Wait』『Final version』` は**一字一句同じ列挙が2箇所**にある（surface と persona）
  - **感情タグの指示が2回あり、「2つ付けろ」と「1つだけ付けろ」で食い違う**。パーサ（`utils/emotion.py`）は両形式を受けるので機能不具合ではないが、**モデルは最初の1トークンを出す前に矛盾を解かされている**。約225tok
  - **完全に同一の文が2回**（`persona.py` 68行目と69行目）
  - 「質問だけで終わるな」が4箇所。うち `ASK_FOLLOW_UP` は**コードが既に判定して yes/no を渡している**
- **コードへ移せるもの**: 「オウム返しはしない」は2026-07-28に `echo.is_echo()` を実装済み。「『なるほど』『確かに』から始めない」は `conversation_critic._CANNED` が同じ語を正規表現で検出済み。内部語の出力禁止は出力検査1つで代替できる
- **要判断**: 人格モジュールの数値ダンプ78tok（`SurfaceRealizer._persona_expression_rule` が同じ数値を読んで日本語へ変換しており二重）、話題候補とスコア約135tok（Plannerが既に最上位を選んでいる）
- **残すもの**: 事実性・非迎合ルール（憲法第1条のINVARIANTそのもの。コードでは判定できない）、ASR誤変換の扱い、キャラクター設定187tok、人格表現
- Changed: `docs/PROMPT_RULE_AUDIT.md`（新規。行単位で承認できる表）
- Behavior impact: **なし**
- Tests: コード変更なしのため実行なし（直前の全体は914 passed）
- Remaining:
  - **C（重複・矛盾）だけ先に実施することを推奨**。−561tok、失うものがない。ただしどの文を残すかは書き手の意図に関わるため**ユーザーの指定を待つ**
  - 効果は約200msでテンポへの寄与は限定的。**本当の効果は「最初のトークンで矛盾を解かせない」ことと、規則:人格の比率が9対1から8対1へ動くこと**
  - Codex側の領域（`_MODE_GUIDE` / `companion.py` / `vision/service.py` / `minecraft_knowledge.py`）は未着手。同じ観点で確認が必要
- Handoff: `docs/PROMPT_RULE_AUDIT.md`
- Related ADR / decision: R-038、D-016、憲法第2条

## 2026-07-28 06:00 JST — Claude — 【総括】セッション全体の振り返りと、Codexへの確認依頼

- Status: completed（総括。コード変更なし）
- Summary: 2026-07-26〜28の一連の作業を、**失敗も含めて**まとめた。ユーザーの出発点は「ネウロ様と比べてテンポとセンスが届いていない」で、セッション末の評価は**「最初に戻った気がする」**。この評価は正しい。テンポは3.5秒のまま、センスも変わっていない。実機で観測された不具合10件は解消したが、依頼の本体には届いていない。
- **最重要の発見（未着手）**: `persona` ブロック2296トークンの内訳は「規則2109 tok / キャラクター設定187 tok」。**モデルが読むものの9割が「してはいけないこと」**。`persona.py` だけで禁止形が11個、68行目と69行目は**完全に同一の文が2回**書かれている（誰も読み返していない証拠）。規則の中身は過去の不具合を1つ直すたびに積み上がった層で、私自身も今日だけで確信度の許可文・継続依頼ブロック・物語メモを足している。**これがテンポ（プロンプト評価1350ms）とセンス（規則の声で喋る）を同時に説明する。**
- **Codexへの確認依頼**:
  1. **プロンプトへ足した規則の棚卸し**。視覚・ゲーム・画面共有の修正で追加した指示文のうち、コードで検証できるものはないか（`_MODE_GUIDE` / `companion.py` / `vision/service.py` / `minecraft_knowledge.py`）
  2. **周期処理の定常状態**。1回の遷移ではなくN回繰り返して落ち着くか（OBS解析ループ、相棒発話のクールダウン）
  3. **サーバ／デバイスがないと実行されないコード**に手を入れていないか
  4. **`llm.probe_on_start` は false のまま**にすること（true だと起動ごとに40秒以上GPUを占有）
- **失敗の型（6つ）**:
  1. **実行して確かめられる形にせずに書いた（5回）**: デバイス監視の既定値でマイクとTTSを殺した／区間`WAIT`で90秒に20回の空回り／`note_suppressed_user_turn`がそれを誘発／プローブの設定キーを隣の正しい実装を見ずに推測／`keep_alive`を**サーバなしでは実行されないコード**へ書いて**全応答を停止**
  2. **症状に合う修正で根本を外した**: 「物語の破綻」に話題ガードを当てたが、真因は`conversation_id`の変化だった
  3. **計測が嘘をついた**: num_ctx掃引を平均し、掃引自身が起こした再ロード7秒と定常値600msを混ぜて無意味な数字を判定根拠にした
  4. **分割の設計ミス**: 「接続／生成」の分離はサーバの応答特性で機能しなかった
  5. **手順違反**: セッション前半、`AGENTS.md` を読まずに作業した
  6. **規則を足し続けた**: D-016で自ら戒めた誘惑に、その後も従い続けた
- 遅延調査の結論: `初トークン ≈ 1106ms + 0.244ms × 未キャッシュtok`。**600msはOllama内部の固定処理で設定では動かせない**。否定した仮説は6つ（リクエスト競合／プロンプト評価が固定費／options／VRAM／KVキャッシュ／常駐切れ）
- 残る手: (a) `Mind.build_context` の5000tokブロック削減 = **プロンプト側の唯一の手段**、(b) personaの規則整理（ユーザー判断待ち。**創作物なので勝手に削らない**）、(c) クライアント側300ms、(d) 推論サーバの変更
- Tests: 914 passed / 3 skipped / 11 subtests（期間中 664 → 914、新規テストファイル10件）
- Handoff: `docs/handoffs/2026-07-28_0600_claude_session-retrospective.md`
- Related ADR / decision: D-016、D-017、D-025、R-037、憲法第2条・第5条・第7条・第13条・第16条・第17条・第20条

## 2026-07-28 05:30 JST — Claude — 遅延調査の完了とプローブの停止

- Status: completed（実機で応答復旧を確認済み。keep_alive効果の確認は次回）
- Summary: 05:00の障害修正後、実機で応答が正常に戻ったことを確認。遅延調査を完了として閉じ、**プローブを既定オフ**にした。掃引はnum_ctxを変えるたびにモデル再ロード（実測7〜16秒）を起こし、起動のたびに40秒以上GPUを占有して最初の会話を遅らせるため、調査が終わった今は害の方が大きい。
- **調査の結論**: 3.5秒の内訳は「600ms Ollamaの固定処理 / 300ms 我々のクライアント側 / 1350ms プロンプト評価 / 1000ms STT・文脈・話者・TTS」。潰した仮説は、我々のリクエスト同士の競合・プロンプト評価が固定費の正体・options指定・VRAM不足・KVキャッシュ・常駐切れの6つ。**残る600msはOllama内部のリクエストごとの固定処理で、設定では動かせない**。次に触るならサーバの版か推論サーバ自体の選定であり、コードの問題ではない。
- Changed:
  - `config/config.yaml`: `llm.probe_on_start` を **false** へ。再測定が必要な時（Ollama更新・GPU構成変更）だけ true にする旨と、判明した内容をコメントへ残した
- Behavior impact: 起動時のGPU占有40秒以上が解消する。会話の挙動は不変
- Config / DB migration: なし。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 914 passed / 3 skipped / 11 subtests
- Remaining（テンポを詰める手は3つ）:
  - (a) `Mind.build_context` の5000tokブロックを小さくする → プロンプト評価1350msが減る。**プロンプト側に残る唯一の手段**
  - (b) 我々のクライアント側300msの調査
  - (c) 推論サーバの変更（llama.cpp直 / vLLM）
  - 実機確認: 5分以上黙ったあとの1発目が速くなっているか（`keep_alive` 修正の効果）
- Handoff: `docs/handoffs/2026-07-28_0530_claude_latency-investigation-closed.md`
- Related ADR / decision: D-017、R-037、憲法第16条・第20条

## 2026-07-28 05:00 JST — Claude — 【障害】keep_alive追加で全応答が停止した件の修正

- Status: completed（自動回帰済み、実機確認が必要）
- Summary: 04:30に追加した `keep_alive` 送信で **`'OpenAICompatBackend' object has no attribute '_keep_alive'`** となり、**すべての応答が失敗する状態**を作った。値は `_multimodal_keep_alive` にしか保持されておらず、存在しない属性名を書いていた。リクエスト組み立てがストリーミング用コルーチンの内部にあり、**サーバなしでは一度も実行されない**ため、テストでも起動時でも検出できなかった。
- Changed:
  - `neuro_voice/llm/openai_compat.py`: `self._keep_alive` を明示的に保持（従来の `_multimodal_keep_alive` も残す）。リクエスト組み立てを `request_extra_body()` として**コルーチンの外へ抽出**し、サーバなしで検証できるようにした
  - `tests/test_llm_request_body.py`（新規15件）: 組み立てが例外を出さないこと、読む属性がすべて存在すること、keep_alive/options/reasoning_effort の各条件、`send_ollama_options: false` で **keep_alive まで消えないこと**
- Behavior impact: 04:30の障害を解消。`keep_alive: 10m` は意図どおり送信される
- Config / DB migration: なし。**GUI再起動が必要**
- Tests: 全体 914 passed / 3 skipped / 11 subtests。**属性名を1文字変えると新規テストのうち8件が失敗する**ことを確認済み
- 反省: ホットパスを、実行されないコードのまま変更した。テストのない箇所へ手を入れる時は、まず実行できる形へ切り出すこと。R-037に追記
- Handoff: `docs/handoffs/2026-07-28_0200_claude_fixed-latency-probe.md`
- Related ADR / decision: R-037

## 2026-07-28 04:30 JST — Claude — KVキャッシュも無罪。keep_aliveが送信されていない不具合を修正

- Status: completed（原因の切り分け完了、実機再測定は必要）
- Summary: num_ctx掃引の実機結果。**num_ctxを変えた直後だけ6.6〜7.9秒**（モデルの再ロード）、同じ値の2回目は約600ms。定常状態は `1024→602ms / 4096→621ms / 8192→633ms` で、**文脈長を8倍にしても31msしか増えない**。KVキャッシュは無罪。VRAM不足も無罪（全部GPU）。optionsも無罪。残る600msはOllamaのリクエストごとの固定処理で、我々が設定で動かせる範囲にはない。**あわせて確定した不具合**: `keep_alive` が一度も送信されていなかった。`extra_body` には `reasoning_effort` と `options` しか入っておらず、設定の `10m` は届かずOllama既定の5分でモデルが落ちていた。プローブで `keep_alive: 30m` を明示指定したところ期限が23:39→00:05へ動いたので、リクエストに入れれば効くことは実証済み。
- Changed:
  - `neuro_voice/llm/openai_compat.py`: **`keep_alive` を実際に送信する**（Ollamaのみ）。5分の無言後の初回が丸ごと再ロード待ち（実測6.6〜7.9秒）になっていた問題が解消する見込み
  - `neuro_voice/llm/ollama_probe.py`: 集計を**平均から最小へ**修正。掃引が自分で起こす再ロードを平均に混ぜて `3624/4088/3767ms` という無意味な数字を出していた。再ロード費用は別行で報告し、判定は設定値(=最大)での定常値を使う。掃引後は設定値へ戻してから終了し、次の実会話に再ロードを負わせない
  - `tests/test_ollama_probe.py`: 再ロードの除外、定常値の取り方、再ロード費用の別報告を追加（計26件）
- Behavior impact:
  - 会話が5分以上途切れてもモデルが常駐し続ける（`llm.keep_alive: 10m`）。**長い沈黙のあとの1発目が約7秒速くなる見込み**
  - 通常のターンの600msは変わらない
- Config / DB migration: なし。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 914 passed / 3 skipped / 11 subtests。`compileall`成功
- Remaining:
  - 残る約600msはOllama側のリクエストごとの固定処理。**設定では動かせない**ため、サーバの版・`OLLAMA_NUM_PARALLEL` 等の確認か、推論サーバ自体の変更という判断になる
  - 我々のクライアント側の約300msは未調査
  - プロンプト側の残り手段は `Mind.build_context` の5000tokブロックのみ
- Handoff: `docs/handoffs/2026-07-28_0200_claude_fixed-latency-probe.md`（同一調査の続き）
- Related ADR / decision: D-017、R-037、憲法第16条・第20条

## 2026-07-28 03:50 JST — Claude — VRAMは無罪。num_ctx掃引で「モデル準備」の中身を特定する

- Status: completed（測定の第4段。実機測定はこれから）
- Summary: 実機結果は **`合計7599MB / VRAM7599MB — 全部GPU`**。VRAM不足という予想は外れ、モデルは完全にGPU上にあるのに毎リクエスト約620msの `load_duration` がかかっている（0.2秒間隔の連続リクエストでも同じ）。あわせて**`keep_alive` が効いていないこと**が判明した: 測定23:23:08に対し期限は23:27:50で、設定の `10m` ではなくOllama既定の5分。つまり5分無言だとモデルが本当に落ちる。
- Changed:
  - `neuro_voice/llm/ollama_probe.py`: **num_ctx を 1024 / 4096 / 設定値 で掃引**し、`load_duration` が文脈長に比例するかを見る。比例すればKVキャッシュの確保、横ばいならランナーかスケジューラ側と判別できる。冷えた初回は捨てる。`keep_alive: 30m` を明示指定した呼び出しを1本入れ、計測前後の `/api/ps` の期限を比較して**keep_aliveが届いているか**を判定する。options無しの対照も1本
  - `tests/test_ollama_probe.py`: options差の判定を**削除**（実測で無罪のため）。掃引・keep_alive・冷えた初回の除外を追加（計24件）
- Behavior impact: なし（計測のみ）
- Config / DB migration: なし。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 912 passed / 3 skipped / 11 subtests。`compileall`成功
- Remaining:
  - 実機で `num_ctx と準備時間` の行を見る。比例していれば `num_ctx` を下げるか `OLLAMA_KV_CACHE_TYPE=q8_0`
  - `keep_alive` が届いていない件は独立した不具合。5分の無言後の初回応答が丸ごとロード待ちになる
  - 我々のクライアント側の約300msは未調査
- Handoff: `docs/handoffs/2026-07-28_0200_claude_fixed-latency-probe.md`（同一調査の続き）
- Related ADR / decision: 憲法第16条、D-017、R-037

## 2026-07-28 03:10 JST — Claude — 固定費の正体が判明: 毎リクエスト590msの「モデル準備」

- Status: completed（原因の半分を特定。VRAM収まりの確認待ち）
- Summary: プローブの実機結果。**`load_duration` が毎リクエスト約590ms**、モデルは常駐（`/api/ps` に7599MB）、`keep_alive=10m`、0.2秒間隔の連続リクエストでも変わらない。そして**`options` は無罪**（883 vs 822 / 802 vs 804 — 誤差）。推測で `send_ollama_options: false` にしていたら、効かないものを「効いた」と誤認するところだった。固定費1106msの内訳は「590ms Ollamaのモデル準備 / 約70ms プロンプト評価 / 約140ms 生成 / 約300ms 我々のクライアント側」。
- Changed:
  - `neuro_voice/llm/ollama_probe.py`: `/api/ps` から `size` と `size_vram` の**両方**を読み、モデルがVRAMに収まりきっているかを判定へ入れる。収まっていなければリクエストごとに用意し直されるため、`load_duration` が毎回かかることの説明になる。`expires_at` も表示
  - `tests/test_ollama_probe.py`: +2件（計20件）
- Behavior impact: なし（計測のみ）
- Config / DB migration: なし。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 908 passed / 3 skipped / 11 subtests。`compileall`成功
- Remaining:
  - 実機で `合計 / VRAM` を読む。**溢れていれば**GPUを分け合っているもの（Whisper large-v3-turbo / Style-Bert-VITS2 / 映像認識）を減らすか、より小さい量子化にする。**収まっていれば**別の原因（他モデルの追い出し、Ollama側の設定）
  - 我々のクライアント側の約300msは未調査
  - プロンプト側の残り手段は `Mind.build_context` の5000tokブロックのみ
- Handoff: `docs/handoffs/2026-07-28_0200_claude_fixed-latency-probe.md`（同一調査の続き）
- Related ADR / decision: 憲法第16条（GPU競合）、D-017、R-037

## 2026-07-28 02:40 JST — Claude — プローブが動かなかった件の修正（設定キーの読み違い）

- Status: completed（自動回帰済み、実機測定はこれから）
- Summary: 02:00に入れた遅延プローブが実機で1行も出なかった。原因は設定キーの読み違いで、`llm.ollama.base_url` を読んでいたが実際の構造は **`llm.backends.ollama.base_url`**。`factory.py` は最初から `cfg.section("llm.backends")` と正しく読んでおり、**すでに動いている実装がすぐ隣にあったのに確認せずキー名を推測で書いた**。加えて値が読めない時に黙って `return` していたため、失敗したことすら分からなかった。
- Changed:
  - `neuro_voice/pipeline.py`: `cfg.section("llm.backends")["ollama"]` から読むよう修正。省略・失敗した場合はINFO/WARNINGで理由を出す。ログへ `keep_alive` の値も併記（モデル常駐の判断材料）
- Behavior impact: なし（計測のみ）
- Config / DB migration: なし。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 906 passed / 3 skipped / 11 subtests。実configを読み込んで `base_url=http://localhost:11434/v1` / `model=gemma4:12b-it-qat` / `keep_alive=10m` が取れることを確認
- Remaining: 実機で `LLM遅延の切り分け` を見る。**判定が出るまで `send_ollama_options` や `OLLAMA_KEEP_ALIVE` を変更しない**
- Handoff: `docs/handoffs/2026-07-28_0200_claude_fixed-latency-probe.md`（失敗した試行として追記）
- Related ADR / decision: R-037

## 2026-07-28 02:00 JST — Claude — 初トークンの固定費(約1106ms)をサーバ自身の数字で切り分ける

- Status: completed（計測のみ。実機測定はこれから）
- Summary: `初トークン ≈ 1106ms + 0.244ms × 未キャッシュtok` の**切片**を説明するための計測。傾き（プロンプト評価）は妥当だが、未キャッシュ464tok（評価≒113ms）でも1138msかかり、`同時実行=1` `待ち=0ms` なので我々のリクエスト同士の競合でもプロンプト評価でもない。00:20に入れた「接続／生成」の分割は `生成=0ms` となり**設計ミスで分離できなかった**（サーバは最初のトークンができるまで応答を返さない）。そこでOpenAI互換エンドポイントが捨てている `load_duration` / `prompt_eval_duration` / `eval_duration` を、Ollamaのネイティブ `/api/chat` から直接読む。
- Changed:
  - `neuro_voice/llm/ollama_probe.py`（新規）: 極小のリクエスト（「こんにちは」）を **options付き／なしで2回ずつ**投げ、サーバ自身の内訳と `/api/ps` の常駐モデルを読む。「モデルのロード」「optionsによる再構成」「スケジューラ待ち」を1回で判別し、判定文まで出す
  - `neuro_voice/pipeline.py`: `_probe_llm_latency()` を起動3秒後に1回。会話経路の外
  - `config/config.yaml`: `llm.probe_on_start` / `llm.probe_delay_s`
  - `tests/test_ollama_probe.py`（新規18件）
- Behavior impact: **会話には影響なし。** 起動時にリクエストが1組増え、ログとステータスへ判定が出るだけ
- Config / DB migration: なし（新規キー2つ）。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 906 passed / 3 skipped / 11 subtests。コールドな1回目を判定から除外すること、サーバ停止時に例外を投げないことも固定。`compileall`成功
- Remaining:
  - **判定が出るまで `send_ollama_options` や `OLLAMA_KEEP_ALIVE` を変更しないこと。** 同時に複数変えると切り分けにならない
  - ロードが原因だった場合、`gemma4:12b-it-qat` は会話と視覚で共用のため、視覚・埋め込みによる追い出しを `/api/ps` で確認する
  - プロンプト側の残り手段は `Mind.build_context` の5000tokブロックのみ（01:10の記録参照）
- Handoff: `docs/handoffs/2026-07-28_0200_claude_fixed-latency-probe.md`
- Related ADR / decision: D-017、R-037、憲法第16条・第20条

## 2026-07-28 01:10 JST — Claude — 切り詰めslackの撤回と、同じ返答の繰り返しの停止

- Status: completed（自動回帰済み、実機受入は必要）
- Summary: (1) 00:20に入れた `trim_slack_ratio` は**前提から誤りだった**ので既定を0へ戻した。実測では除外件数が 11→13→15→17→19→21 と毎ターン増え続け、再利用率は28〜32%のまま。原因は「systemブロックだけで5000〜6000tokあり、プロンプトが一度も予算(5875tok)に収まらない」こと。収まるターンが存在しないため余裕を持たせても毎ターン切り直しになり、予算が下がったぶん履歴を余計に捨てるだけだった。**プロンプト側に残る手は、あの5000tokのブロックを小さくすることだけ**である。(2) 別件の不具合として、「マジでそう。」のように新しい内容のないターンで、直前の自分の返答を一字一句繰り返す現象が3回連続で観測された。語彙リストで個別に潰すのではなく、出来上がった文を比較して止める。
- Changed:
  - `config/config.yaml`: `llm.trim_slack_ratio` を 0.15 → **0.0**（仕組みは残す。プロンプトが収まるようになれば意味を持つ）
  - `neuro_voice/dialogue/echo.py`（新規）: `containment` / `is_echo` / `sentences` / `trim_echoed_opening` / `ReplyEchoGuard`。区間の言い直しと返答の繰り返しは同じ現象なので判定を1箇所へ集約
  - `neuro_voice/dialogue/directive.py`: 上記をechoモジュールから再エクスポート（既存の呼び出しは不変）
  - `neuro_voice/pipeline.py`: 返答の**冒頭1文だけ**を保留し、直前の返答の繰り返しなら画面にも音声にも出さずに打ち切る。全文を溜めると生成時間がそのまま遅延になるため、1文で判定する。音声側は元から文単位で待つので追加の遅延はない
  - `config/config.yaml`: `conversation.suppress_repeated_reply: true`
  - `tests/test_reply_echo.py`（新規17件）
- Behavior impact:
  - 直前の返答をなぞった場合、発話せずターンを閉じる（`reply_suppressed` を発行）
  - 話題語が同じだけの返答、短い相槌、言い換えを含む新しい返答は通す
  - 表示は冒頭1文ぶん遅れる。音声の開始タイミングは不変
  - `trim_slack_ratio: 0.0` により、履歴の保持量は00:20以前へ戻る
- Config / DB migration: なし。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 888 passed / 3 skipped / 11 subtests。トークンの区切り方に判定が依存しないことを4通りで固定。`compileall`成功
- Remaining:
  - **再利用率を上げる手はもう1つしかない**: `Mind.build_context` が返す5000tokのブロックを削るか、可変部と不変部へ分ける
  - `send_ollama_options: false` の試行（1106msの固定費の切り分け）は未実施
  - `合計 > num_ctx` のターンが依然ある
- Handoff: `docs/handoffs/2026-07-27_2330_claude_prompt-layout-and-latency-split.md`（同一調査の続き）
- Related ADR / decision: D-016、R-037、憲法第2条

## 2026-07-28 00:20 JST — Claude — 履歴の切り詰め位置を安定させる／固定費の容疑者を切替可能に

- Status: completed（自動回帰済み、実機再測定はこれから）
- Summary: 23:30の配置修正後の実機ログで2つ判明した。(1) **`LLM内訳: 接続=2450ms / 生成=0ms`** — サーバは最初のトークンができるまで応答を返さないため、この分割では待ち行列とプロンプト評価を切り分けられなかった。ただし**同時実行=1・待ち=0msが全ターン**で我々のリクエスト同士の競合は否定でき、**未キャッシュ464tok（評価≒113ms）でも1138ms**かかるためプロンプト評価でもない。残るのはOllama内部のリクエストごとの準備で、毎回送っている特殊なものは `options(num_ctx/num_predict)` だけ。(2) **配置修正は目的を達していない。** 再利用は30〜35%のままで、`最初に変わった所=assistant` — 今度は**会話履歴**が壊していた。毎ターン予算ぎりぎりで切り詰めるため、残る最古のメッセージが毎回変わる。
- Changed:
  - `neuro_voice/llm/context_budget.py`: `fit_messages(slack_ratio=)`。予算ちょうどではなく余裕を持って切ることで、数ターン切り直さずに済み先頭が保たれる。**入るかどうかの判定は実予算のまま**なので、収まっているプロンプトは切られない
  - `neuro_voice/pipeline.py`: `llm.trim_slack_ratio`（既定0.15）を渡す
  - `neuro_voice/llm/openai_compat.py` `factory.py`: `send_ollama_options` を追加。`llm.send_ollama_options: false` でリクエストごとの `options` を止められる。num_ctxは `OLLAMA_CONTEXT_LENGTH`（8192）で担保される
  - `config/config.yaml`: `llm.trim_slack_ratio` / `llm.send_ollama_options`
  - `tests/test_context_budget.py`: +6件（計31件）
- Behavior impact:
  - 履歴の切り詰めが数ターンに一度になり、その間は先頭が同一のまま。**保持する往復数は平均的に減る**（slack 0.15ぶん）
  - `send_ollama_options` は既定 true のまま。**まだ何も変えていない**——切り分けのための切替を用意しただけ
- Config / DB migration: なし（新規キー2つ）。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 871 passed / 3 skipped / 11 subtests。切り詰め後に次ターンが来ても再利用が保たれること／余裕なしだと毎ターン切り直しになることの両方を固定。`compileall`成功
- Remaining:
  - 実機で再利用率が30〜35%からどう動くか。**上がらなければ履歴以外にまだ犯人がいる**
  - `send_ollama_options: false` を試して接続時間が変わるか。変わらなければモデル常駐やOllama側の設定を疑う
  - 依然として `合計=10437tok > num_ctx=8192` のターンがある（systemを落とせない件、未対処）
- Handoff: `docs/handoffs/2026-07-27_2330_claude_prompt-layout-and-latency-split.md`（同一調査の続きとして追記）
- Related ADR / decision: D-017、R-037、憲法第16条・第20条

## 2026-07-27 23:30 JST — Claude — 初トークンの固定費の切り分けと、プロンプト配置の修正

- Status: completed（自動回帰済み、実機再測定はこれから）
- Summary: 40サンプルの実測から **`初トークン ≈ 1114ms + 0.241ms × 未キャッシュトークン数`** が得られた。プロンプト評価は実在するが、**約1114msはプロンプトと無関係な固定費**である（未キャッシュ442tok＝評価約100msのターンでも1121ms待っている）。「遅延の主因はプロンプト評価」という当初の説明は半分誤りだった。そこで (1) 固定費が「接続待ち」か「生成」かを切り分ける計測を追加し、(2) 22:10で判明した巨大ブロックの配置を修正した。
- Changed:
  - `neuro_voice/llm/openai_compat.py`: `_InFlight`（全バックエンド共有）と `LLM内訳: 接続=Xms / 生成=Yms / 同時実行=N / 待ち=Zms` ログ。接続が大きければ待ち行列かモデルのロード、生成が大きければプロンプト評価と切り分けられる
  - `neuro_voice/llm/prompt_layout.py`（新規）: `insert_before_user_turn` / `insert_after_stable_prefix` / `last_user_index` / `volatile_tokens`
  - `neuro_voice/realtime/context_assembler.py`: 4700〜5800tokのMind contextを index 1 → ユーザー発話の直前へ
  - `neuro_voice/pipeline.py`: 会話方針/音楽/ゲーム訂正/継続依頼/ASR/確信度の6箇所を同様に
  - `neuro_voice/discord_bridge/bot.py`: 同じ7箇所（第19条）
  - `tests/test_prompt_layout.py`（新規16件）
- Behavior impact:
  - **送るブロックの内容も個数も変えていない。位置だけ。** `total_tokens` が変わらないことをテストで固定している
  - 見込み: 未キャッシュ5600tok→900tok、初トークン2464ms→1331ms、合計3464ms→2331ms（**約1.1秒短縮**）
  - ただし1114msの固定費が残るため2.3秒止まりで、1秒には届かない。固定費の正体が判明するまでこれ以上は詰められない
  - 各挿入箇所のコメントは元から「ユーザー発話の直前に置く」と書かれており、実装だけがindex 1だった。今回コメントに実装を合わせた
- Config / DB migration: なし。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 865 passed / 3 skipped / 11 subtests。`messages[0], {"role": "system"` の残存0件。`compileall`成功
- Remaining:
  - **実機で `LLM内訳` を最初に見る。** `接続` が1秒近ければプロンプトをいくら削っても無駄で、モデル常駐・待ち行列・KVキャッシュ再確保を疑う
  - `options.num_ctx` を毎回送っている件は固定費の容疑者だが、**原因を確かめてから**外す（症状に合う修正を先に当てて根本を外す失敗を今日3回している。R-037）
  - `fit_messages` がsystemを落とせずnum_ctxを超える件は別課題（現状は警告のみ）
- Handoff: `docs/handoffs/2026-07-27_2330_claude_prompt-layout-and-latency-split.md`
- Related ADR / decision: D-017、R-037、憲法第16条・第20条・1.3

## 2026-07-27 22:10 JST — Claude — 計測結果と、巨大な単一ブロックの発見（送信内容は不変）

- Status: completed（計測の第2段。実機での再測定はこれから）
- Summary: 21:30に入れた計測の実機結果は **再利用可 28〜33%** で仮説どおりだった。さらに内訳から、原因がブロックの散らばりではなく**単一の巨大ブロック**であることが判明した。`system:CONVERSATION KERNEL` が **4700〜5800tok**（プロンプト全体の約60%）を占め、`context_assembler.py` がこれを **index 1（personaの直後）へ挿入**している。したがって再利用できるのは persona の2296tokだけで、観測値と正確に一致する。あわせて `合計=9766tok > num_ctx=8192` のターンを確認した。`fit_messages` はsystemメッセージを落とさない設計のため、この巨大ブロックが膨らむと予算を超えても切り詰められず、**サーバ側で先頭（＝persona）が切り捨てられている可能性がある**。
- Changed:
  - `neuro_voice/llm/prompt_metrics.py`: `split_sections()` / `section_report()` を追加。連結された1つのsystemメッセージを`【…】`見出しで分解し、**セクション単位でどれが変わったか**を測る。どの部分を前へ出せるかを決めるための材料
  - `neuro_voice/pipeline.py`: 詳細内訳のログと、`num_ctx`超過の警告
  - `tests/test_prompt_metrics.py`: +6件（計23件）
- Behavior impact: **なし。** 送信するプロンプトの内容・順序・量は依然として一切変更していない
- Config / DB migration: なし。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 849 passed / 3 skipped / 11 subtests。`compileall`成功
- Remaining:
  - 実機で `プロンプト内訳(詳細)` を見て、4700〜5800tokのうち何tokが毎ターン変わらないかを確定する
  - その結果に基づき、変わらないセクションをpersonaの直後へ、変わるセクションをユーザー発話の直前へ移す（**内容は削らない**）
  - `fit_messages` がsystemを落とせない件は別問題として対処が要る（現状はnum_ctx超過を検知して警告するのみ）
  - Discord側は未対応（計測は意味論ではないため片側のみ）
- Handoff: `docs/handoffs/2026-07-27_2130_claude_prompt-latency-measurement.md`（同一作業の続きとして追記）
- Related ADR / decision: 憲法第16条、第20条、1.3

## 2026-07-27 21:30 JST — Claude — 応答テンポの原因特定のための計測（送信内容は不変）

- Status: completed（計測のみ。実機での測定はこれから）
- Summary: 「ネウロ様と比べて会話のテンポとセンスが届いていない」という評価に対し、人格プロンプトを触る前に実機ログで遅延を測った。`LLM初回 p50=2540ms / p95=3144ms` で**合計3500msの80〜85%**を占める。プロンプトは毎ターン予算上限の約6900トークンで、うち約3200が常時ONの指示文（persona 1789 / planner 738 / realizer 608 / 継続依頼 916）。仮説は「KVキャッシュが一度も効いていない」——`pipeline.py`は毎ターン変わるブロックをindex 1（personaの直後）へ挿入しており、コメントには`Placed last so it sits closest to the user turn`とあるが実装は先頭。これが正しければ**内容を削らず順番を変えるだけ**で改善する。確かめるための計測を入れた。
- Changed:
  - `neuro_voice/llm/prompt_metrics.py`（新規）: `PromptProfiler`。プロンプトの内訳と、**前回から変わっていない先頭部分**のトークン数を測る
  - `neuro_voice/pipeline.py`: `_fit_prompt`直後に`_profile_prompt()`。`llm.profile_prompt: false`で無効化
  - `config/config.yaml`: `llm.profile_prompt: true`
  - `tests/test_prompt_metrics.py`（新規17件）
- Behavior impact: **なし。** 送信するプロンプトの内容・順序・量を一切変更していない。ログが2行増えるだけ
- Config / DB migration: `llm.profile_prompt`（新規）。DB変更なし。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 843 passed / 3 skipped / 11 subtests。`test_ordering_alone_changes_how_much_is_reusable` が中心で、同じ内容でも順番だけで再利用量が変わることを計測器側で固定。`compileall`成功
- Remaining:
  - **実機で `プロンプト内訳` と `LLM初回` を突き合わせる。** 再利用可が25%前後なら仮説どおりで並び替えへ進む。80%以上なら仮説は誤りでprefill以外を疑う
  - 並び替え時は`ConversationPlanner.prompt`/`SurfaceRealizer.prompt`の可変部と不変部を分ける必要がある（今は1つの文字列に混在）
  - 履歴の切り詰めを塊単位にし、毎ターン切り直さない
  - Discord側は未対応（計測は意味論ではないため片側のみ。並び替え段階では第19条どおり両方へ）
- Handoff: `docs/handoffs/2026-07-27_2130_claude_prompt-latency-measurement.md`
- Related ADR / decision: 憲法第16条（p50/p95の計測）、第20条、1.3（GPT Liveを遅延の参照目標とする）

## 2026-07-27 07:00 JST — Claude — 本当に迷っている時だけ言い淀む

- Status: completed（自動回帰済み、実機の聞こえ方は未確認）
- Summary: 「たまに人間らしく詰まる」演出ではなく、**実際に迷いがあるターンだけ**言い淀み・間・考えながらの話し方を許可する仕組みを追加した。確信しているのに悩むフリをするのは知識と能力の偽装であり憲法第1条の`INVARIANT`が禁じている。一方で同条は「覚えていないを自然に言えることは品質の一部」とも言っており、本当の迷いが声に出ることは求められている方向である。役割分担は、コードが「迷う理由があるか」を事実として確定し、LLMが「文のどこが迷いどころか」を決め、コードが「間と話速」を音にする（第2条）。
- Changed:
  - `neuro_voice/dialogue/hesitation.py`（新規）: `assess()`が聞き取りの確信度・参照解決・記憶ヒット数・検索判定という**既に他の目的で計算されている値**だけから不確かさを集める。判定用の追加LLM呼び出しはゼロ（D-017）。確信時は明示的に禁止文を出す（無言だと前ターンの許可が残るため）
  - `neuro_voice/tts/delivery.py`（新規）: 本文中の「……」を実際の無音へ（1つ=320ms、上限900ms）、言い淀み部分**だけ**を0.88倍速へ。「……」のない文は従来どおり1回の合成で処理する
  - `neuro_voice/mind/mind.py`: `hesitation_permission()`。直前の発話の仕草数から頻度を減衰させる
  - `neuro_voice/pipeline.py`、`neuro_voice/discord_bridge/bot.py`: 許可文のプロンプト挿入と、間を音にする合成をLocal/Discord同形で配線
  - `config/config.yaml`: `conversation.hesitation`節
  - `tests/test_hesitation.py`（23件）、`tests/test_delivery.py`（23件）
- Behavior impact:
  - 聞き取りが怪しい／指すものが特定できない／思い出せない／根拠がない／意見が定まらない、のいずれかが立ったターンでのみ仕草が出る。既定は1ターン1回、2回は複数の意味で迷った時だけ
  - 確信しているターンでは「言い淀みを入れない」と明示的に指示される
  - 内部タグを使わず`「……」`をキャリアにしたため、チャット表示と声が一致し、内部語がTTSへ漏れる経路が構造的に存在しない（第1条 1.4）
  - 許可文に「えっと」「うーん」などの例示を書いていない。例示は口癖になるため、書くのは状態の説明のみ
- Config / DB migration: `conversation.hesitation`（新規、`enabled: false`で完全に従来動作）。DB変更なし。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 826 passed / 3 skipped / 11 subtests。**確信時に仕草が出ないことを6件の独立したテストで固定**。フィラー判定が「ありがとう」「そうだね、それは面白い」を誤検出しないことも回帰に含む。`compileall`成功
- Remaining: 実機で (a) 出すぎていないか、(b) 聞き取りが怪しいターンで実際に出るか、(c) 間の長さ(320ms)と話速(0.88)が自然か、(d) チャット表示と声が一致しているか、(e) ローカル12Bが許可文に従って迷いどころへ置けるか
- Handoff: `docs/handoffs/2026-07-27_0700_claude_honest-hesitation.md`
- Related ADR / decision: D-016、D-017

## 2026-07-27 05:30 JST — Claude — ラジオの次区間が前区間の締めを言い直す問題

- Status: completed（自動回帰済み、実機受入は必要）
- Summary: `_MODE_GUIDE[MONOLOGUE]` の「前の区間の最後の考えから自然につながる」という指示と、同じプロンプトへ入る「直近で自分が話した区間」の本文が競合し、ローカル12Bが示された締めの一文から書き始めていた。実例の2文は「安全や」が落ちており**同一ではない**ため、文字列一致では検出できない。プロンプト同士の競合はプロンプトでは安定せず、かつ「直前の文をもう一度言っているか」は文字を比べれば判定できる機械的性質なので、コード側で確定させた（第2条）。
- Changed:
  - `neuro_voice/dialogue/directive.py`: `sentences()` と `trim_echoed_opening()` を追加。対称な類似度ではなく**包含率**（候補文のうち前区間に順序を保って現れる割合）で判定。閾値0.85、冒頭最大2文、8文字未満は対象外
  - `neuro_voice/dialogue/continuation.py`: 発話前に適用。全文がechoなら発話せず`waited`（reason=`echoed_previous_segment`）としてループ検出を進める。`segment_echo_trimmed`メトリクスとログ
  - `tests/test_segment_echo.py`（新規16件）
- Behavior impact:
  - 区間の冒頭が前区間の言い直しなら、その部分だけ落として残りを話す
  - 区間全体が言い直しなら何も話さず、次の区間へ回す
  - 同じ話題語を含むだけの文、短い相槌（「うん。」等）、新しい話題は落とさない
- Config / DB migration: なし。閾値はコード内定数。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 780 passed / 3 skipped / 11 subtests。**修正を戻すと2件が失敗することを確認済み**。実際に観測された文のペアをそのままテストへ使用。誤検出しない側のテストも追加。`compileall`成功
- Remaining: 実機で (a) 3分ラジオで同じ台詞が二度流れないか、(b) 話が細切れにならないか、(c) ログの`区間の冒頭が前区間の繰り返しだったため除去`の頻度、を確認する
- Handoff: `docs/handoffs/2026-07-27_0530_claude_segment-echo.md`
- Related ADR / decision: D-016、R-026

## 2026-07-27 04:00 JST — Claude — ターン制セッションの区間ポーリング暴走と、文脈切り詰めによる物語の消失

- Status: completed（自動回帰済み、実機受入は必要）
- Summary: 実機ログ（23:31〜23:37）で02:30の修正が効いていること（`Directive rekeyed`、`Directive created`が1回のみ）を確認したうえで、残る2件の原因を特定した。(1) ターン制Directiveでモデルが`WAIT`（＝相手の手番）を返してもステータスがACTIVEのままで、tickごとに再生成していた。90秒で約20回のLLM生成が走り、発話されたのは1件。直列なローカルLLMでこれがユーザーの発話の前に並び、「続きを話さなくなった」状態になっていた。(2) ターンを重ねるほど文脈切り詰めが増え（9→13→17→20件除外）、冒頭の場面設定が履歴から消える一方、systemのペルソナ指示は落ちないため、GMのナレーションではなく友達のコメントに寄っていた（R-027の実害化）。
- Changed:
  - `neuro_voice/dialogue/continuation.py`: `WAIT` かつ `needs_user_input` なら `WAITING_FOR_USER` へ遷移。`note_reply`と区間SPEAK時に`record_progress()`
  - `neuro_voice/dialogue/directive.py`: `opening_note` / `progress_notes` と `record_progress()` を追加。冒頭は永久保持、以降は直近6件の窓、重複は記録しない
  - `neuro_voice/dialogue/intent_plan.py`: `directive_prompt_block` へ「この依頼の始まり」「ここまでの流れ」を追加
  - `tests/test_turn_based_session.py`（新規13件）
- Behavior impact:
  - ターン制セッションで手番を渡したあと、tickごとの無駄なLLM生成が起きない。一人語り（ラジオ等）は従来どおり`WAIT`後も継続する
  - 長いセッションでも冒頭の設定と直近の流れがプロンプトへ残り、履歴が切り詰められても筋を失いにくい
  - 記録はモデルを使わない決定的処理（発話済みテキストを110文字で切る）。ターンごとの追加LLM呼び出しはゼロ（D-017）
- Config / DB migration: なし。新規設定キーなし。永続データへの影響なし。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 764 passed / 3 skipped / 11 subtests。**修正を戻すと新規テストのうち3件が失敗することを確認済み**。sounddeviceスタブ下でCodexの視覚系21件も成功。`compileall`成功
- Remaining: 実機で (a) 10ターン以上進めて冒頭設定が保たれるか、(b) 選択への回答後もナレーションが続くか、(c) 「うんうん」の後に5秒間隔のLLM生成がログに出ないこと、(d) ラジオが`WAIT`で止まらないこと
- Handoff: `docs/handoffs/2026-07-27_0400_claude_turn-based-session-polling-and-story-memory.md`
- Related ADR / decision: D-016、D-017、R-026、R-027

## 2026-07-27 02:30 JST — Claude — 相槌で読み上げが停止する問題と、話者確定によるDirectiveの孤児化

- Status: completed（自動回帰済み、実機受入は必要）
- Summary: 実機ログ（2026-07-26 23:07〜23:16）から2件の根本原因を特定した。(1) VAD検知時点（STT前、相槌か割り込みか不明）で合成済み音声を破棄していたため、相槌と判定されても復帰する音声が残っておらず、さらに相槌分岐がreply pathへ到達しないためDirectiveがPAUSEDのまま解除されなかった。(2) `conversation_id`が話者識別の完了前に決まるため、TRPGのNARRATION Directiveが`local:user`へ登録され、以降のターンは`speaker:1`を検索して見つけられず、別Directiveを新規作成していた。元のDirectiveは終端状態にならず、ログにも一切残らないまま到達不能になっていた。
- Changed:
  - `neuro_voice/dialogue/directive_runtime.py`: `pause_for_human(discard_audio=)`。`False`なら生成停止のみで音声は保持。`resume_after_backchannel()`を追加
  - `neuro_voice/dialogue/directive.py`: `DirectiveStore.rekey(old, new)`。`put()`による置換をログへ出力
  - `neuro_voice/dialogue/continuation.py`: `rekey()`と`directive_rekeys`メトリクス
  - `neuro_voice/mind/mind.py`: `_follow_speaker_key()`を`identify_speaker`/`set_speaker_hint`から呼び、`merge_speakers`でもrekey
  - `neuro_voice/pipeline.py`、`neuro_voice/discord_bridge/bot.py`: VAD開始は非破壊、割り込み確定時に破棄、相槌／誤検知／barge-in無効時にDirectiveを再開。Local/Discord同形
  - `tests/test_backchannel_and_rekey.py`（新規15件）
- Behavior impact:
  - 相槌（「うん」「うんうん」）で読み上げが止まらず、その位置から続く。継続依頼も止まらない
  - 実質的な割り込みでは従来どおり即座に停止し、古い区間の音声を破棄する
  - TRPGなどの継続セッションが、話者識別の完了・話者統合をまたいで1つのまま続く。`Directive rekeyed` がログへ出る
  - Directiveが別のDirectiveに置き換えられた場合、`Directive replaced` としてログに残る（従来は無言）
- Config / DB migration: なし。新規設定キーなし。**GUI再起動が必要**
- Tests: `pytest tests/ -q` → 751 passed / 3 skipped / 11 subtests。**修正を戻すと新規テストのうち4件が失敗することを確認済み**。sounddeviceスタブ下でCodexの視覚系・音声系21件も成功。`compileall`成功
- Remaining: 実機で (a) ラジオ中の相槌で途切れないか、(b) 実質的割り込みで即停止するか、(c) TRPG開始→設定確認→再開で`Directive created`が1回だけか、(d) Discordで同3点、を確認する
- Handoff: `docs/handoffs/2026-07-27_0230_claude_backchannel-and-directive-rekey.md`
- Related ADR / decision: D-014、D-015、R-026、R-030

## 2026-07-27 01:30 JST — Claude — マイク／スピーカーの手動再認識

- Status: completed（自動回帰済み、GUI実機受入は必要）
- Summary: 既存の自動復旧は「一度開いたストリームが停止した場合」しか救えず、起動時に無かったヘッドセットを後から繋いだ場合と、Windowsが同じ機器を別エンドポイントへ移して古いハンドルが`active`を返し続ける場合は、アプリ再起動が唯一の対処だった。設定画面へ明示的な再認識ボタンを追加した。
- Changed:
  - `neuro_voice/audio/capture.py`: `_open()`を分離。`start(allow_fallback=False)`/`restart(allow_fallback=False)`と`opened_device`/`used_fallback`を追加
  - `neuro_voice/audio/devices.py`: 表示用の`device_label()`（`#hostapi`サフィックスを外す。比較値は不変）
  - `neuro_voice/pipeline.py`: `redetect_audio_devices()`。再生reopen → デバイス一覧の再構築 → スナップショット → マイク再open → 結果報告
  - `neuro_voice/ui/webview_app.py`: 同名のUI APIを公開（pywebviewスレッド→イベントループ、20秒timeout）
  - `neuro_voice/ui/assets/index.html`: 設定「通話の聞き取り方法」欄へ「🎤 マイクを再認識」ボタンと結果表示
  - `tests/test_audio_redetect.py`（新規9件）
- Behavior impact:
  - ボタン押下でデバイス一覧を作り直し、マイクとスピーカーを開き直す。**再起動不要**
  - 設定した指定マイクを必ず先に試し、見つからない場合のみ既定デバイスで開いて「設定したマイクが見つからないため既定デバイスで開きました」と明示する
  - **自動監視は既定デバイスへ落ちない。** `allow_fallback`の既定は`False`で、`_device_watch_loop`からは渡していない。黙って別のマイクへ切り替わることはない
  - `_device_watch_loop`の`refresh=False`方針（2026-07-26 17:40 Codex）は変更していない。プロセス全体の再初期化は明示操作時のみ
- Config / DB migration: なし。新規設定キーなし
- Tests: `pytest tests/ -q` → 736 passed / 3 skipped / 11 subtests。`sounddevice`スタブ下で`test_local_audio_liveness.py`と`test_audio_devices.py`を実行し、`start()`/`restart()`の引数追加が既存呼び出しを壊していないことを確認（32件成功）。`compileall`成功
- Remaining: GUI実機で、(a) ヘッドセット未接続で起動→接続→ボタン、(b) 存在しないマイクを設定した状態での表示、(c) 会話中に押した場合の再生復帰、(d) 押下後のデバイス選択リスト更新、を確認する
- Handoff: `docs/handoffs/2026-07-27_0130_claude_microphone-redetect.md`
- Related ADR / decision: D-021、R-032

## 2026-07-27 00:30 JST — Claude — 沈黙したターンの手番喪失と、話者ヒステリシスの確定一致上書き

- Status: completed（自動回帰済み、実機受入は必要）
- Summary: Codexの2026-07-26変更を監査し、2件を修正した。(1) Turn Closureが沈黙を選んだターンはLocal/Discordとも早期returnし、Directive層へ届いていなかった。ターン制セッション（GM）が手番を返した直後に「うん」と答えると、`WAITING_FOR_USER`を解除する経路が存在せず物語が無言で停止していた。(2) 話者ヒステリシスが確定閾値以上の一致すら直前話者へ引き戻し、かつ「直前に話した人」の判定がwall clockの解像度に依存していたため、Windowsでは新規登録直後の本人の発話が別人へ吸収されていた。後者は`test_speaker_merge.py::test_the_longer_history_is_offered_as_the_target`がCodex環境でのみ失敗し5エントリ連続で持ち越されていた事象の実体である。
- Changed:
  - `neuro_voice/dialogue/directive_runtime.py`: `note_suppressed_user_turn()`を追加。手番を返し、停止語だけは既存の一括取消へ委譲する。Directive作成・計画改訂は行わない
  - `neuro_voice/dialogue/state.py`: `ConversationState.directive_waiting_for_user`を追加し、metadataから更新・snapshotへ公開
  - `neuro_voice/dialogue/turn_closure.py`: 手番待ちのDirectiveがある間は`CONTINUE`（reason=`directive_awaiting_user_move`）
  - `neuro_voice/mind/mind.py`: `directive_waiting_for_user(source)`を追加（判断はMindが所有、D-015）
  - `neuro_voice/pipeline.py`、`neuro_voice/discord_bridge/bot.py`: フラグ供給と、応答見送り経路からの手番通知をLocal/Discord同形で配線
  - `neuro_voice/mind/speakers.py`: ヒステリシスへ`best_score < threshold`の上限を追加。`_seen_order`単調カウンタで「直前に話した人」の同着を解消。`merge_candidates`の同着を`turns → n → id`で決定的に。`merge`/`forget`で順序を整理
  - `tests/test_silent_turn_directive.py`（新規15件）、`tests/test_speaker_stability.py`（+3件）、`tests/test_speaker_merge.py`（対象テストを書き直し+1件）
- Behavior impact:
  - GMの問いに「うん」とだけ答えても物語が進む。ラジオ中の相槌は従来どおり沈黙のままだが、番組は止まらない
  - 相槌のような短い停止語（「もういいよ」）でも継続依頼は停止する
  - Windows上で、新規登録直後の話者が直前話者の名前で呼ばれ続ける現象が解消する
  - Turn Closureの語彙は一切変更していない。R-030の誤沈黙の範囲は不変
- Config / DB migration: なし。新規設定キーなし。`data/speakers_*.json`の形式も不変
- Tests: `pytest tests/ -q` → 726 passed / 3 skipped / 11 subtests（sandboxに`sounddevice`・GPU・GUIがないため4ファイル除外）。**修正を戻すと新規テストのうち9件が失敗することを確認済み**。`compileall`成功
- Remaining:
  - 実機受入: TRPGで「うん」だけの返答で進行するか、ラジオ中の相槌で止まらないか、Windowsで話者名が入れ替わらないか、Discordで同3点
  - 除外した4テストファイルは実機で実行すること
  - R-030（Turn Closureの誤沈黙）自体は未解決。実会話replay corpusでの計測が必要
- Handoff: `docs/handoffs/2026-07-27_0030_claude_silent-turn-and-speaker-hysteresis.md`
- Related ADR / decision: D-015、D-016、D-019、R-021、R-030

## 2026-07-26 22:09 JST — Codex — 「画面見てる？」の古いUIプレビュー再表示を防止

- Status: completed（自動回帰済み、OBS実機再起動確認待ち）
- Summary: 22:01の実ログでは「今、画面見てる？」に対するfresh OBS captureが一度も起きず、通常会話のmessage buildが以前の`latest_frame`をUIへ再送して「いま見た画面」と表示していた。質問分類だけでなく、画像そのものを現在turnへ結び付けるrequest ID境界を追加した。
- Changed:
  - `neuro_voice/vision/service.py`: 「画面見てる/見えてる」を明示current-screen質問へ追加
  - `neuro_voice/vision/video.py`: 明示取得前に旧frameを消去し、成功frameへ`explicit_request_id`と要求時刻を付与
  - `neuro_voice/vision/discord_share.py`: 現在response IDと一致するframeだけをUIへ通知
  - `neuro_voice/pipeline.py`、`neuro_voice/discord_bridge/bot.py`: 通常会話context構築時のqueued/latest frame再送を廃止
  - `tests/test_discord_screen_share.py`、`tests/test_realtime_vision.py`: 語句分類、request ID一致/不一致、取得失敗、通常turn再送禁止の回帰
- Behavior impact: 「画面見てる？」は質問時にfresh captureを行う。取得に失敗した場合や別turnの画像しかない場合、古い画像を「いま見た画面」として表示しない。初回session previewは従来どおり表示する。
- Config / DB migration: なし。
- Tests: 関連33件成功。全体740件成功 / 既知の未変更話者統合テスト1件失敗（`test_speaker_merge.py::test_the_longer_history_is_offered_as_the_target`）。
- Remaining: GUIを再起動し、OBS映像を大きく変えた直後に「今、画面見てる？」を試す。UI画像とログの`Fresh obs capture`が同じturnで発生すること、OBS取得失敗時に旧画像が出ないことを確認する。
- Handoff: `docs/handoffs/2026-07-26_2209_codex_request-bound-vision-preview.md`
- Related ADR / decision: D-024、R-020、R-034

## 2026-07-26 21:41 JST — Codex — 画面判断をナレーション経路から分離しSTT hotword混入を修正

- Status: completed（自動回帰済み、OBS/実マイク受入は必要）
- Summary: 「今の持ち物で焚き火を作れる？」が最新画面のsummaryを直接読む高速経路へ入り、質問に答えず`inventory_open: True`まで発話していた。またWhisperへペルソナ名・登録話者全員・終了済みActivityをhotwordとして渡し、その一覧が正しい発話末尾へ混入していた。画面説明と画面根拠の判断を分離し、後者は最新frame・公式Minecraft DB・通常会話LLMを統合する。STT biasは現在話者とactive activityだけへ限定し、3個以上のhotwordからなる末尾混入を保守的に除去する。
- Changed:
  - `neuro_voice/vision/discord_share.py`: direct scene/OCR質問をwhitelist化、持ち物/攻略/次の一手をfresh vision後の通常推論へ戻し、同一turn二重解析を防止、内部player-state keyを発話用日本語へ変換
  - `neuro_voice/pipeline.py`、`neuro_voice/discord_bridge/bot.py`: 視覚根拠が必要なrecipe/affordance質問をverified短文で先取りせず、Local/Discord同一経路へ統一
  - `neuro_voice/mind/mind.py`、`neuro_voice/stt/transcript_repair.py`: 全登録話者/終了済みActivityのdecode biasを廃止し、複数context hotwordの末尾echoだけを除去
  - `neuro_voice/games/minecraft_knowledge.py`、`neuro_voice/vision/service.py`、`neuro_voice/games/companion.py`: 料理表現の分類追加、inventory item/count抽出要求、情景言い換えだけのゲーム相棒発話を禁止
  - `config/config.yaml`: final beam 5、固定hotwords廃止、bias上限4、Minecraft JSON 220 tokens、視覚推論refresh 6.5秒
  - `tests/test_discord_screen_share.py`、`tests/test_transcript_repair.py`、`tests/test_stt_context_bias.py`: routing、二重解析防止、schema非発話、hotword echo、active-only biasを回帰
- Behavior impact: 「何が見える？」は最新frameから短く直接回答するが、「今の持ち物で作れる？」「次はどうする？」はscene narrationを返さず、最新視覚状態と公式知識を材料に実際の問いへ答える。`inventory_open`等は読み上げない。Whisperが「ポッポ チビ ゲスト4 日本語しりとり」を末尾へ挿入するdecode条件を除去した。
- Config / DB migration: DB変更なし。設定値のみ変更。GUI/プロセス再起動後に有効。
- Tests: 関連72件成功。全体738 passed / 1 failed。失敗は既知・未変更の`tests/test_speaker_merge.py::test_the_longer_history_is_offered_as_the_target`のみ。
- Remaining: 実マイクで固有名詞精度/末尾混入、OBSで持ち物判別と助言内容、Local/Discordの応答時間を受入確認する。12B視覚modelが判別できない小さいitem icon/countは推測せず不明として残る。
- Handoff: `docs/handoffs/2026-07-26_2141_codex_visual-reasoning-stt-bias.md`
- Related ADR / decision: D-010、D-024、R-020、R-034

## 2026-07-26 21:14 JST — Codex — OBS視覚説明をイベント駆動ゲーム相棒へ接続

- Status: completed（自動回帰・構文検査済み、OBS実プレイ受入は必要）
- Summary: fresh視覚回答が毎回「今の質問で撮り直した画面だと」で始まり、OBS sessionのbackground観測が既存GameCompanionDirectorへ届いていなかった。質問時回答を状況・現在行動・次の一手で構成し、Minecraft OBSの意味ある変化だけをLocal/Discord双方の短い相棒反応へ接続した。
- Changed:
  - `neuro_voice/vision/discord_share.py`: 定型前置き廃止、助言優先のfresh回答、background observation callback
  - `neuro_voice/vision/service.py`: 現在行動と視覚根拠のある一手をstructured outputへ要求
  - `neuro_voice/games/companion.py`: eventへplayer state/助言候補を保持し、説明ではなく行動への反応を要求
  - `neuro_voice/pipeline.py`、`neuro_voice/discord_bridge/bot.py`: OBS Minecraft観測をLocal/Discordのゲーム相棒Runtimeへ接続
  - `config/config.yaml`: 通常actionも候補になるimportance/cooldown/probabilityへ調整
  - `tests/test_discord_screen_share.py`、`tests/test_game_companion.py`: 定型文除去、行動/助言、callback、prompt groundingを回帰
- Behavior impact: 「今何してる？」「次はどうすれば？」は画面説明だけでなく、現在行動と根拠のある次の一手を短く返す。プレイ中は危険・発見・明確な行動変化に時々反応し、静止場面、メニュー、同一イベント、会話中は発話を抑制する。
- Config / DB migration: DB変更なし。`vision.important_event_threshold: 0.55`、`proactive_reaction_cooldown_sec: 6`、`proactive_reaction_probability: 0.85`。
- Tests: 変更5 Python moduleの`py_compile`成功。画面共有・ゲーム相棒・interaction・realtime visionの174件成功。全体は731 passed / 1 failedで、未変更の`test_speaker_merge.py::test_the_longer_history_is_offered_as_the_target`のみ単独再実行でも既存期待と不一致。
- Remaining: GUI再起動後、OBS Minecraftで採掘・戦闘・発見・通常移動・静止を試し、変化時だけツッコミ/助言が入り、連発しないことをLocal/Discordで確認する。12B視覚解析自体の遅延は残る。
- Handoff: `docs/handoffs/2026-07-26_2114_codex_obs-game-companion.md`
- Related ADR / decision: D-024、R-034

## 2026-07-26 20:56 JST — Codex — Minecraft攻略質問を公式DB高速経路へ分離

- Status: completed（自動回帰・実DB回答検証済み、OBS実機遅延の再計測は必要）
- Summary: Minecraft映像認識中の「豚肉を焼くにはどうすれば」が、`どうすれば`だけでcurrent-screen質問と誤分類され、公式レシピDBより先に12B映像推論へ流れていた。安定した攻略知識と現在画面依存の判断を分離し、公式JARで確定できる調理法はLocal/Discordとも映像・会話LLMを待たず直接回答する。
- Changed:
  - `neuro_voice/games/minecraft_knowledge.py`: 攻略知識分類、材料側からの公式recipe検索、かまど/燻製器/焚き火の検証済み短文回答
  - `neuro_voice/games/__init__.py`: 共通知識分類を公開
  - `neuro_voice/vision/discord_share.py`: Minecraft攻略質問をfresh visionから除外し、明示画面質問と現在状況質問だけ質問時frameへ送る
  - `neuro_voice/pipeline.py`、`neuro_voice/discord_bridge/bot.py`: Local/Discord共通で公式調理回答を映像推論より前に実行し、Local画面共有中の一般recipe質問にも公式contextを保持
  - `config/config.yaml`: 公式調理直接回答を有効化し、Minecraft OBS解析幅を1024pxから768pxへ軽量化
  - `tests/test_minecraft_official_knowledge.py`、`tests/test_discord_screen_share.py`: 豚肉/一般的な肉の調理、誤映像routing、現在状況のfresh解析維持を回帰
- Behavior impact: 「豚肉の焼き方」「肉の焼き方」はactive Java 1.21.6公式game dataを根拠に、初回実測約292msで回答文を確定する。「次はどうすれば」「今どこ」は引き続き質問時の最新OBS frameを解析する。明示的に「画面を見て」と言った場合は視覚が優先される。
- Config / DB migration: schema変更なし。`game_assistant.minecraft.direct_verified_recipe_answer: true`追加。`discord.screen_share.minecraft_capture_max_width: 768`へ変更。
- Tests: 変更5 moduleの`py_compile`成功。関連7 test file全210件成功。実config + 公式DB（Java 1.21.6、1345 recipes）で回答内容と292ms初期取得を確認。最終config読込も成功。
- Remaining: GUI再起動後、実機で「豚肉を焼くには？」がvision requestを発生せず短時間で読み上がること、768pxでHUD/状況認識精度を保つこと、現在状況質問のp50/p95を再計測する。
- Handoff: `docs/handoffs/2026-07-26_2056_codex_minecraft-knowledge-fast-lane.md`
- Related ADR / decision: D-024、R-034

## 2026-07-26 20:36 JST — Codex — 「今」の画面質問を質問時フレームへ固定

- Status: completed（実ログ原因確認・自動回帰テスト済み、OBS実機再確認は必要）
- Summary: OBSは1fpsで更新されていたが、「今」の質問が質問前から13秒間解析中のold taskへjoinし、その結果を通常会話LLMでもう一度15秒かけて言い換えていた。明示的な現在画面質問は旧解析をcancelし、質問時に撮り直したframe世代へ固定して、structured vision結果から直接発話する。
- Changed:
  - `neuro_voice/vision/video.py`: `force_fresh`解析、質問時frame保持、解析frame/latest frameのsequence・age・距離ログ
  - `neuro_voice/vision/discord_share.py`: fresh current-screen direct answer、旧観測の使用禁止、OCR/状態/対処の短い整形
  - `neuro_voice/vision/service.py`: 「もう一回見て」「見直して」「前の画面」等を明示視覚turnへ追加
  - `neuro_voice/pipeline.py`、`neuro_voice/discord_bridge/bot.py`: Local/Discord共通でfresh視覚回答を通常LLMより先に実行
  - `config/config.yaml`: fresh直接回答18秒、Minecraft視覚出力160 tokens
  - `tests/test_discord_screen_share.py`、`tests/test_realtime_vision.py`: old task cancel、新frame固定、二重LLMなしの回帰
- Behavior impact: 「今どこ？」「もう一回見て」では、以前の景色を現在形で答えない。質問時のOBS frameをUIへ表示し、1回の視覚推論結果をそのまま短く読み上げる。18秒以内に解析できなければ古い状態を代用せず、timeoutを明示する。
- Config / DB migration: DB変更なし。`discord.screen_share.direct_answer_timeout_sec: 18.0`、`minecraft_max_output_tokens: 160`を追加。
- Tests: 変更5 Python moduleの`py_compile`成功。映像・画面共有・Conversation関連60件成功。実config loader成功。
- Remaining: GUI再起動後、移動前後で「今どこ？」「もう一回見て」を試し、UI previewと回答が同じ質問時frameであること、ログの`analyzed_seq`が質問前taskでないことを確認する。12B modelの単一視覚推論自体は実測約13秒であり、サブ秒映像理解には小型vision model等の別段階が必要。
- Handoff: `docs/handoffs/2026-07-26_2036_codex_fresh-visual-turn.md`
- Related ADR / decision: D-024、R-034

## 2026-07-26 20:15 JST — Codex — Discord共有とOBSゲーム映像の解像度・解析プロファイル・ソース切替

- Status: completed（自動回帰テスト済み、Discord/OBS実機再確認は必要）
- Summary: Discord共有映像を通常ゲーム用640px・Minecraft専用プロンプトで解析していたため、小さい共有領域の文字が潰れ、Granblue等もMinecraftとして誤認していた。Discord共有を汎用1280px解析へ分離し、「OBSのマイクラの画面を見て」で同じ継続視覚sessionをMinecraft OBS sourceへ切り替えられるようにした。
- Changed:
  - `neuro_voice/vision/discord_share.py`: source kind、汎用/ゲーム別プロファイル、解像度、OBS source切替、共有専用出力上限、直接回答context
  - `neuro_voice/vision/service.py`: 画面内文字の読取要求を明示視覚質問として扱う
  - `neuro_voice/pipeline.py`、`neuro_voice/discord_bridge/bot.py`: active視覚sessionの質問を宛先判定より優先し、UI eventへsource名を付与
  - `neuro_voice/vision/video.py`: OBS取得ログへsource名と実frame寸法を追加
  - `neuro_voice/ui/assets/index.html`: OBS previewを拡大し、Discord共有/マイクラと実source名を表示
  - `config/config.yaml`: Discord共有1280px、Minecraft 1024px、共有解析240 tokens
  - `tests/test_discord_screen_share.py`、`tests/test_realtime_vision.py`: source切替、profile/width分離、OCR質問、後方互換回帰
- Behavior impact: 「Discordの画面共有を見て」はOBS `Discord画面共有`を汎用解析し、「OBSのマイクラの画面を見て」は設定済みゲームsourceへ明示的に切り替わる。「画面の中の文字を読んで」は複数人VCでも見送られず、解析結果から直接回答する。
- Config / DB migration: DB変更なし。新規任意設定`discord.screen_share.max_output_tokens`、`capture_max_width`、`minecraft_source_name`、`minecraft_capture_max_width`。既定値を設定済み。
- Tests: 変更5 Python moduleの`py_compile`成功。画面共有・映像・Conversation関連57件成功。
- Remaining: GUI再起動後、Discord共有previewのラベル・文字読取・Granblue認識、続けて「OBSのマイクラの画面を見て」によるsource切替を実機確認する。
- Handoff: `docs/handoffs/2026-07-26_2015_codex_obs-visual-source-switch.md`
- Related ADR / decision: D-024、R-034

## 2026-07-26 19:51 JST — Codex — Discord画面共有の初回解析・プレビュー・宛先判定修正

- Status: completed（実ログ原因確認・自動回帰テスト済み、Discord/OBS実機再確認は必要）
- Summary: OBSフレームは毎秒取得できていたが、初回視覚解析13秒の途中で2.5秒の会話timeoutが解析をcancel/restartし、Discord UIには共有frameも出ていなかった。初回解析を開始直後から単一taskで継続し、会話timeoutでは破棄せず次turnへ結果を引き継ぐ。Discordの明示画面共有commandは複数人宛先判定で無視せず、OBS frameをUIへ表示する。
- Changed:
  - `neuro_voice/vision/video.py`: 単一foreground analysis、shield付きbounded wait、first-frame event、停止時cleanup、診断値
  - `neuro_voice/vision/discord_share.py`: 開始直後のcold analysis、初回preview callback、pending解析の継続、受信/解析状態の区別
  - `neuro_voice/discord_bridge/bot.py`: 明示commandのaddressing override、Discord共有frameのUI通知
  - `neuro_voice/pipeline.py`: Local共有開始時も初回OBS previewを通知
  - `tests/test_discord_screen_share.py`、`tests/test_realtime_vision.py`: preview、単一in-flight解析、timeout非cancelの回帰
- Behavior impact: 「画面共有を見て」は複数人VCでも無視されず、チャットにOBS共有sourceの実フレームが出る。初回解析が2.5秒を超えても裏で完了し、次の質問では解析済み状態を利用できる。
- Config / DB migration: なし。
- Tests: 画面共有・映像・Conversation関連55件成功。変更4モジュールの`py_compile`成功。
- Remaining: GUI再起動後、Discordから開始し、約1秒で共有preview、cold解析完了後に画面内容回答、停止を実機確認する。OBS側のsourceが静止した誤対象ならpreviewを見てsource設定を直す。
- Handoff: `docs/handoffs/2026-07-26_1951_codex_discord-screen-share-analysis.md`
- Related ADR / decision: D-024、R-034

## 2026-07-26 19:40 JST — Codex — ローカル発話のDiscord画面共有ルーティング修正

- Status: completed（自動回帰テスト済み、OBS実機再確認は必要）
- Summary: 「画面共有を見て」がローカルマイクから入力された場合、画面共有sessionを開始せず通常のmonitor 1取得へ落ちていた。Local/Discord両surfaceで同じOBS画面共有session意味論を使い、active中は通常monitor/game captureを迂回するよう修正した。
- Changed:
  - `neuro_voice/pipeline.py`: Localの開始/停止command、OBS共有context優先、OBS frame preview、lifecycle cleanup、debug status
  - `neuro_voice/vision/discord_share.py`: Local/Discord共有sessionであることを明示し、最新OBS frame参照を公開
  - `tests/test_discord_screen_share.py`: Local turnがmonitor captureへfallbackしない回帰
  - `docs/CURRENT_ARCHITECTURE.md`、`docs/CODEMAP.md`、`docs/DECISION_LOG.md`、`docs/KNOWN_RISKS_AND_DEBT.md`: surface parityと実機受入項目を同期
- Behavior impact: トップ画面のローカルマイクで「画面共有を見て」と言っても、monitor 1ではなく設定済みOBS source `Discord画面共有`を継続認識する。停止までこのsourceが視覚groundingを所有する。
- Config / DB migration: なし。既存`discord.screen_share.*`をLocal/Discordで共有。
- Tests: 画面共有専用5件成功。映像・Conversation Kernel/Generation/Featuresを含む関連54件成功。
- Remaining: GUI再起動後、OBS source previewがDiscord共有映像であること、連続質問、停止後に通常画面取得へ戻ることを実機確認する。
- Handoff: `docs/handoffs/2026-07-26_1940_codex_local-screen-share-routing.md`
- Related ADR / decision: D-024、R-034

## 2026-07-26 19:03 JST — Codex — Discord画面共有のOBS継続認識

- Status: completed（自動回帰テスト済み、Discord/OBS実機受入は必要）
- Summary: 「画面共有見て」でDiscord用のOBS映像セッションを開始し、1fpsの最新状態を会話へ参加させ、「見るのやめて」またはVC退出で停止する経路を追加した。DAVE音声経路は変更していない。
- Changed:
  - `neuro_voice/vision/discord_share.py`: 開始/停止意図、OBS transport overlay、時刻付き視覚context、future DAVE映像transport境界
  - `neuro_voice/discord_bridge/bot.py`: Discord final発話の制御、会話context注入、leave/shutdown cleanup
  - `neuro_voice/ui/webview_app.py`、`neuro_voice/ui/assets/index.html`: 有効化とOBSソース名の設定
  - `config/config.yaml`: `discord.screen_share.*`
  - `tests/test_discord_screen_share.py`: command、source分離、lifecycle、誤実行防止
- Behavior impact: Discordで共有画面を見ながら質問できる。明示質問では最大2.5秒だけ最新解析を待ち、超過時は時刻付き直近状態として扱って現在だと偽らない。
- Config / DB migration: DB migrationなし。OBSでDiscord共有画面を捕捉するソースを作り、既定名`Discord画面共有`を一致させる。
- Tests: Discord画面共有・OBS・会話生成・ターン終了45件成功、変更Pythonのcompileall成功。
- Remaining: OBS WebSocketと実Discord共有で開始、連続質問、停止、VC退出、ソース非表示時の文言を確認。DAVE映像の直接復号は未実装。
- Handoff: `docs/handoffs/2026-07-26_1903_codex_discord-screen-share-vision.md`
- Related ADR / decision: D-024、R-034

## 2026-07-26 18:10 JST — Codex — ラジオ停止・終了後の独り言暴走を修正

- Status: completed（自動回帰テスト済み、実機音声での再確認は必要）
- Summary: 実機ログを調査し、終了文が停止ではなく新しい5分ラジオ開始要求として解釈される問題、区間生成・未再生音声が残る問題、終了後もAutonomyが同じ話題を再開する問題を共通停止トランザクションへ統合した。
- Changed:
  - `neuro_voice/dialogue/intent_plan.py`: 「一旦終わろう」「もう終わったよ」等を開始候補より先に停止判定し、否定形は除外
  - `neuro_voice/dialogue/directive_runtime.py`: Local/Discord共通で計画task、区間task、LLM生成、未再生音声、Directive状態を一括取消
  - `neuro_voice/autonomy/engine.py`: 停止後の継続状態を消去し、既定5分間は沈黙・過去話題起点の自律発話を抑止
  - `neuro_voice/pipeline.py`、`neuro_voice/discord_bridge/bot.py`: 共通停止通知を各surfaceのheartbeat・自律発話taskへ接続
  - `config/config.yaml`: 停止後quiet 300秒、沈黙起点の発話上限を5分あたり2回へ調整
  - `tests/test_narration_session.py`、`tests/test_directive_runtime.py`、`tests/test_autonomy.py`: 実ログ文言、pending-plan race、surface parity、quiet解除の回帰
- Behavior impact: 「ラジオは一旦終わろう」で即終了し、古い区間が後から流れず、「もうラジオは終わったよ」が新規ラジオを開始しない。終了後5分は独り言を再開しない。明示的な新しい継続依頼はこのquiet中でも実行できる。
- Config / DB migration: DB migrationなし。`autonomy.post_stop_quiet_seconds: 300`を追加。
- Tests: 継続・自律関連161件成功、変更moduleの`compileall`成功。
- Remaining: GUI再起動後、3分ラジオを途中停止し、即時無音・5分間の自発再開なし・新規明示依頼は開始可能、をLocal/Discord実機で確認する。
- Handoff: `docs/handoffs/2026-07-26_1810_codex_radio-stop-runaway.md`
- Related ADR / decision: D-023、R-026

## 2026-07-26 17:54 JST — Codex — ポッポの無邪気さ・遊び心・軽い生意気さ

- Status: completed（自動回帰テスト済み、実会話での頻度・好みは要確認）
- Summary: ポッポを幼児語や固定語尾へ寄せず、好奇心が表に出る無邪気さ、文脈由来のジョーク、親しい安全な雑談での軽い生意気さとして調整した。深刻な相談・訂正・危険場面では自動的に抑える。
- Changed:
  - `config/config.yaml`: ポッポ固有の人格値と発話原則を更新
  - `neuro_voice/dialogue/working_memory.py`: `playfulness`、`cheekiness`、`spontaneity`を独立した人格軸・GUI項目として追加
  - `neuro_voice/dialogue/conversation_planner.py`: 新人格軸をplayful/energetic/opinionated、会話shape、機能選択へ反映
  - `neuro_voice/dialogue/feature_engines.py`: 現在の具体的文脈から作る小ドラマ、軽い張り合い、自虐的な得意顔を追加
  - `neuro_voice/dialogue/surface_realizer.py`: 親密度と会話の深刻度で無邪気さ・生意気さを制御
  - `neuro_voice/dialogue/intelligence.py`: 新人格軸を会話特徴と明示フィードバック学習へ接続
  - `tests/test_conversation_generation.py`、`tests/test_conversation_features.py`: 人格重み、深刻場面の抑制、ユーモア候補の回帰
- Behavior impact: 楽しい話では短い素直なリアクションや一度だけの軽いからかいが出やすくなる。毎回の口癖化、相手への侮辱、深刻場面での冗談は抑制される。
- Config / DB migration: DB migrationなし。既存personaは新しい3軸が未設定でも安全な既定値で補完される。設定GUIには動的に3項目が追加される。
- Tests: 会話生成・特徴25件、既存interaction 139件、計164件成功。`compileall`成功。
- Remaining: 実会話でジョーク頻度、生意気さの強さ、SBV2上の聞こえ方を確認し、ポッポの3軸だけ微調整する。
- Handoff: `docs/handoffs/2026-07-26_1754_codex_poppo-childlike-personality.md`
- Related ADR / decision: D-022、R-033

## 2026-07-26 17:40 JST — Codex — ローカルマイク・TTS同時停止の修正

- Status: completed（自動回帰テスト・実デバイス列挙済み、GUI再起動後の実機会話は要確認）
- Summary: 3秒ごとの音声デバイス監視がPortAudio全体を毎回terminate/initializeし、既存のマイク入力とTTS出力を停止させていた。通常監視をread-onlyにし、停止検知時だけ安全な再取得・再接続を行うよう修正した。
- Changed:
  - `neuro_voice/audio/devices.py`: 通常pollでPortAudioを暗黙再初期化しない
  - `neuro_voice/audio/capture.py`: stream objectの存在でなく実際の`stream.active`を監視
  - `neuro_voice/audio/playback.py`: 停止した出力streamを同じPCM位置から最大3回再構築し、workerを自己復旧
  - `neuro_voice/pipeline.py`: read-only device watchと、会話・再生中を避けた停止時refresh
  - `tests/test_local_audio_liveness.py`、`tests/test_audio_devices.py`: 再初期化禁止、入力停止検知、出力復旧の回帰
- Behavior impact: 起動後数秒でマイクと音声が同時に死なず、USB音声streamが一時停止しても次のTTSを受け付け続ける。
- Config / DB migration: なし。`audio.input_device=null`は現在のRazerマイク、`audio.ai_output_device=9`はRazerスピーカーであることを実デバイス列挙で確認。
- Tests: 実運用Pythonで音声liveness 4件、TurnClosure 9件、GameProfile 5件の計18件成功。`compileall`成功。
- Remaining: GUIを再起動し、30秒以上マイクメーターが動き続けること、連続2応答がRazerから聞こえることを実機確認する。
- Handoff: `docs/handoffs/2026-07-26_1740_codex_local-audio-liveness.md`
- Related ADR / decision: D-021、R-032

## 2026-07-26 17:00 JST — Codex — フォルダ型ゲームプロファイルとKTaNEマニュアル担当

- Status: completed（静的検証・smoke test済み、実機音声プレイは未確認）
- Summary: ゲーム固有設定を`game_profiles/<game_id>/`へ集約し、Minecraft互換を維持したまま、Keep Talking and Nobody Explodesでポッポが画面を見ないマニュアル担当になる専用プロファイルと共通セッションを追加した。
- Changed:
  - `game_profiles/`: 一覧README、Minecraft、KTaNE、公式日本語マニュアル参照情報
  - `neuro_voice/games/profiles.py`: JSONプロファイルの検証・列挙・alias解決
  - `neuro_voice/games/session.py`: Local/Discord共通のKTaNE開始・終了・役割コンテキスト
  - `neuro_voice/mind/mind.py`: persona別ゲームセッション、検索禁止、Mind context統合
  - `neuro_voice/pipeline.py`、`neuro_voice/discord_bridge/bot.py`: 共通ゲーム入力とactive状態を会話経路へ接続
  - `neuro_voice/vision/service.py`: プロファイルの映像方針を解析指示へ反映
  - `neuro_voice/ui/webview_app.py`、`neuro_voice/ui/assets/index.html`: フォルダから設定一覧を動的生成
  - `config/config.yaml`: `game_profiles.root`とKTaNEマニュアル版
  - `tests/test_game_profiles.py`: registry、folder validation、Local/Discord parity、session persistence
- Behavior impact: KTaNE開始時はポッポがExpertになり、爆弾画面を受け取らず、プレイ中のWeb検索を禁止し、不足情報を推測しない。プロファイル追加は新しいゲームフォルダで一覧へ現れる。
- Config / DB migration: DB migrationなし。persona別`data/game_session_<persona>.json`を新規作成する。既存`video.game_profile: minecraft`は互換維持。
- Tests: compileall成功。`GameProfileTests` 5件と`TurnClosureTests` 9件、計14件成功。
- Remaining: 公式日本語マニュアル全文のローカル同期と全モジュールの決定的solverは別段階。現時点のprofileは役割・参照版・安全な会話制約を提供する。
- Handoff: `docs/handoffs/2026-07-26_1700_codex_game-profiles-ktane.md`
- Related ADR / decision: D-020、R-031

## 2026-07-26 16:28 JST — Codex — 自然な会話終了と短応答

- Status: completed（自動テスト済み、実機会話は未確認）
- Summary: 一対一でも全発話へ長く返す一問一答挙動を改め、ユーザーの相槌・理解・感想だけで会話が自然に閉じる場合は、LLM・検索・TTSを開始しない共通turn closure層を追加した。
- Changed:
  - `neuro_voice/dialogue/turn_closure.py`: `CONTINUE / SILENCE / BRIEF_ACK`の決定的分類
  - `neuro_voice/dialogue/planner.py`: closure結果を第一級`ResponsePlan`へ統合
  - `neuro_voice/dialogue/state.py`: assistant本文・Activity状態・終了時の話題復元
  - `neuro_voice/dialogue/orchestrator.py`: Local/Discord共通のsettleと観測reason
  - `neuro_voice/pipeline.py`、`neuro_voice/discord_bridge/bot.py`: Activity優先、direct短応答、assistant本文伝播
  - `neuro_voice/mind/mind.py`: canonical Activity有効状態の公開
  - `config/config.yaml`: `conversation.turn_closure`
  - `tests/test_turn_closure.py`、`tests/test_interaction.py`: 終了・保護・回帰テスト
- Behavior impact: 「なるほど、そういうことか」「たしかに、それ面白いね」等では無応答を選べる。質問、訂正、依頼、実行確認、しりとり等は沈黙へ落とさない。「ポッポ、ありがとう」は生成せず「どういたしまして。」だけ返す。
- Config / DB migration: DB変更なし。feature flag既定ON。`conversation.turn_closure.enabled: false`で旧動作へ戻せる。
- Tests: py_compile成功。`ConversationAddressingTests` 23件 + `TurnClosureTests` 9件、計32件成功。全unittest discoveryは読み込めた290件成功・3件skipで、20 moduleはbundled runtimeにpytest/PyYAML/requestsがないためimport error（テスト失敗ではなく環境不足）。
- Remaining: 実機の発話replayでfalse-silence/over-responseを計測し、語彙を追加する場合もguardを先に拡張する。
- Handoff: `docs/handoffs/2026-07-26_1628_codex_turn-closure.md`
- Related ADR / decision: D-019、R-030

## 2026-07-26 — Codex — ポッポ開発憲法1.1.0

- Status: completed
- Summary: 所有者の承認に基づき、意図・指示対象・会話状態を継続的に理解することと、経験が現在の判断へ参加する固有人格を最上位目標として明文化した。
- Changed:
  - `docs/PROJECT_CONSTITUTION.md`: version 1.1.0をACTIVE化し、会話理解、固有人格、参照品質、思考と表現を第1条へ追加
  - `docs/DECISION_LOG.md`: 憲法の承認を確定し、品質目標の明文化をD-018として記録
  - `docs/SHARED_CHANGELOG.md`: 本改正を共通台帳へ記録
  - `docs/handoffs/2026-07-26_codex_constitution-v1.1.0.md`: 改正内容と検証を記録
- Behavior impact: 今後の会話機能は、一問一答の正答だけでなく、参照解決、意図理解、話題継続、自然なターンテイキング、経験に基づく固有反応を評価対象とする。
- Config / DB migration: なし
- Tests: 文書の版・status・第1条・決定ログ・共通台帳の整合性を静的確認
- Remaining: 実装が新しい憲法目標を満たすかは機能別の差分監査と実機評価が必要
- Handoff: `docs/handoffs/2026-07-26_codex_constitution-v1.1.0.md`
- Related ADR / decision: D-013、D-018

## 2026-07-26 12:00 JST — Claude — 継続行動基盤 BehaviorDirective と実機フィードバック修正

- Status: partial（実装とテストは完了、実機検証は未了）
- Summary: 「しばらく話して」「終わるまで実況して」等が1回の返答で終わる問題に対し、専用エンジンではなく共通の継続依頼基盤を新設した。あわせてユーザー実機検証7ラウンドで判明したSTT誤変換、話者分裂、応答途切れ、物語破綻、デバイス認識の各問題をログ根拠付きで修正した。
- Changed:
  - `neuro_voice/dialogue/directive.py`（新規）: ExecutionMode / InteractionMode / PlanProposal / BehaviorDirective / SegmentAction / SegmentLoopDetector / DirectiveStore。純粋な状態のみ
  - `neuro_voice/dialogue/intent_plan.py`（新規）: 継続依頼の指名、追加指示の分類、プロンプト構築、素の依頼の決定的計画
  - `neuro_voice/dialogue/continuation.py`（新規）: 汎用ContinuationController。ラジオ/実況/見守り/共同思考/物語を専用クラスなしで実行
  - `neuro_voice/dialogue/directive_runtime.py`（新規）: Local/Discord共通の実行ランタイム（DirectiveHostで差分注入）
  - `neuro_voice/dialogue/kernel.py`: TurnFrameへ execution_mode / directive_action / plan_candidate を追加。検索判定を `search/intent.py` へ委譲
  - `neuro_voice/search/intent.py`（新規）: 「調べて」の依頼形と過去形を区別。kernel と deepsearch の二重定義を解消
  - `neuro_voice/stt/transcript.py` `transcript_repair.py`（新規）: 認識結果を仮説として扱い、同音異義語を文脈で補正
  - `neuro_voice/llm/context_budget.py`（新規）: 送信前にトークン予算で履歴を切り詰め、出力枠を確保
  - `neuro_voice/vad/segmentation.py`（新規）: 語頭のフィラーを無音として捨てない
  - `neuro_voice/audio/devices.py`（新規）: 起動後に接続されたデバイスの検出
  - `neuro_voice/utils/errors.py`（新規）: 例外メッセージからURL/パーセント列を除去
  - `neuro_voice/mind/mind.py` `speakers.py` `relationship.py` `dialogue/intelligence.py` `dialogue/adaptive_store.py`: 同一人物の話者統合（声紋・関係性・会話プロファイル・適応学習）と場面別の別名
  - `neuro_voice/pipeline.py` `neuro_voice/discord_bridge/bot.py`: Directive配線、文脈予算、区間TTSの文単位分割、旧regex継続の停止
  - `neuro_voice/audio/capture.py` `playback.py`: デバイス不在で停止せず再接続を待つ
  - `neuro_voice/autonomy/engine.py` `state.py`: 自発発話のハードコード5種を廃止、OpenThreadに発言原文と経過時間
  - `neuro_voice/tts/volume.py`: 「読み上げてみる」が音量アップと解釈される誤爆を修正
  - `neuro_voice/ui/webview_app.py` `ui/assets/index.html`: 音声モデルの並行初期化（起動51秒→28秒）、Directive状態表示、話者統合UI
  - `neuro_voice/bootstrap.py`: 依存確認でdiscordを実importしないようメタデータ判定へ
  - `config/config.yaml` `SetupOllamaEnv.bat` `README.md`
  - `tests/`: 新規16ファイル
- Behavior impact:
  - 継続依頼が状態として保持され、区間ごとにLLM自身が次の行動を決める。人の発話でCANCELではなくPAUSEする
  - LocalとDiscordが同じ判断（Mind所有のController）を共有する。Discordでも区間を実行する
  - `num_ctx` 4096→8192。**`SetupOllamaEnv.bat` の再実行が必要**（configだけではサーバ上限で頭打ち）
  - `data/dialogue_*.json` の `users[*].topics` は読込時に自動掃除される（文まるごと保存されていた値を破棄）
  - 既存の分裂した話者プロファイルは「こころ」画面から1回統合操作が必要
- Config / DB migration:
  - 新規節: `behavior_directive` / `stt.transcript_repair` / `speaker.sticky_*` / `audio.device_watch_interval_s` / `llm.context_reserve_tokens`(auto)
  - DB schema変更なし。dialogue JSONのtopicsのみ読込時に浄化
  - すべてfeature flagで従来動作へ戻せる（handoffのRollback節）
- Tests: `pytest tests/ -q` → 664 passed / 3 skipped / 11 subtests（音声系2件はsandbox依存で除外）。開始時373からの増分はすべて新規
- Remaining:
  - **実機検証が全面的に未了**（音声・Discord・VRAM実測・レイテンシp50/p95）
  - 長いセッションで履歴が落ち筋が失われる。セッション要約は未実装
  - ゲームイベントのDiscord側供給がない（実況DirectiveはDiscordでWAITのまま）
  - R-004（巨大クラス）は今回さらに肥大した
- Handoff: `docs/handoffs/2026-07-26_1200_claude_behavior-directive-and-realtime-fixes.md`
- Related ADR / decision: D-014、D-015、D-016、D-017（継続行動基盤のADR化は次担当へ提案）

## 2026-07-26 — Codex — 共通同期台帳の導入

- Status: completed
- Summary: CodexとClaude Codeが同じ憲法、変更履歴、handoffを読み書きする入口を追加した。
- Changed:
  - `AGENTS.md`: 共通の開始手順、変更後の同期、開発上の不変条件を定義
  - `CLAUDE.md`: `AGENTS.md`を取り込み、Claude Codeにも同じ同期規則を適用
  - `docs/SHARED_CHANGELOG.md`: 両AIが共有する逆時系列の変更台帳を追加
  - `docs/AI_DEVELOPMENT_PROTOCOL.md`: 共通台帳への追記を完了条件へ追加
- Behavior impact: 新しい作業では、直前に別AIが変更した内容を共通台帳から追跡できる。
- Config / DB migration: なし
- Tests: 文書間の参照先と必須更新手順を確認
- Remaining: 新しいCodexタスク／Claudeセッションで読込確認を行う
- Handoff: `docs/handoffs/2026-07-26_codex_shared-change-ledger.md`
- Related ADR / decision: なし
## 2026-08-04 02:06 JST — Codex — Phase 7D Trace output path repair

- Status: partial (awaiting one real GUI turn after restart)
- Summary: Fixed the local normal-conversation Trace route; Memory, Persona, DB, and migration state were not changed.
- Changed: bounded async writer, project-root resolution, startup diagnostics, legacy normal-turn emit, absolute config source path, and isolated writer tests.
- Tests: compileall PASS; direct writer checks PASS. Pytest is blocked by the broken project interpreter and missing packages in the bundled runtime.
- Remaining: one full restart and the user’s single real utterance; then inspect process provenance, startup log, and JSONL.
- Handoff: `docs/handoffs/2026-08-04_0206_codex_phase7d-trace-path.md`

---
## 2026-08-04 02:18 JST — Codex — Phase 7D Trace real-GUI verification

- Status: completed for the requested local AItuber trace path
- Result: one post-restart normal local turn generated exactly one current JSONL record at the project-root path. Startup diagnostics reported Trace enabled, writer initialized, queue enabled, no error, and the absolute local config path.
- Observed metadata: active persona `neuro`; retrieved records were all `neuro` / `persona_private`; `persona_leak=false`; `trace_enqueue_ms=0.12`; queue depth 1; dropped count 0.
- Scope: no Memory DB, persona settings, or migration state changed. Do not resume Memory smoke until the user directs it.
- Handoff: `docs/handoffs/2026-08-04_0206_codex_phase7d-trace-path.md`

---
## 2026-08-04 02:47 JST — Codex — Phase 7D Memory retrieval/write provenance repair

- Status: partial (code verified in isolated tests; real restart verification pending)
- Root cause: Local normal turns called `Mind.build_context(..., include_recall=False)`, so b’s correctly scoped records were never searched. The old Trace then reported stale/empty recall state.
- Changed: enabled bounded real recall on the local context path; strict MemoryStore text/embedding reads honor the bound persona; Trace now records privacy-safe retrieval stage counters; questions cannot become preference candidates; new episode writes receive origin turn/persona provenance.
- Scope: existing Memory DB rows, migration data, and persona settings were not edited.
- Tests: compileall PASS; isolated scope/provenance/question-gate/Trace diagnostics checks PASS. Full pytest remains blocked by the project interpreter/dependency condition.
- Remaining: after a full restart, one controlled b/c/b retrieval smoke and one question-only write-gate check. Do not run the old broad smoke sequence.
- Handoff: `docs/handoffs/2026-08-04_0247_codex_phase7d-memory-retrieval.md`

---
