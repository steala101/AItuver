# ポッポ Code Map

<!-- 2026-09-11 search evidence regression: `search/deepsearch.py::_ArticleTextExtractor` owns main/article/MediaWiki-first HTML extraction and paragraph/heading fallback without global nav lists; tag-matched unwinding and non-stacked void elements keep malformed furniture from leaking exclusion depth. `cognition/search_result_presenter.py` owns boilerplate rejection, complete 1-2 sentence BRIEF output, bounded process-local supporting text, and supported-only ENDING spans bound to the same primary relevant result; meta ending mentions are not outcome evidence. Local `pipeline.py` and `discord_bridge/bot.py` apply the current route mode when reusing the same Outcome and never execute a second search. `utils/errors.py::safe_exception_summary` plus both Local/Discord TTS catches keep UI/log diagnostics to exception type/status and content-free length/hash. Top-level `ui/assets/index.html` keeps `.sys` textContent-based, selectable and wrapping. -->

<!-- 2026-09-10 Stage M current: `mind/store.py::RetentionPolicy` owns non-destructive source retention; typed explicit deletion remains separate. `memory_search_state` triggers own persistent freshness only for indexed canonical fields. `*.search-index` schema generation 8 is a disposable FTS5 + 14-table deterministic dense random-hyperplane ANN index for Memory/transcript. High-cosine queries may stop after a 4-table/Hamming-1 tier only after canonical cosine >=.95; the full tier is Hamming-2. Probe rows are capped per table, returned candidates remain Memory80/transcript40. Canonical raw vectors are validated and normalised exactly once to unit float32; sidecar exact/bands and selected-row provenance both derive from that same representation, never `N(N(v))`. Persona/scope/status plus dim/length/finite/nonzero remain canonical gates. Any mismatch rebuilds only the derived file once and retries the same semantic query at most once, including well-formed wrong-vector poison. Repair holds one Store RLock across close/unlink/recreate/rebuild so parallel search cannot open an intermediate sidecar; Windows file-lock errors become safe semantic-empty or lexical fallback. Invalid/corrupt VIEW/TABLE schema replaces only the derived file once; path identity is checked before/after open and before DDL. `_source_write` owns mutation rollback/read-only transition; current query-only schema remains readable, required migration raises `MemoryStoreMigrationRequired`. `storage_health()` additionally reports content-free per-volume capacity for canonical/data/backup/sidecar with configurable 20% warning and non-fatal UNKNOWN. `mind/mind.py` opens both initial/switched persona stores through one factory, has no startup full-vector preload, uses bounded semantic candidates, and exposes the same storage health to Local/Discord status. `tools/benchmark_memory_retention.py` remains synthetic-only. Production seven-DB schema/sidecars were promoted only after the separately approved backup/isolation/fingerprint gate on 2026-09-10; earlier untouched records remain historical. Same-directory malicious-owner undetectable omission remains outside the availability guarantee. -->

<!-- 2026-09-08 P0-P2: `search/intent.py` owns compound-request parsing and emits frozen `search/contracts.py::SearchRouteDecision`; `cognition/search_result_presenter.py` owns query-aware normalization, `SearchOutcome`/`EvidenceSpan`, extraction validation, deterministic presentation, and the Discord/shared executor-side 7-second/shorter-config deadline with 50ms stale-owner polling. `search/deepsearch.py` propagates a supplied `is_current` through query, page start, and redirects while always enforcing the same monotonic deadline. `pipeline.py` uses that strict deadline plus its existing response/delivery gates; `discord_bridge/bot.py` additionally uses the polling executor route. Both retain their permission/transport owners. `dialogue/conversation_quality_eval.py` plus `tools/evaluate_conversation_quality.py` are offline synthetic evaluation only. -->

<!-- 2026-08-07: Phase 8 Local PROD Tool probe: `pipeline.py` recognizes an explicit read-only search request and proposes the existing `web_search.search`; `cognition/tool_runtime.py` supplies a process-local READ_ONLY enablement and one-attempt limit that leave config and write tools disabled; `cognition/search_result_presenter.py` re-normalizes untrusted result metadata and a bounded extractive Evidence View, then deterministically selects the one UI/TTS result; `trace.py` records result-use evidence without arguments, titles, URLs, snippets, or output text. Discord does not enable this Tool override, but its explicit DeepSearch reply calls the same presenter rather than passing raw results to an LLM. -->
<!-- 2026-08-07: `VoicePipeline._respond_direct_text()` is not a separate TTS route: it delegates to `_speak_loop`, which owns TurnTracker TTS jobs, logical playback sessions, segments, callbacks and sealing. `_await_delivery_finalization()` gates direct/normal cognitive completion; `trace.py` records content-free finalizer state/error. -->

<!-- Phase 8: `neuro_voice/cognition/rollout.py` owns rollout resolution; `cognition/test_session.py` owns temporary TEST/PROD session epochs; `cognition/fallback.py` owns pre-side-effect fallback admission. `pipeline.py` and `discord_bridge/bot.py` consume the same frozen TurnMetrics contract. `dialogue/planner.py:ResponsePlan.requires_response_contract` distinguishes addressed answer/continuation intent from activity input/acknowledgement; `pipeline.py` freezes only that contract into `Mind.cognitive_state`; `kernel.py` enforces it; `executor.py` validates silence provenance; `trace.py` records the privacy-safe silence contract. -->
<!-- 2026-08-07: `cognition/action_constraints.py` owns hard per-turn ActionEligibility and ActionConstraintValidator. `pipeline.py` derives its fields from existing closure, TurnFrame, and response-plan signals, injects the text-free snapshot before selection, and records it in `trace.py`; constrained BRIEF_ACKNOWLEDGE uses the direct one-sentence output route. -->

- Snapshot: 2026-09-10
- 対象: 実働ルートの主要ファイル

小さなutilityを網羅する一覧ではなく、変更時に責任境界を判断するための地図である。状態ラベルは`ACTIVE`、`PARTIALLY_ACTIVE`、`LEGACY`、`EXPERIMENTAL`、`UNKNOWN`を使う。

## 1. 起動と構成

### `run.py` — ACTIVE

- 責任: stdout/stderr保護、依存確認、アプリ入口への委譲。
- 主要関数: `main`相当のtop-level。
- 入力/出力: CLI引数 / process exit。
- 呼び出し先: `neuro_voice.bootstrap.check_dependencies`、`neuro_voice.app.main`。
- 副作用: process初期化。
- 注意: 業務ロジックを追加しない。

### `neuro_voice/app.py` — ACTIVE

