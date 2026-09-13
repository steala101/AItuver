# ADR-0005: 会話品質を実測し、既存の判断・根拠・表現経路を段階改善する

- Date: 2026-09-07
- Status: PARTIALLY ACCEPTED / P0〜P2 CODE VERIFIED、独立MはUSER-APPROVED PRODUCTION PROMOTED、P3以降と会話実機受入はPROPOSED。
- Author: Codex（再レビュー担当）
- Executor: Sol（ユーザー指定。自動起動・依頼送信はしていない）
- Scope: 現在のローカル正本 `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Plan: [Sol向け実装指示書](../superpowers/plans/2026-09-07-conversation-quality-sol.md)

## 1. 目的と前回提案の修正

2026-09-07ユーザー回答: **Local優先、次にDiscord複数人／ほどほどに活発でからかい多め、製作者へ強め／整合性を守って速度優先／Local LLMのみ／現在の声を維持、Fish Audioは候補／状況依存の自然な発話／容量増を許容して記憶保持**。この製品方針は確認済み。詳細実装は引き続き本計画で扱う。

目的は、ユーザーの現在の意図へ自然に応じ、確かでないことを捏造せず、人格と声に連続性がある会話にすること。配送成功、pytest成功、Trace項目の存在だけで会話品質の合格とはしない。

前回の「経路が分散していることが中核原因」という説明は強すぎた。分散は確認できる設計負債だが、理解・自然さの低下への寄与率は未測定。共通化だけで賢くなるとは扱わない。次の訂正を設計前提にする。

1. 感情の連続状態、Move選択、人格表現、WorkingMemoryは既に存在する。新しい感情エンジン・Memory・Plannerを足す前に、最終応答へ届くかを測る。
2. 検索の今回の失敗には再現できる判定バグがある。全体改造より先に閉じる。
3. 現在のモデル能力、プロンプト干渉、音声表現の限界を分離して評価する。最新モデルや他社製品との同等性能は約束しない。
4. キャラクター性は、文脈に合う見解・好み・反応・関係の継続で評価する。語尾、笑い、質問頻度、ランダム性を増やすだけでは合格にしない。

## 2. 根拠の区分

| ID | 区分 | 確認事項と含意 |
|---|---|---|
| E1 | CONFIRMED / 純粋関数実行 | `is_search_request` と `should_search` は「饅頭こわいを検索して、短く教えて」でfalse、「饅頭こわいを検索して」でtrue。複合依頼が検索入口を抜ける。実機の全原因を特定したわけではない。 |
| E2 | CONFIRMED / 純粋関数実行 | 同じ複合依頼を `build_query` が対象名だけへ整理できない。依頼検出とquery抽出を一緒に検証する必要がある。 |
| E3 | CONFIRMED / コード | `DeepSearch._expand_queries` は一般queryにも「対処法」「条件 手順」を足し、`_rank_results` は攻略・手順語を優先する。一般の作品解説に適した検索目的別処理になっていない。 |
| E4 | CONFIRMED / コード＋合成fixture | `normalise_search_results` は `excerpt → body → content` を選ぶ。`_safe_evidence` がtitle用160文字cleanerを使い、360文字上限に届かない。合成300文字body＋900文字contentはbodyの160文字になった。 |
| E5 | CONFIRMED / コード | Presenterはqueryを受け取らずOpenAI候補を優先する。`relevance`は順位からの定数式で、意味的関連性の測定値ではない。 |
| E6 | CONFIRMED / コード | Discordの `_grounded_search_reply` は無効設定・非検索・policy拒否をすべてNoneで返す。呼出側が通常生成へ進めるため、「検索不能」と「検索不要」の区別が必要。 |
| E7 | CONFIRMED / コード | Discordの当該検索awaitはactive response登録より前にある。検索後のUI・発話に現世代チェックが必要。実際にstale表示が起きたとは未判定。 |
| E8 | CONFIRMED / コード | `ConversationCritic.evaluate` の `goal_focus=1.0`、`personality_continuity/memory_naturalness/humor_fit/emotional_fit/topic_scope=.9` は固定値。自然さの測定として利用不可。 |
| E9 | CONFIRMED / コード | `ConversationPlanner._emotion` はinertia付きvalence/arousalを既に保持。`UnifiedExpressionPlan`、`PersonalityEngine`も存在。複数状態の役割・更新頻度・TTS反映は要監査。 |
| E10 | CONFIRMED / コード | `ResponsePlanner`、`ConversationPlanner`、`CognitiveKernel`、`SurfaceRealizer`が別の責任を持つ。数の多さ自体はバグではないが、応答義務・質問・長さの競合を検証する必要がある。 |
| E11 | USER_REPORTED | 音楽継続、最新入力への切替、退出は直近のユーザー試験で改善。検索後の作品説明は未合格。前3項目の広範囲な再実装は不要。 |
| E12 | HISTORICAL | 最新handoffの全pytestは2974 passed / 1 skipped。今回全pytestは実行していない。過去のLegacy p50=3652ms / p90=4113ms / p95=4202msも今回のbaselineではない。 |

本レビューは静的追跡とネットワーク・LLM・DBを使わない純粋関数probe。現在の実行プロセス、ASR、GPU、実効設定、声の聞こえ方は新たに確認していない。古いhandoffの「未合格」やCURRENTの全項目を最新事実として転記しない。

## 3. 方針と採否

| 案 | 採否 | 理由 |
|---|---|---|
| 人格prompt・感情値だけを強める | 不採用 | 検索の未実行、参照の誤り、配送問題を直せず、作話や定型演技を増やし得る。 |
| メインモデル変更・音声方式変更を先行 | 保留 | 同じLocalモデルと短いpromptの比較を先に行う。クラウドLLMは対象外、声は維持。Fish Audioは導入せず候補として保持。 |
| 評価を先に作り、検索→判断契約→表現を小さく改善 | 採用提案 | 再現できる失敗から直せ、既存のMemory分離・配送・DAVE改善を維持しやすい。 |

実装順は P0 基準線 → P1 検索入口 → P2 根拠選択 → P3 判断整合 → P4 会話表現 → P5 感情・声 → P6 遅延 → P7 実機受入。2026-09-08にP0/P1/P2のoffline/fake実装と、単一7秒query+fetch deadline、検索中cancelの早期待機解除・後続page/redirect停止まで完了した。P3以降へは結果確認後に進む。

追記: P0でMemory保持監査を先に行い、2026-09-08〜10に別単位Mをtmp_pathだけで実装・六段階の独立レビュー修正を行った。日数/件数の自動削除を非削除policyへ置換し、正本recordと再構築可能なFTS/semantic候補sidecar、bounded contextを分離した。freshnessはindex対象列だけのcanonical永続revisionが所有し、Recallのtouchは再構築を起こさない。semantic候補はMemory/transcriptとも全次元deterministic dense random-hyperplane ANNを使い、full tier 14表/Hamming-2でcosine .72のfixed-seed nomination 95%以上を守る。canonical cosine .95以上だけは4表/Hamming-1 fast tierで停止する。各probe row、Memory/transcript候補80/40、context各5を上限とし、canonical persona/scope/statusとvector dim/length/finite/nonzeroを再検査する。canonical rawは一度だけunit float32へ正規化し、sidecar exact/bandsとprovenance再導出は同じ表現を使うため、`N(N(v))`のfloat32丸めをpoisonと誤認しない。選択sidecar rowのband/bucket/exact provenanceが不一致なら派生sidecar全体だけを一度atomic再構築して同じqueryを最大一回再試行する。invalid/well-formed wrong-vector exact hitはfull ANNへ進める。repairは単一Store lock区間で、PermissionErrorはsafe fallbackへ倒す。embedder warmupも旧来の永続vector全RAM展開を廃止した。disk-fullは全source mutationをrollbackしてprocess内read-onlyへ倒し、完全migration済みquery-only startupだけをretrieval-onlyで許可する。不正VIEWは派生ファイルだけ一回置換し、path identityはopen前後とDDL前に検査する。benchmarkはtarget cosine/probe pathを明示し、full tierの高衝突100k synthetic p95 367.410msをRecall・integrity品質とのtradeoffとして記録する。同権限の悪意writerによる検出不能なsidecar omissionの可用性は保証外で、OS directory ownershipと明示rebuildを残存防御とする。明示削除primitive/DO_NOT_STOREは維持するが、確認付き自然言語削除callerは破壊操作の別承認事項。14表build中の長いsource lock、本番DB移行・永続write設定変更・実機起動も別承認のまま。

2026-09-10運用追記: ユーザーがStage M後日確認事項と本番昇格を明示承認した。停止中に本番7 DBをSQLite online backupし、全DB integrity、件数、ID・本文・summary・embedding指紋の一致を確認した。隔離copyでmigration＋generation 8 sidecar、既知vectorの反復Recall、候補/context上限、persona/status漏れ0、rebuild 0/0、backup不変を確認後、本番schema/sidecarへ昇格した。canonical指紋は不変で、証跡は`data/backups/memory_retention_stage_m/20260910_074937_preindex`に置く。さらにcanonical/data/backup/sidecar所在volumeの空き率をcontent-freeに測り、既定20%未満をwarning、probe失敗をUNKNOWNとして共有Mind statusと「こころ」へ公開した。低容量/UNKNOWNを自動削除やwrite停止へ結び付けない。永続configと`memory.write_enabled`は変更していない。過去の`PRODUCTION_DATA_UNTOUCHED`記録は当時の事実として保持する。

## 4. 所有権と境界

| 対象 | 正本／改善位置 | 禁止する重複 |
|---|---|---|
| persona・rollout・epoch・相関ID | 既存TurnFrame/TurnMetrics、rollout resolver | 各段階でactive personaやON/OFFを読み直すこと |
| 現在の会話目的・参照・訂正 | Mind配下ConversationKernel/WorkingMemory | 第2の会話履歴・永続Memoryを新設すること |
| 応答義務とAction適格性 | 既存ResponsePlan→ActionEligibility→Validator | 後段の文体選択がsilenceやTool実行を上書きすること |
| 返し方 | ConversationSelectionKernel/ConversationPlan | 全turnへ意見・共感・質問・冗談を必須にすること |
| 外部情報 | Search intent→既存permission/executor→Evidence→Presenter | 検索失敗を通常LLMの知識で黙って埋めること |
| 表現 | 既存UnifiedExpressionPlan→StyleManager→TTS | 各surfaceが別の感情状態を更新すること |
| 配送 | 既存SpeechRequest/TurnTracker/Playback coordinator | Tool専用の直接TTSや第2のdelivery ledger |

共通化は再現fixtureが両surfaceで同じ結果を出す境界から行う。最初は検索判定・結果表現とAction→Moveの整合だけを共有し、巨大classの一括分割はしない。transport固有の受信、PCM、mix、接続処理は維持する。

## 5. 自然さと根拠性

通常雑談はメインLLMの一回生成を基本とし、コードは権限・世代・事実出典・配送を保証する。すべての言い回しをコードで決めない。表現上の反復は品質信号であり、別turnの同じ正答を無音にする理由ではない。

検索はまず現行D-041の抽出的回答を改善する。引用span、結果ID、queryとの関連、十分性を管理し、タイトル発見と質問への回答を区別する。「安全なURL」「source IDがある」「抜粋と一致する」は、それぞれアクセス安全性・出典整合・抽出整合であって、内容の真実や質問への適切さの証明ではない。

将来の自然な検索要約には、同じメインLLMの一回生成へ限定Evidenceを渡す選択肢がある。ただしD-041の変更提案として別途承認する。承認前の実装は抽出経路まで。source IDをモデルが付けたことだけでgrounded=trueにしない。本文にない筋書き・固有名詞・結末の補完を検出できない要約方式は実験止まりとする。

感情はユーザーの心理を断定する装置ではない。既存の会話気分・長期人格・ユーザー感情推定を区別し、短期状態は一turn一更新、同epochへ限定する。voice capabilityがNeutralだけなら喜怒哀楽styleを架空に生成しない。

からかいは文脈と関係性に基づくsoft選択。製作者への強い当たりは既存Identityで相手を確認し、不快表明・深刻な相談・訂正場面では抑える。冗談を毎回強制せず、からかいながらも質問へ答える。記憶の継続性と固有の見解を人に近い会話の評価対象にする。

## 6. 評価・予算・停止条件

- 配送・privacy・検索実行契約は決定的assert、文脈理解・自然さは合成会話と人間評価、音質は実機評価に分ける。未測定はnull/NOT_MEASURED。固定高得点は廃止する。
- 既存ユーザー会話・raw audioはfixtureに複製しない。計画に示す人工会話を使う。本番DBを開くfixtureを使わない。
- 通常CIはfake LLM/TTS/searchでネットワーク0。有料API、自動実機発話、モデルdownload、永続設定変更は自動で行わない。
- 実モデル比較はインストール済みLocalモデル・合成24入力以下、最大48本文生成を上限とする。実装段階で隔離実行し、実会話とGPUを競合させない。クラウドモデル/外部judgeは使わない。
- 技術失敗を「自然な沈黙」と採点しない。missing Traceは成功率の分母から黙って除外しない。
- hard契約失敗、世代漏れ、個人情報混入、二重配送、明示検索から創作への迂回が1件でもあれば、その段階の昇格を止める。

## 7. 承認・確認待ち

指示書作成は承認済み。Solによる実装はユーザーから実装開始指示を受けた時点で実行する。既存憲法・承認済みADRは維持し、本ADRを保存したことだけでD-041や本番権限を変更しない。

主な希望はユーザー回答で確定した。Stage Mのtranscript retain、backup/本番昇格、容量20%警告も2026-09-10に承認・実施済み。残る製作者Identity・からかいの範囲、Local LLM検索要約のD-041変更、Fish Audio具体比較条件は実装計画末尾へ記載する。P0〜P2のオフライン検証を再質問で止めない。