- 責任: GUI、local CLI、Discord、video file modeの選択と主要依存の組立。
- 主要関数: `main`、`_amain`、`_amain_discord`、`_amain_video`。
- 入力/出力: argparse/config / 各runtime。
- 呼び出し先: LLM/TTS/STT factory、Mind、ConversationManager、VoicePipeline、DiscordBridge、GUI。
- 副作用: model/server/task lifecycle。
- 注意: shutdown順序と共有instanceを崩さない。

### `neuro_voice/utils/config.py` — ACTIVE

- 責任: YAML、`.env`展開、runtime override、値取得・一部永続化。
- 主要クラス: `Config`。
- 所有状態: 読み込んだconfig mapping。
- 設定: `config/config.yaml`、`AITUBER_*`環境変数。
- 既知の問題: YAML duplicate keyを検出せず、後勝ち。GUIにも別の永続化helperがある。
- 注意: secret値をログへ出さない。duplicate検出なしで構造変更しない。

### `config/config.yaml` — ACTIVE

- 責任: 全feature flagとbackend設定。
- 既知の問題: `privacy`二重定義、`tts.backend`と`audio_output.backend`の併存。
- 注意: コメントを仕様の根拠にせず、factoryの優先順位を確認する。

### `game_profiles/` — ACTIVE

- 責任: ゲームごとの役割、表示名、alias、映像方針、知識参照を一目で管理する宣言データ。
- 現行profile: `minecraft/`、`keep_talking_and_nobody_explodes/`。
- 注意: `profile.json`から任意Pythonをimportしない。実行処理はコード側で既知IDへ明示登録する。

### `neuro_voice/games/profiles.py` — ACTIVE

- 責任: `game_profiles/<id>/profile.json`の検証、列挙、alias解決、UI-safe summary。
- 主要クラス: `GameProfile`、`GameProfileRegistry`。
- 注意: folder名とIDを一致させ、重複ID・不正IDを起動前に拒否する。

### `neuro_voice/games/session.py` — ACTIVE

- 責任: 選択profileのLocal/Discord共通セッション、KTaNE Expert役割、開始/終了、永続snapshot。
- 主要クラス: `GameProfileSessionManager`。
- 所有状態: persona別`data/game_session_<persona>.json`。
- 注意: KTaNEのExpertは爆弾画面を見ない。足りない情報をLLM推測で埋めない。

## 2. GUIとRemote

### `neuro_voice/ui/webview_app.py` — ACTIVE

- 責任: pywebview GUI、JS API、backend thread/async loop、初期化、設定、event buffer、shutdown。
- 主要クラス: `GuiBackend`、GUI API class、`run_gui`。
- 入力/出力: UI request / JSON-safe response、GUI events。
- 所有状態: loop/thread、pipeline、Discord bridge、Mind、LLM/TTS/STT、event deque。
- 呼び出し元: `app.main`。
- 副作用: model/server起動、config保存、audio/Discord操作。
- 自律研究: `get_research_history` / `get_research_detail`はサニタイズ・永続化済みrecordだけを返す。表示は`ui/assets/index.html`の「こころ → 自律研究・検索履歴」。
- 配線診断: `ui/assets/index.html`の既存 🩺 パネルで実行・表示する。診断項目は項目名・技術キー・説明・層・フラグで画面内検索でき、要注意またはお気に入りだけにも絞り込める。お気に入りは診断キーだけを端末のブラウザ保存領域へ残す。スモークテスト用の4フラグと設定上の`active_persona_id`も、役割の説明・現在値・技術キーを通常の診断行として明示し、`key = value`の完全表記でも検索結果へ含める（診断API・設定・DBは変更しない）。
- 既知の問題: 約2,700行の巨大module、設定永続化責任がConfigと重複。
- 注意: GUI closeではpipeline、Discord、Mind、LLM、owned TTS serverを順序付きで停止する。

### `neuro_voice/remote/launcher.py` — PARTIALLY_ACTIVE

- 責任: desktop appの起動/停止、tray、remote API/PWA lifecycle。
- 所有状態: Launcher process/controller。
- 呼び出し元: `remote_launcher.py`。Windows通常入口は`RemoteLauncher.vbs`からwindow style 0で起動し、`RemoteLauncher.bat`はそこへ委譲する。`RemoteLauncher_Console.bat`だけが明示診断用consoleを持つ。
- 注意: LAN bind、token未設定、localhost例外をセキュリティレビューする。

### `neuro_voice/remote/api.py` — PARTIALLY_ACTIVE

- 責任: remote management HTTP API、session、rate limit、operation tracking。
- 主要クラス: `LauncherApi`、`Sessions`、`RateLimiter`、`Operations`。
- 入力/出力: HTTP/JSON。
- 安全: admin token比較、cookie session、CSP。
- 注意: 認証をGUI convenienceのために弱めない。

### `neuro_voice/remote/app_control.py` — PARTIALLY_ACTIVE

- 責任: 実行中GUIへのlocal control API。
- 主要クラス: `AppControlServer`。
- 副作用: join/leave、persona/speaker/TTS変更、shutdown。
- 安全: `X-Control-Token`。
- 注意: empty token時のbind範囲を確認する。

## 3. Local realtime

### `neuro_voice/pipeline.py` — ACTIVE

- 責任: local audio inputから応答・再生までのend-to-end orchestration。
- 主要クラス: `VoicePipeline`。
- 入力: PCM frame、text input、vision event、autonomy event。
- 出力: LLM response、TTS PCM、GUI event、Mind update。
- 所有状態: `_frame_q`、active response/task/id、cancelled ids、executors、perception/video、autonomy、playback。
- 呼び出し元: `app._amain`、`GuiBackend`。
- 呼び出し先: Mic/VAD/STT、ConversationOrchestrator、Mind、DeepSearch、LLM、TTS、SpeakerPlayback。
- 設定: audio、vad、realtime、barge_in、stt、search、vision、autonomy等。
- 副作用: microphone、speaker、network search、model calls、persona data。
- 既知の問題: 約3,000行、Discordと応答/検索/autonomy意味論が重複。turn-local `sentence_q`はunbounded。
- 変更時の注意: response_id、TurnManager、PlayedTextTracker、LLM cancel、TTS queue、playbackを一体で検証する。

### `neuro_voice/audio/capture.py` — ACTIVE

- 責任: local microphone callbackとdevice open/close。
- 入力/出力: sounddevice / PCM。
- 所有状態: active stream/device。`active`はstream objectの有無でなくbackendの実稼働状態を返す。
- 注意: callbackでblock・LLM・disk I/Oをしない。

### `neuro_voice/audio/playback.py` — ACTIVE

- 責任: local TTS再生、pause/resume/discard、response validity、frame progress callback。
- 主要クラス: `SpeakerPlayback`。
- 所有状態: playing chunk、queue、pause、volume、active generation。
- 復旧: PortAudio write停止時は同じPCM blockを最大3回reopen/retryし、失敗chunkをpartial完了にしてworker自体は継続する。
- 注意: audio callbackとasync controlのlock順、stale PCM、resume位置を守る。

### `neuro_voice/audio/loopback.py` — LEGACY/FALLBACK

- 責任: Windows output loopback capture。
- 現状: Discord direct DAVEの主経路ではない。診断/互換用。
- 注意: speaker名の部分一致をidentityとみなさない。endpoint IDを使う。

### `neuro_voice/audio/devices.py` — ACTIVE

- 責任: 起動後に接続された音声デバイスの検出（`AudioDeviceMonitor`）。
- 注意: identityはindexでなく**名前**で比較する（indexは抜き差しで振り直される）。PortAudioの`terminate/initialize`は全streamを停止するため、通常pollはread-onlyとし、明示refreshは停止検知後のidle recoveryだけで行う。

### `neuro_voice/vad/silero.py` — ACTIVE

- 責任: speech probability、segment boundary。
- 入力/出力: PCM / VAD probability/state。
- 注意: threshold変更は短い相槌、咳、Discord圧縮音声を含む回帰が必要。

## 4. STT、話者、感情

### `neuro_voice/stt/factory.py` — ACTIVE

- 責任: STTとTranscriptRepairer構築。
- 現状: Gemma audio指定でもcurrent Ollama adapter非対応のためWhisper fallback。
- 注意: backend名だけでnative audio対応済みと報告しない。

### `neuro_voice/stt/faster_whisper_stt.py` — ACTIVE

- 責任: faster-whisper inference、partial/final beam、language/hotwords/corrections、confidence。
- 入力/出力: speech PCM / transcript metadata。
- 設定: `stt.*`。
- 注意: beam増加だけで固有名詞問題を解決しない。partialとfinalの予算を分ける。

### `neuro_voice/stt/transcript.py` — ACTIVE

- 責任: 認識結果を確信度付きで保持する型（`Transcript` / `WordConfidence` / `detailed_transcribe`）。
- 注意: 書き起こしは確定した文字ではなく仮説。確信度を捨てて素の文字列を下流へ渡さない。

### `neuro_voice/stt/transcript_repair.py` — ACTIVE

- 責任: context vocabularyとconfidenceを使う保守的な同音異義語修復。復号ループ（吃音）の圧縮。三個以上のdecode bias語だけからなる短い末尾echoの除去。
- 所有状態: `ContextVocabulary` は `bias_terms`（認識へ渡す）と `repair_terms`（後処理のみ）に**分離**されている。
- 注意: ユーザー訂正を無条件の文字置換として学習しない。**書き起こし由来の語、登録話者全員、終了済みActivityを `bias_terms` へ渡さない**（前回の誤認識やhotword一覧が次へ挿入される自己強化ループになる）。日本語の語抽出は字種で分けること（1文字クラスだと文全体が1語になる）。一個の名前は末尾echoとみなさない。

### `neuro_voice/stt/hallucination.py` — ACTIVE

- 責任: Whisperで既知の動画末尾定型句を、発話時間とconfidence根拠から保守的に棄却する。
- 注意: 文字列だけのblacklistにしない。正当な短い挨拶や感謝を巻き込まない。

### `neuro_voice/vad/segmentation.py` — ACTIVE

- 責任: 発話の開始位置判定。pre_roll内の本当に無音な先頭だけを削る純粋関数。
- 注意: 開始しきい値に届かない「うん」「えーと」を無音扱いで捨てない（`onset_threshold`）。

### `neuro_voice/utils/errors.py` — ACTIVE

- 責任: 例外を人へ見せられる短い文字列にする（`safe_error_text`）。TTS境界の`safe_exception_summary`は例外型と有効なHTTP statusだけを返す。URLと長いパーセント列を除去。
- 注意: TTS系のUI/ログ通知は必ず安全なsummaryを通す。requestsの例外には発話本文入りURL、query、response bodyが含まれ得るため、例外文字列や全文発話をログへ渡さない。

### `neuro_voice/mind/speakers.py` — ACTIVE

- 責任: voiceprint registry、alias、identity merge、speaker matching。
- 所有状態: persona別speaker JSON。
- 注意: Discord account IDと実話者identityを分離する。誤mergeは関係・記憶汚染になる。

### `neuro_voice/emotion/recognizer.py` — PARTIALLY_ACTIVE

- 責任: OpenSMILE featuresとSpeechBrain emotion confidence。
- 現状: optional dependency/model失敗時は警告して無効化。
- 注意: Whisperへ感情責任を戻さない。低confidenceを口調へ強反映しない。

## 5. 会話制御

### `neuro_voice/dialogue/orchestrator.py` — ACTIVE

- 責任: final speech eventに対するrespond/brief/ignore/silence等の会話参加判断。
- 主要クラス: `ConversationOrchestrator`。
- 入力/出力: `ConversationEvent` / decision。
- 所有状態: event busと`ConversationState`。終了相槌では会話のfloorを閉じる。
- 注意: 一対一とgroup floor、assistant call、follow-upを別testで守る。

### `neuro_voice/dialogue/turn_closure.py` — ACTIVE

- 責任: AI発話後の短いユーザー発話を`CONTINUE / SILENCE / BRIEF_ACK`へ保守的に分類する。
- 主要クラス: `TurnClosurePolicy`、`TurnClosureDecision`。
- 入力/出力: final text・addressing・ConversationState / disposition。
- 注意: LLMやI/Oを呼ばない。`ほんとそれ / マジでそう`等の内容を追加しない口語同意は閉じるが、質問、訂正、依頼、実行確認、Activity入力を沈黙へ誤分類しない。

### `neuro_voice/dialogue/echo.py` — ACTIVE

- 責任: 直前assistant発話・現在のユーザー発話・割り込み保留に対する生成冒頭の反復を、表示/TTS前に検出する。
- 主要クラス: `ReplyEchoGuard`、`EchoSource`、`EchoMatch`。純粋関数は`containment`、`is_echo`。
- 注意: 通常は最初のTTS文だけを保留する。最初が比較不能な短い笑い・相槌なら最大二文まで保留し、後続本文が重複なら全体を抑止、新内容なら保持分を解放する。assistant自己反復とuserオウム返しは別閾値で、短い定型句だけでは確定しない。全文生成待ちはしない。

### `neuro_voice/dialogue/planner.py` — ACTIVE

- 責任: addressingとturn closureから、LLMを呼ぶか、応答長、役割、direct短文を決める。
- 主要クラス: `ResponsePlanner`、`ResponsePlan`。
- 注意: `should_respond=false`は正常な会話行為であり、生成失敗ではない。

### `neuro_voice/dialogue/state.py` — ACTIVE

- 責任: 直近話者、会話floor、話題、直前assistant発話、Activity有効状態を保持する。
- 主要クラス: `ConversationState`。
- 注意: 終了相槌を新しい話題として残さず、`settle_exchange()`で直前の意味ある話題を復元する。

### `neuro_voice/dialogue/kernel.py` — ACTIVE

- 責任: conversation meaning、planner、working memory、activity、factsの統合。
- 主要クラス: `ConversationKernel`。
- 入力/出力: turn context / structured planning・grounding。
- 既知の問題: 約900行、責任が広い。

### `neuro_voice/dialogue/repair.py` — ACTIVE

- 責任: 明示的な意味訂正を構造化し、置換される解釈、訂正事実、LLM内部文脈を提供する。通常の重複抑止後は固定謝罪でなく、一回再生成用contextと`NO_REPLY`正規化を提供する。
- 注意: 曖昧な否定をすべて訂正とみなさない。訂正は検索より優先し、無効になった割り込み続きを再開しない。
- 注意: prompt生成とcanonical commitを混同しない。

### `neuro_voice/dialogue/directive.py` — ACTIVE

- 責任: 継続依頼の型と状態。ExecutionMode / InteractionMode / PlanProposal / BehaviorDirective / SegmentAction / SegmentLoopDetector / DirectiveStore。
- 所有状態: BehaviorDirectiveのstatus、state_version、進捗、停止条件。
- 注意: **LLM呼び出しとI/Oを入れない**。純粋な状態に保つことでテスト可能性を担保している。

### `neuro_voice/dialogue/hesitation.py` — ACTIVE

- 責任: そのターンに**実際に**不確かな点があるかを、既存の信号（聞き取り確信度、参照解決、記憶ヒット数、検索判定）から確定し、言い淀みの許可量を決める。
- 所有状態: なし（純粋関数と値オブジェクトのみ）。
- 注意: **確信している時は明示的に禁止文を返す**。無言だと前ターンの許可がモデルへ残る。許可文へ「えっと」等の具体例を書かない（口癖化する）。推測に基づく信号を足さない（第1条の偽装禁止に反する）。D-025。

### `neuro_voice/tts/delivery.py` — ACTIVE

- 責任: 本文中の`「……」`と言い淀み語を読み取り、無音の長さと話速倍率を持つ`SpokenPiece`列へ変換する。
- 所有状態: なし。
- 注意: **書かれていない間を作らない**。「文が長いから間を入れる」といった生成的規則を足さない。キャリアが普通の日本語なので、内部タグの剥がし忘れによるTTS漏洩が構造的に起きない（第1条 1.4）。

### `neuro_voice/dialogue/intent_plan.py` — ACTIVE

- 責任: 継続依頼の指名、追加指示の分類、計画・区間・修正のプロンプト構築、素の依頼の決定的計画（`plan_from_request`）。
- 注意: Activity名はtopicであり開始命令ではない。`is_activity_start_request()`はラジオ・実況・TRPG等の名称に加え、assistantへ向いた明示的な開始行為を要求する。「前にTRPGをやったの覚えてる」「ラジオはどう思う」等の履歴・情報・意見質問からplanを作らない。裸の「話してて／何か話して」は依頼として文末にある場合だけ指名する。`is_stop_request()`は開始判定より先に適用し、停止文から新規planを作らない。`_MODE_GUIDE`は既定であって法律ではない。

### `neuro_voice/dialogue/continuation.py` — ACTIVE

- 責任: すべてのBehaviorDirectiveを実行する汎用Controller。区間の順序、stale破棄、上限、ループ検出、状態遷移。
- 所有状態: DirectiveStore、区間履歴、ループ検出器、保留イベント。
- 注意: ラジオ専用/実況専用クラスを作らない。差はモデルが書いたフィールドのみ。

### `neuro_voice/dialogue/directive_runtime.py` — ACTIVE

- 責任: LocalとDiscordで共通の実行ランタイム。`DirectiveHost`（speak / floor_busy / playback_ahead_s / run_tool / ready / cancel_generation / discard_pending_audio / on_user_stop）でサーフェス差分を注入。
- 注意: 判断はMind所有のControllerが持つ。ここへ判断を複製しない（両サーフェスがドリフトする）。明示停止時はactive状態だけでなくpending plan、segment、generation、queued audioを一括取消する。

### `neuro_voice/dialogue/working_memory.py` — ACTIVE

- 責任: 現在の興味、目的、未解決疑問、継続思考、編集可能なpersona会話軸の正規化。
- 所有状態: turn間working state。
- 注意: 曖昧な過去参照を自発発話へ直接流さない。人格軸は発話の確定文でなく重みであり、未設定personaは既定値で補完する。

### `neuro_voice/dialogue/conversation_planner.py` — ACTIVE

- 責任: 今回の返答方針、長さ、質問、提案、ユーモア、意見等を決定。
- 所有状態: persona別`last_plan`。各planは`conversation_move`、`response_id`、`source`、recall groundingを持つ。
- 注意: planner結果をfinal textとして読み上げない。質問は`ASK_QUESTION` Moveの時だけ許可する。`playfulness`、`cheekiness`、`spontaneity`は複数style/shapeへ寄与するが、repeat penaltyと深刻場面guardを上書きしない。永続`response_length`はsoft biasであり、一度の「詳しく」を全turnの`long`へ固定しない。候補比較はPython内で完了させ、棄却候補・scoreをpromptへ再送しない。

### `neuro_voice/dialogue/selection_kernel.py` — ACTIVE

- 責任: 一度のLLM生成前に、直接回答・短い反応・意見・遊び・話題展開・質問のMove候補を比較して一つ選ぶ。
- 入力/出力: intent、momentum、group、直近Move、学習重み / `SelectionResult`。
- 注意: 返答本文を候補ごとに生成しない。訂正・支援・履歴想起のhard obligationをsoft scoreで逆転しない。scoreはprompt/TTSへ出さない。

### `neuro_voice/dialogue/reaction_learning.py` — ACTIVE

- 責任: 次のユーザー発話を、直前Moveへの明示肯定/否定、反復指摘、笑い、継続、終了相槌として保守的に分類する。
- 所有状態: なし。更新値は`AdaptiveConversationStore`の`move:<MOVE>`へ保存。
- 注意: 割り込み・沈黙だけを不評にしない。更新幅と重みをboundedに保ち、group本文を保存しない。

### `neuro_voice/dialogue/semantic_graph.py` — ACTIVE

- 責任: 短いtopic nodeと`co_occurs` / `corrected_to` edgeをpersona別に保持し、話題展開候補を返す。
- 所有状態: `adaptive_<persona>.db`の`semantic_graph_nodes/edges`。
- 注意: graphは事実DBではない。directとgroup session scopeを混ぜず、保存不可turnを入れない。

### `neuro_voice/dialogue/expression_plan.py` — ACTIVE

- 責任: voice表現と将来avatar表現を同じresponse IDで表す。
- 現状: voice deliveryはStyleManager/TTSへ接続。avatarは`NullExpressionSink`のみで実行しない。
- 注意: 内部labelやgestureを本文・TTSへ混ぜない。avatar接続時もgeneration guardを維持する。

### `neuro_voice/dialogue/audience.py` — EXPERIMENTAL

- 責任: direct/groupと将来broadcast comment入力の境界型。
- 現状: `BROADCAST`と`BroadcastCommentClusterer` protocolのみ。配信comment readerは未接続。
- 注意: 現在の一対一・Discord group品質より先に外部配信入力を有効化しない。

### `neuro_voice/dialogue/conversation_contract.py` — ACTIVE

- 責任: Plannerが選んだ主Conversation Move、質問可否、過去会話の取得根拠、自己経験主張を決定的に検証する。
- 主要クラス: `ConversationContract`、`ContractResult`。
- 入力/出力: plan snapshot + TTSサイズ文またはfinal reply / canonical text + reason code。
- 注意: 同じ`response_id`・surfaceの契約だけを適用し、stale planを次turnへ持ち越さない。会話本文はログに出さずreason codeだけを記録する。現在知覚・否定形・明示的なfictionを過去経験として誤遮断しない。

### `neuro_voice/dialogue/surface_realizer.py` — ACTIVE

- 責任: Plannerが決めた「何を話すか」を自然な話し言葉へ変換する最終規則。
- 注意: 子供っぽさを幼児語・固定語尾・毎回の決まり文句にしない。生意気さは親密度と会話温度で抑制し、一発言内で一度までにする。`recent_reply_avoidance`は直近返答の冒頭だけをboundedなturn-local材料にする実験機能。本文を別store/logへ複製せず、既定OFFを維持し、グループpromptへは注入しない。

### `neuro_voice/dialogue/feature_engines.py` — ACTIVE

- 責任: ユーモア、雑談、想像、物語等の低遅延な候補生成と安全gate。
- 注意: HumorEngineは現在の発話にある具体点を材料にし、support/correction/低気分/高リスクでは候補を出さない。inside referenceは確かな記憶がある時だけ使う。

### `neuro_voice/dialogue/conversation_critic.py` — ACTIVE

- 責任: template反復、類似、話題展開等の軽量評価。
- 注意: criticの再生成loopへ上限を持たせる。

### `neuro_voice/dialogue/response_director.py` — ACTIVE

- 責任: observableな発話種別から今回の応答shapeを決め、Agreement Gateと独立した社会姿勢を一度だけ付与する。
- 注意: 温かさを自動同意へ変換せず、関係性質問でも実際の関係状態より好意・称賛・親密さを盛らない。追加LLMは呼ばない。

### `neuro_voice/dialogue/continuation.py` — ACTIVE

- 責任: 継続依頼、OpenThread、復帰条件、expiry。
- 主要クラス: `ContinuationController`。
- 所有状態: Mind配下のsurface共有continuation。
- 注意: vague memoryを具体的なOpenThreadと混同しない。

### `neuro_voice/dialogue/directive.py` / `directive_runtime.py` — ACTIVE

- 責任: 長いラジオ、実況、共同作業等のBehaviorDirectiveとsegment実行。
- 所有状態: directive store/runtime task、pending segment。
- 注意: human speech、surface leave、generation cancelで必ず停止/保留する。

### `neuro_voice/dialogue/intelligence.py`、`adaptive_store.py` — ACTIVE

- 責任: conversation features、Move反応学習、semantic graph、expression plan、adaptive records、turn traces。
- 永続化: persona別SQLite/JSON。
- 注意: 「成長」が前景LLM開始をblockしないようdeferする。対話promptには選択済み方針だけを渡し、同じpersona軸・Agreement・候補を複数blockで重複させない。

### `neuro_voice/dialogue/plan_metrics.py` — ACTIVE

- 責任: 返答本文を保存せず、opening/ending hash、質問率、planned length、style/shape分布、plan adherenceを測る。
- 注意: `planned_length_distribution`で`long`固定を観測する。自動A/Bは`tools/check_reply_avoidance_ab.py`を使い、実マイク会話の受入とは区別する。

## 6. Mindと永続状態

### `neuro_voice/memory/conversation.py` — ACTIVE

- 責任: surface別の短期会話履歴、割り込み保留、通常turnと自発turnのmessage構築。
- 主要クラス: `ConversationManager`。
- 注意: `messages_for_autonomous_turn()`の内部指示はそのgenerationだけに渡し、human utteranceとして保存しない。実際に再生した自発本文だけを`add_assistant()`でcommitし、次の人間発話との連続性を保つ。明示訂正時は無効なdeferred topicを捨て、遮られたassistant本文は直後のecho検知証拠としてだけ有界保持する。

### `neuro_voice/mind/mind.py` — ACTIVE

- 責任: persona-scoped memory、personality、relationship、time、activity、research、speaker、conversation kernelのfacade/lifecycle。persona初期化/切替は同じStore factoryを使い、保存volume healthを共有statusへ載せる。
- 主要クラス: `Mind`。
- 入力/出力: turn/event / context、state update、status。
- 所有状態: 各store/engine、mind/recall executors、reflection task、async lock。
- 呼び出し元: local、Discord、GUI。
- 注意: 通常のsemantic transcript想起は、重複ユーザー発話を一件へ畳み、過去assistant文面をpromptへ入れない。明示的な会話履歴質問・文脈参照では事実照合のため両者の発話を利用できる。`directive_waiting_for_user()`は真のターン制interactionだけを返し、一般DIALOGUEをTurn Closureの迂回路にしない。
- 副作用: persona DB/JSON、background reflection/research。
- 既知の問題: 約1,900行の巨大class、例外隔離が経路ごとに不均一。
- 注意: close順、persona切替、statusのJSON safety、前景deadlineを守る。

### `neuro_voice/mind/store.py` — ACTIVE

- 責任: `memories`と`transcripts` SQLite、canonical sourceの非削除保持policy、理由付き明示削除、write health、canonical/data/backup/sidecar所在volumeのcontent-free容量health、再構築可能なFTS5/ANN sidecar。
- 入力/出力: memory/transcript records。
- 所有状態: sqlite connection + thread lock、process-local read-only state、`*.search-index` connection/signature、容量警告閾値と測定target（容量値自体はstatus取得時に再測定）。
- schema: text/kind/importance/embedding/source/time/access、transcript。
- 既知の問題: privacy owner/audience/scope/confidence/provenance列が不足、明示schema versionなし。
- 注意: schema変更にはmigration、backup、旧recordのdeny-by-defaultが必要。派生indexはACL正本ではないため、取得後のcanonical行でもpersona/scopeを再検査する。容量`warning`/`unknown`は診断であり、自動削除やwrite設定変更の命令にしない。2026-09-10の本番昇格は別承認・online backup・隔離copy検証・canonical指紋照合後に完了した。

### `neuro_voice/privacy.py` — ACTIVE

- 責任: rule-first sensitivity/visibility classification、retrieval gate、output guard。
- 主要クラス: `PrivacyManager`。
- 注意: regexだけを完全なPII検知とみなさない。config duplicateの影響を受ける。

### `neuro_voice/mind/backup.py` — ACTIVE

- 責任: database snapshotと自動backup。
- 既知の問題: Windows file lockでtemp snapshot unlinkが失敗した実行例がある。
- 注意: open handleを閉じてからunlinkし、cleanup retryをboundedにする。

## 7. 自律行動と研究

### `neuro_voice/autonomy/engine.py` — ACTIVE

- 責任: event、AgentState、candidate action、utility、gates。
- 主要クラス: `AutonomousActionSystem`。
- 所有状態: surface runtime AgentState、cooldown/open thread references、post-stop quiet期限、使用済みfocus、initiative move・質問budget。
- 注意: 継続トークの停止は`suppress_speech()`へ渡し、continuation消去とquietを同時に行う。quiet中はsilence/open-thread起点の発話を作らない。
- 既知の問題: local/Discordに別instance。
- 注意: `DO_NOTHING`をfailure扱いしない。同じfocusを再利用せず、材料を使い切ったら黙る。過去open-thread callbackは既定OFFで、明示継続と混同しない。自発質問は現在focusとbudgetがある時だけ許可する。

### `neuro_voice/autonomy/heartbeat.py` — ACTIVE

- 責任: periodic wakeupと評価callback。
- 主要クラス: `AutonomyHeartbeatScheduler`。
- 所有状態: one asyncio task、tick metadata。
- 注意: heartbeat自身がtopicを発明、LLM、TTSを直接実行しない。自発発話OFFでもschedulerは停止せず、発話評価だけを見送って研究保守を継続する。Local suspendはDiscordへの共有Mind tick所有権移譲であり、単なる発話OFFとは別である。

### `neuro_voice/research/service.py` — ACTIVE

- 責任: question gate、quota、queue、single worker、search、Evidence/Knowledge/Reflection、公開persona興味からのbounded seed。
- 主要クラス: `AutonomousResearchService`。
- 所有状態: queue/queued set、worker task、active ID、seed cooldown/reason、metrics。
- 副作用: web search、persona research DB。
- 注意: raw transcriptをqueryへ渡さない。Heartbeatは候補を発明せず、`Mind`が公開persona設定だけを渡す。一対一の知識gapは関係・感情質問を除外する。高リスクapprovalとprompt injection rejectionを維持する。

### `neuro_voice/research/store.py` — ACTIVE

- 責任: research SQLite schemaと履歴/detail/retention。
- schema: questions、runs、evidence、knowledge、reflections、interests、preferences。
- 注意: Evidenceとactive Knowledgeを混ぜない。

### `neuro_voice/utils/emotion.py` / `utils/textseg.py` — ACTIVE

- 責任: legacy感情/style metadataと内部思考の発話漏れを除去し、日本語音声へ混ざる限定的な英語下書き語を正規化する。
- 注意: `[emotion: <unknown>]`も本文へ通さないが、一般の`[Python]`等は保持する。製品名・技術語を一般英語として翻訳しない。表現metadataの正本は`UnifiedExpressionPlan`。

### `neuro_voice/search/deepsearch.py` / `search/intent.py` — ACTIVE

- 責任: query実行、article-main優先HTML抽出、検索意図の共通判定。
- 注意: `main`/`article`/MediaWiki本文rootを優先し、generic fallbackは見出し・段落・定義本文を使う。global navの`li`で本文上限を埋めない。void tagをframe stackへ積まず、malformed closing tagは一致frameまで安全にunwindする。短い「何？」、過去会話の参照、音楽操作を検索へ誤routingしない。

### `neuro_voice/cognition/search_result_presenter.py` — ACTIVE

- 責任: untrusted検索結果の再正規化、navigation boilerplate棄却、Evidence hash/source整合、決定的BRIEF/ENDING提示、Local/Discord共通のOutcome再利用。
- 注意: BRIEFは支持された完全な事実文1〜2文・190文字以下。ENDINGはprimary relevant resultと同一resultの有界支持本文からactual outcome cue付きspanだけを返し、`結末が有名/紹介`等のmeta言及や別resultを使わない。なければ不足を明示する。支持本文をLLM、UI result、Trace、永続Memoryへ出さず、追質問で検索/Toolを再実行しない。

## 8. Activityとゲーム

### `neuro_voice/activity/engine.py` — ACTIVE

- 責任: canonical activity session、proposal/validation/commit、correction、ledger。
- 主要クラス: `ActivityStateManager`。
- 所有状態: persona activity snapshot/session/version/history。
- 注意: AI発話より先にvalidationし、失敗時に誤状態をcommitしない。

### `neuro_voice/games/minecraft_knowledge.py` — PARTIALLY_ACTIVE

- 責任: Minecraft recipe/攻略knowledge sync、安定攻略質問の分類、材料/出力からのlookup、公式調理回答、correction validation。
- 副作用: local DB、official data fetch。
- 注意: edition/versionを固定し、会話訂正を検証せずFact化しない。現在画面を見なくても確定するrecipe質問はvisionより先に扱うが、公式JARで一意に確認できない内容を直接回答へ昇格しない。

## 9. Vision

### `neuro_voice/vision/obs.py` — PARTIALLY_ACTIVE

- 責任: obs-websocket v5 auth、source list、`GetSourceScreenshot`。
- 主要クラス: `ObsWebSocketClient`、`ObsCapture`。
- 所有状態: websocket、selected source、request sequence。
- 注意: stale cached desktop/game frameへsilent fallbackしない。passwordをログへ出さない。

### `neuro_voice/vision/video.py` — PARTIALLY_ACTIVE

- 責任: capture loop、latest-frame-wins queue、raw short history、adaptive background analysis、explicit current frame。
- 主要クラス: `VideoObservationService`。
- 所有状態: capture/background tasks、bounded deques、metrics。
- 注意: capture taskをmodel inferenceでblockしない。priority request時は古いbackground inferenceをcancelする。

### `neuro_voice/vision/service.py` — PARTIALLY_ACTIVE

- 責任: multimodal LLM request、PerceptionMemory、current scene context、cache/stale age、Minecraftの現在行動/次の一手を含むstructured observation。
- 主要クラス: `VisualAnalyzer`、`PerceptionService`。
- 注意: previous observationをcurrent frameとして断定しない。

### `neuro_voice/vision/discord_share.py` — ACTIVE / OBS transport

- 責任: Local/Discord両方からの画面共有開始・停止指示、Discord共有/OBSゲームsource切替、初回frame preview、現在画面質問と安定攻略知識の分離、質問時frame世代固定、状況/行動/一手を組み立てるfresh direct answer、background observation callback、lifecycle。
- 主要クラス: `DiscordScreenShareSession`、`ScreenShareControl`。
- 所有状態: source kind/name、requester、source別game profile/capture width、session-local video config、PerceptionService、VideoObservationService。
- 注意: DAVE sidecarの音声経路とは独立。Discord共有は汎用1280px、Minecraft OBSはMinecraft profile/768pxを既定とする。共有source未設定時にゲームsourceへ暗黙fallbackしない。active中は通常monitor captureへfallbackしない。cold/background解析は会話timeoutでcancelしないが、明示的な「今」の質問は旧解析をcancelし、質問時frameを解析する。UI previewは初回frame、または`explicit_request_id == response_id`の質問時frameだけを送信し、Local/Discordの通常message buildから`latest_frame`を再送しない。取得失敗時は前回の明示frameを破棄する。Minecraftのrecipe/how-toは公式knowledge laneへ渡し、`どうすれば`だけでfresh visionへ送らない。fresh結果は二重LLM生成せず直接発話し、固定の「撮り直した画面だと」を付けない。Minecraft background観測はGameCompanionDirectorへ渡すが、generic Discord共有は自動実況しない。raw imageを会話履歴へ保存せず、Local shutdownまたはVC退出時に必ず停止する。将来のdirect videoはこのtransport境界で差し替える。

## 10. LLM、TTS、再生

### `neuro_voice/llm/context_budget.py` — ACTIVE

- 責任: 送信前にトークン量を見積もり、出力枠（`context_reserve_tokens`）を確保して古い往復を落とす。
- 注意: **systemメッセージは絶対に落とさない**（人格・Kernel判断・検索根拠。落ちると答える人格が変わる）。予約は`max_tokens`へ自動追従させる（固定値だと`max_tokens`変更時に応答が途中で切れる）。

### `neuro_voice/llm/factory.py` / `openai_compat.py` — ACTIVE

- 責任: selected backend、streaming、Ollama options、vision、cancel、unload/fallback。
- 所有状態: model/backend/request tracking。
- 注意: internal thoughtをsurface text/TTSへ漏らさない。runner crash fallback時は実model名をUIへ反映する。

### `neuro_voice/tts/factory.py` — ACTIVE

- 責任: `audio_output.backend`優先でVOICEVOX/SBV2/Qwen3/Nullを構築。
- 注意: `tts.backend`はlegacy compatibility。persona別speaker/modelを同期する。

### `neuro_voice/tts/style_bert_vits2.py` — ACTIVE

- 責任: SBV2 synthesis、style/weight、pronunciation。
- 注意: first request startup、server lifecycle、GPU競合、voice selectionをGUIと一致させる。単一styleモデルは`lock_single_style_weight`で基準weightを固定し、複数styleモデルだけに感情強度倍率を掛ける。

### `neuro_voice/utils/textseg.py` — ACTIVE

- 責任: LLM streamからUI/TTSへ安全な文を取り出し、内部thought/tagを除去し、音声chunk境界を決める。
- 注意: 句点はhard boundary、読点は20文字以上のsoft boundary。短い読点節を独立合成するとSBV2の抑揚が毎回閉じるため、低遅延と語句のまとまりを同時に守る。Local/Discordの片側だけで境界を変えない。

### `neuro_voice/tts/voicevox.py` — ACTIVE backend / 非選択時待機

- 責任: VOICEVOX query/synthesis/self-heal。
- 注意: switch後に旧server/旧speaker表示を残さない。

## 11. Discord

### `neuro_voice/discord_bridge/bot.py` — ACTIVE

- 責任: Discord client、join/leave/member、direct/loopback receive、VAD/STT、conversation、autonomy、music、TTS/playback。
- 主要クラス: `DiscordBridge`、`QueueAudioSource`。
- 注意: finalized inputには単調epochを付け、古いqueued/STT/LLM taskが新しい要求へ応答しないよう失効する。direct DAVEの音楽/TTSは2本のPCM writerを使わず、`MixedAudioSource`でmusicを70%へduckする。退出はsidecarの`left`確認が成功するまでUI成功にしない。
- 所有状態: VC、utterance queue、per-account segments、active response、turn tasks、playback/music、autonomy。
- 既知の問題: 約3,900行、local pipelineと意味論が重複。
- 注意: join/leave、sidecar reconnect、human floor、response_id、text echo、music intentを横断testする。

### `neuro_voice/discord_bridge/direct_receiver.py` — ACTIVE

- 責任: Node sidecar IPC、event/audio receive、direct TTS playback、recovery。
- 注意: websocket disconnectのtask回収と再接続、sidecar process ownershipを明示する。

### `discord_receiver/receiver.js` — ACTIVE

- 責任: Discord voice/DAVE receive、Opus decode、account stream、VAD/PCM event、TTS encode/playback。
- 入力/出力: Discord RTP/Opus ↔ IPC message。
- 注意: Node/Python protocol versionを同時に変更する。account IDを人間identityとみなさない。

### `neuro_voice/discord_bridge/music.py` — ACTIVE

- 責任: search/play/queue/volume/stopと音声mix。
- 注意: keywordだけで操作せず、conversation intentで確定する。queue clearを独立commandとして扱う。

## 12. Tests

- `tests/test_interaction.py`: 会話・割り込み・routingの大規模回帰。
- `tests/test_behavior_directive.py`、`test_directive_runtime.py`: 継続行動。
- `tests/test_activity_*`: Activity/transaction/grounding。
- `tests/test_transcript_repair.py`: STT修復。
- その他、privacy、research、vision、Discord、playbackの個別testが存在する。

`CURRENT`: 調査時のPython runtimeが壊れていたため、全suiteの実行結果は`UNKNOWN`。テストが存在することと通ることを混同しない。

## 13. Legacy・Experimental一覧

| 対象 | 状態 | 削除条件 |
|---|---|---|
| `proactive.enabled`旧random loop | LEGACY / default OFF | 新autonomyの長期回帰と設定migration完了 |
| `legacy_spontaneous_mode` | LEGACY / OFF | 保存data互換とUI参照の除去 |
| Discord loopback receive | FALLBACK | direct DAVEの環境要件・復旧手順が確立し、ユーザー承認後 |
| direct screen/window game capture | EXPERIMENTAL | OBS以外を正式supportする実機回帰が揃うまで |
| Gemma native audio selector | EXPERIMENTAL/UNAVAILABLE | runner/APIがnative audioを正式提供するまで |
| Qwen3-TTS | EXPERIMENTAL backend | lifecycle、voice、latency、GPU test完了 |
| 入れ子`AItuber/` repo | OUTDATED SNAPSHOT | 正本確認と履歴保全後 |

## 14. 横断変更チェック

- 音声変更: localとDiscord、TurnManager、PlayedTextTracker、TTS、playback。
- 会話変更: Orchestrator、Kernel、Mind、Privacy、Search intent、group floor。
- Memory変更: persona separation、ACL、migration、backup、forget。
- Autonomy変更: local/Discord両instance、heartbeat、Directive、human priority。
- Activity変更: proposal/validate/commit、correction、TTS grounding。
- Vision変更: OBS current frame、background cancel、GPU priority、stale age。
- Config変更: GUI persistence、factory priority、runtime env override、duplicate key。
### `neuro_voice/cognition/async_trace_writer.py` — ACTIVE (Phase 7D)

- Responsibility: CWD-independent, bounded background writer for privacy-safe Cognitive Trace JSONL.
- Current wiring: Local and Discord normal conversation each enqueue exactly one final turn Trace; same-process append is serialized. Writer is non-blocking and does not fsync per turn.

### `neuro_voice/utils/latency.py` / `neuro_voice/cognition/turn_tracker.py` — ACTIVE (Phase 7D)

- Responsibility: one `TurnMetrics` object travels through Local/Discord real paths; `LatencyWindow` reports source-separated p50/p90/p95. `TurnTracker` counts only safe delivery-stage attempts/accepts/rejections for `SpeechRequest → TTS job → Playback`.
- Current wiring: Local and Discord attach the final metrics and delivery snapshot to Cognitive Trace. Conversation text, prompt text, raw audio, and commit IDs are excluded.
- Entry points: `VoicePipeline._trace_writer()` at startup, `VoicePipeline._emit_legacy_turn_trace()` after local Mind context, and cognitive outcome emit points.
- Output: `<project_root>/logs/cognitive_trace.jsonl`; records metadata only, never waits for fsync on the conversation path.
### `neuro_voice/realtime/context_assembler.py` / `neuro_voice/cognition/recall.py` — ACTIVE (Phase 7D)

- Normal Local context assembly is the retrieval caller. `memory_recall_budget_ms` caps a triggered read but never enables one by itself; only no-LLM explicit past/labelled-recall detection sets `include_recall=true`.
- Cognitive obligation recall uses an active same-persona WorkingMemory gate: explicit resumption, deterministic topic/entity overlap, or a selected continuation action is required. Current-turn response duties never qualify. Due obligations feed the Initiative attention path instead of unrelated answer context.
- `neuro_voice/dialogue/echo.py` classifies only historical textual similarity. `VoicePipeline` and Discord retain an undelivered explicit primary reply when similarity persists; delivery idempotency is owned separately by the SpeechRequest/Playback ledger. Cognitive outcome is successful only after a logical playback session.
- `CognitiveTrace.llm_diagnostics` contains privacy-safe prompt/provider timing metadata only.
### `neuro_voice/cognition/test_session.py` / `neuro_voice/pipeline.py` — ACTIVE (Phase 7D)

- Responsibility: process-local cognition test-session state machine and turn-boundary epoch control.
- Entry points: GUI `認知: ON/OFF` and `python -m neuro_voice.app --cognition-test-session`; neither persists `cognition.enabled`.
- Current wiring: Local `TurnMetrics` and `TurnFrame` freeze effective enabled/mode/execution-path/epoch. Cognitive Trace separately records configured rollout (`disabled`/`test_session`), execution path (`legacy`/`cognitive`), selector/gate, and accepted `SpeechRequest` count. A delayed old-epoch speech request is rejected at the final gate.
### `neuro_voice/mind/episodes.py` / `neuro_voice/mind/store.py` — ACTIVE (Phase 7D)

- Retrieval: strict persona binding applies to keyword and embedding source rows, not only episode APIs.
- Retrieval-only smoke mode: `memory.write_enabled=false` blocks episode mutations, access touches, and transcript persistence while retaining scoped retrieval; missing vectors use the scoped lexical fallback.
- Diagnostics: 🩺 lists the same gate with an explanation, so the test cannot mistake retrieval-only mode for a broken retrieval switch.
- Response contract: explicit recall questions (including ASR-tolerant passphrase variants) exclude stored questions as evidence; recognised labelled facts are emitted deterministically, while `NOT_FOUND` returns a short non-invented reply before TTS and emits `recall_llm_call_count=0`.
- Write provenance: new private episodes persist origin turn, source role/person, persona ID, and persona epoch in metadata.
- Guard: question-shaped user input is not a preference candidate.
- Stage M retention: automatic source pruning is non-destructive; old explicit recalls may include archived records. Normal retrieval uses a bounded rebuildable sidecar, while process history/prompt context remain bounded independently of retained source volume.
