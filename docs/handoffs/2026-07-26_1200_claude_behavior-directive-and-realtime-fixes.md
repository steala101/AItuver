# AI作業Handoff

- 担当AI: Claude (Cowork / claude-opus-5)
- Role: design / implementation / test
- Task: 継続行動基盤 BehaviorDirective の新規実装と、実機フィードバックによる会話・STT・音声デバイスの一連の修正
- Started At: 2026-07-25 (JST)
- Finished At: 2026-07-26 (JST)
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: 実働ルート直接 (R-001のとおりGit未管理)
- Start Commit: なし (Git履歴なし)
- End Commit: なし
- Work Lock: このセッション中は他AIの同時編集なし

## 目的

1. 「ラジオ風にしばらく話して」「終わるまで実況して」等の継続依頼が、1回の返答で終わってしまう問題を解消する。
2. ユーザーの実機利用で見つかった不具合（誤変換、話者の揺れ、応答の途切れ、物語の破綻、デバイス認識）を根本原因から修正する。

## 作業開始時の状態

- Git status: 取得不可（実働ルートに`.git`なし＝既知リスク R-001）
- 既存変更: なし
- 起動中process: ユーザー側でアプリを起動・検証しながら進行
- 読んだ憲法/ADR: セッション後半で `AGENTS.md` / `PROJECT_CONSTITUTION.md` / `SHARED_CHANGELOG.md` / `TEMPLATE.md` を読了。**前半は未読のまま作業しており、これは手順違反（下記「失敗した試行」参照）**

## 調査結果

### 継続依頼が1回で終わる根本原因

- 根拠ファイル/関数: `neuro_voice/pipeline.py::respond_text`
- `respond_text()` はLLM生成1回のあと`finally`で必ずターンを完了していた。継続という状態が存在しなかった。
- 唯一の継続は `autonomy/engine.py` の `_CONTINUATION_REQUEST` 正規表現＋固定3区間ハック。
- state owner: 継続状態の所有者が存在しなかった → `ContinuationController`（Mind所有）を新設。

### 実機ログから特定した原因（`logs/neuro_voice.log`）

| 症状 | ログ根拠 | 原因 |
|---|---|---|
| ラジオが途中で止まる | `Directive finished status=FAILED reason=max_consecutive_errors segments=4` | 区間JSONのパース失敗2回で強制終了。ローカル12Bは時々JSONを外す |
| 初回応答が14.7秒 | `初トークン=1750ms` と `12087ms` が並存 | Ollamaは直列。計画用LLM呼び出しを非同期化しても片方が待つだけだった |
| 応答が文の途中で切れる | `finish=length`（12文字の応答で） | 出力上限ではなく**プロンプトがnum_ctxを埋め尽くし**出力枠が枯渇 |
| 話題が乗っ取られる | `06:18:28 Directive created COLLABORATIVE_SESSION` | TRPG中に別Directiveが新規作成された |

### async/queue/cancel

- 区間の先読み生成は `floor_busy` に自分の再生を含めていたため一度も発動していなかった。人の発話とユーザーターン処理のみをfloorとし、自分の再生はTTS背圧（`pending_seconds`）で制御する形へ変更。
- 割り込みは `state_version` バンプで in-flight 区間とTTSを無効化。DirectiveはCANCELではなくPAUSE（第7条）。

### privacy/data

- 区間・計画へ渡す記憶は PrivacyManager 済みの `Mind` 経由のみ。NARRATIONでは記憶・興味を一切渡さない。
- ログ・文書へ raw audio / raw image / transcript全文を複製していない。

## 変更

| File | Change | Why |
|---|---|---|
| `neuro_voice/dialogue/directive.py` | 新規。ExecutionMode/InteractionMode/PlanProposal/BehaviorDirective/SegmentAction/SegmentLoopDetector/DirectiveStore | 継続依頼を状態として保持する。純粋な状態のみでLLM・I/Oを持たない |
| `neuro_voice/dialogue/intent_plan.py` | 新規。指名・追加指示分類・プロンプト構築・決定的計画 | 依頼の解釈。`plan_from_request`は素の依頼のみ、条件付きはLLMへ |
| `neuro_voice/dialogue/continuation.py` | 新規。汎用ContinuationController | ラジオ/実況/見守り/共同思考/物語を共通実行。専用エンジンなし |
| `neuro_voice/dialogue/directive_runtime.py` | 新規。DirectiveRuntime + DirectiveHost | Local/Discord共通ランタイム。第3条・R-003対応 |
| `neuro_voice/dialogue/kernel.py` | execution_mode/directive_action/plan_candidate を TurnFrame へ追加。`_EXPLICIT_SEARCH`を`search/intent.py`へ委譲 | 単発/継続の判断をKernelへ一元化。二重の判断器を作らない |
| `neuro_voice/search/intent.py` | 新規。依頼形と過去形の区別 | 「何調べてたの?」が検索命令になり捏造を招いた事故の一本化 |
| `neuro_voice/search/deepsearch.py` | `_EXPLICIT_SEARCH`削除→intent.py参照。`_OWN_ACTIVITY_QUESTION`追加 | 同上（二重定義の解消） |
| `neuro_voice/stt/transcript.py` | 新規。Transcript/WordConfidence/detailed_transcribe | 認識結果を「確定文字」でなく仮説として扱う |
| `neuro_voice/stt/transcript_repair.py` | 新規。ContextVocabulary/TranscriptRepairer/asr_uncertainty_prompt | 同音異義語補正。bias_terms(認識へ)とrepair_terms(後処理のみ)を分離 |
| `neuro_voice/stt/base.py` `factory.py` `faster_whisper_stt.py` | transcribe_detailed / set_context_bias / 語確信度 / 温度フォールバック / keep_fillers | 既存バックエンドは既定実装で無改修 |
| `neuro_voice/llm/context_budget.py` | 新規。トークン予算で履歴を切り詰め | `finish=length`の根治。systemは絶対に落とさない |
| `neuro_voice/llm/openai_compat.py` | finish=length を専用警告 | 観測可能性（第20条） |
| `neuro_voice/vad/segmentation.py` | 新規。語頭の無音判定 | 「えーと」「うん」が無音扱いで捨てられていた |
| `neuro_voice/audio/devices.py` | 新規。AudioDeviceMonitor | 後から接続したデバイスの検出 |
| `neuro_voice/audio/capture.py` | start()が失敗を返す／restart()／active | デバイス不在でもアプリを止めない |
| `neuro_voice/audio/playback.py` | ストリーム生成失敗でスレッドを殺さない／request_reopen／pending_seconds | 再生スレッドの永久停止を防ぐ。背圧の指標を追加 |
| `neuro_voice/utils/errors.py` | 新規。safe_error_text | 例外メッセージのURL/パーセント列が画面を埋めた事故 |
| `neuro_voice/mind/mind.py` | ContinuationController所有／話者統合／speech_recognition_context／session内の話題判定 | 状態所有権（第5条）。Local/Discord共通 |
| `neuro_voice/mind/speakers.py` | ヒステリシス／merge／aliases／merge_candidates | 同一人物が2プロファイルへ分裂していた |
| `neuro_voice/mind/relationship.py` | merge()。摩擦系はmin、温かさ系はmax | 1人になった途端に両方分の摩擦を背負わせない |
| `neuro_voice/dialogue/intelligence.py` | merge_users／`_TOPIC_WORD`を字種分割／保存済み話題の掃除 | 文まるごとが「話題」として永続化されていた |
| `neuro_voice/dialogue/adaptive_store.py` | reassign_user | 学習した好みも統合先へ移す |
| `neuro_voice/autonomy/engine.py` `state.py` | fresh topicのハードコード5種を廃止／OpenThreadに発言原文と経過時間 | 自発発話の単調さと曖昧な掘り返し |
| `neuro_voice/tts/volume.py` | 複合動詞除去。「読み上げ」は音量語を伴う時のみ対象 | 「読み上げてみる」が音量アップと解釈されていた |
| `neuro_voice/memory/persona.py` | 事実性ガードにASR誤変換の例外条項 | ガードが誤変換の literal 解釈を助長していた |
| `neuro_voice/ui/webview_app.py` | TTS/STT/Mindの並行初期化／話者統合API | 起動51秒→28秒 |
| `neuro_voice/ui/assets/index.html` | Directive状態カード／話者統合ボタン・自動提案 | 観測可能性と操作 |
| `neuro_voice/bootstrap.py` | discord系はメタデータで判定 | find_specがdiscordを実import していた |
| `neuro_voice/discord_bridge/bot.py` | Directive配線／文脈予算／区間TTS分割／旧regex継続の停止 | Local/Discord片側だけ変更しない（不変条件） |
| `config/config.yaml` | behavior_directive節／stt.transcript_repair節／num_ctx 8192／context_reserve_tokens auto／speaker統合閾値／device_watch_interval_s／autonomy緩和 | すべてfeature flagで従来動作へ戻せる |
| `SetupOllamaEnv.bat` | OLLAMA_CONTEXT_LENGTH 8192 | configだけではサーバ上限で頭打ち |
| `README.md` | 上記すべての節を追加 | R-015（時点混在）を増やさない |
| `tests/*.py` (新規16ファイル) | 下記テスト節 | 第22条の完了条件 |

## 変更しなかったもの

- `autonomy/engine.py` の `_CONTINUATION_REQUEST` 旧経路は**削除せず**、`behavior_directive.enabled=false` の時のみ動く形で残した（第19条：互換性）。
- 巨大クラス（pipeline.py 2600行超、bot.py 3800行超）の分割は行っていない。R-004は未着手のまま。
- Git初期化・commit・pushはしていない（不変条件）。

## 設計判断

1. **専用エンジンを作らない。** ラジオ／実況／見守り／物語は同一の`BehaviorDirective`。差はモデルが書いたフィールドのみ。
2. **判断はMind、実行はサーフェス。** `ContinuationController`をMindが所有し、Local/Discordは`DirectiveHost`のコールバック（speak/floor_busy/playback_ahead_s/run_tool/ready）だけを提供する。R-003の縮小方向。
3. **モード指示は既定であって法律ではない。** ユーザーの依頼本文・style/content constraintsが優先される。この点は一度誤って断定形で実装し、ユーザー指摘で修正した（第2条「人格表現を固定ルールだけへ押し込めない」）。
4. **ローカルLLMは直列である前提で設計する。** 計画用の追加呼び出しは、素の依頼では規則で代替してゼロにする。非同期化だけでは解決しない。
5. **語彙バイアスは認識を助けるより簡単に壊す。** デコーダへ渡すのは検証済み固有名詞のみ（最大8語）。書き起こし由来の語は絶対に渡さない。

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| `pytest tests/ -q`（sandbox / Python 3.10 + StrEnum shim, 音声系2件除外） | 664 passed, 3 skipped, 11 subtests | 全実行ログ確認 |
| 新規テスト16ファイル | すべてpass | test_behavior_directive(49) / test_directive_runtime(30) / test_transcript_repair(37) / test_context_budget(25) / test_narration_session(28) / test_request_flexibility(19) / test_audio_devices(17) / test_vad_onset(16) / test_speaker_merge(17) / test_search_intent(19) / test_error_display(10) / test_plan_from_request(18) / test_topic_extraction(13) / test_bootstrap_startup(9) / test_speaker_stability(6) / test_transcript_backend(6) |
| 既存テストの回帰 | なし | 開始時373 → 終了時664（増分はすべて新規） |
| `python -m compileall neuro_voice` | exit 0 | — |
| config.yaml のYAML妥当性 | OK | 各変更後に検証 |

- 未実施: **実機での音声・Discord動作検証、GPU/VRAM実測、レイテンシp50/p95の計測**
- 未実施理由: sandboxに音声デバイス・GPU・Ollamaがなく、実機はユーザー環境のみ。ユーザーが逐次検証しフィードバックを返す形で進めた
- Manual: ユーザーによる実機確認を7ラウンド実施。各ラウンドの指摘をログ根拠付きで修正（下記「失敗した試行」）
- Performance p50/p95: 未計測（残課題）

## 設定・Migration

- Config: `behavior_directive`（新規）、`stt.transcript_repair`（新規）、`stt.temperature_fallback/repetition_penalty/keep_fillers`（新規）、`llm.num_ctx` 4096→8192、`llm.context_reserve_tokens` 新規(auto)、`speaker.sticky_*`/`merge_suggestion_threshold`（新規）、`audio.device_watch_interval_s`（新規）、`vad.onset_threshold`/`pre_roll_ms` 320→640、`autonomy` の沈黙・クールダウン緩和
- Environment: `SetupOllamaEnv.bat` の `OLLAMA_CONTEXT_LENGTH` 4096→8192。**再実行と再ログインが必要**
- DB schema: 変更なし
- Data migration: `data/dialogue_*.json` の `users[*].topics` は**読込時に自動掃除**される（文まるごと保存されていた値を破棄）。破壊的だが、対象は元々「話題」ではない値のみ
- Backup: 既存の自動バックアップ機構をそのまま利用。手動バックアップは取得していない

## 失敗した試行

このセッションで**私が原因の不具合を3件出した**。記録として残す。

1. **語彙バイアスで誤認識を増やした。** 会話履歴から拾った語を`initial_prompt`/`hotwords`へ大量投入した結果、Whisperがバイアス語を出力へ挿入し（「さあ、ib ポッポ、ポッポ、チビを…」）、さらに前回の誤認識が次を誤らせる自己強化ループになった。→ bias/repairの語彙分離、`initial_prompt`注入を既定off、hotwords上限8語で解消。
2. **Intent-to-Planの非同期化が無意味だった。** ローカルOllamaは直列なので、並行にしても片方が12秒待つだけ。→ 素の依頼を規則で解釈しモデル呼び出し自体をなくした。
3. **モード指示を断定形でコードへ埋めた。** 「GM兼プレイヤーとして参加して」という正当な依頼と矛盾する状態を作った。ユーザー指摘で既定値へ降格。

いずれも「第2条：決定論的処理をプロンプトだけへ委ねず、人格表現を固定ルールだけへ押し込めない」の後半を踏み外したもの。

**手順違反**: このセッションの大半で `AGENTS.md` / `PROJECT_CONSTITUTION.md` / `SHARED_CHANGELOG.md` を読まずに作業した。ユーザーの明示指示を受けて本handoffで遡って同期している。

## 残課題

1. **実機検証が全面的に未了。** 特に: 区間の先読みが実際に無音を消せているか、`num_ctx 8192` でのVRAMとGPU占有、STT語頭フィラーの保持、デバイス再接続。
2. **長いセッションで筋が失われる。** 文脈予算で切り詰めるため、TRPG等では古い展開が落ちる。セッション要約で補う設計は未実装。
3. **ゲームイベントのDiscord供給がない。** 実況DirectiveはDiscordではイベントを受け取れずWAITのまま。
4. **`plan_from_request` の語彙は日本語の表層に依存。** 想定外の言い回しは指名されない可能性がある。指名は広めにしたが網羅ではない。
5. **話者プロファイルの統合はユーザー操作が必要。** 既存の分裂データは「こころ」画面から1回統合する必要がある。
6. R-004（巨大クラス）は今回さらに肥大した。`pipeline.py`/`bot.py`へDirective配線を足したため。

## Rollback

すべてfeature flagで無効化できる。コード削除は不要。

```yaml
behavior_directive.enabled: false        # 継続行動基盤を止め、旧regex継続へ戻る
stt.transcript_repair.enabled: false     # STT補正を止める
stt.keep_fillers: false                  # フィラー保持を止める
audio.device_watch_interval_s: 0         # デバイス監視を止める
llm.num_ctx: 4096                        # + SetupOllamaEnv.bat も戻す
speaker.sticky_margin: 0.0               # 話者ヒステリシスを止める
```

`llm.context_reserve_tokens` は `auto` のままにすること（固定値にすると`max_tokens`変更時に応答が切れる）。

## 次に触るべきファイル

- 実機検証で問題が出た場合: まず `logs/neuro_voice.log` を `Directive` / `finish=` / `context_trimmed` / `stt_repair` でgrepする。今回の原因特定はすべてログから行えた。
- セッション要約を実装する場合: `neuro_voice/llm/context_budget.py`（切り詰め位置）と `neuro_voice/dialogue/continuation.py`（`progress_summary`）。
- Discordのゲームイベント: `neuro_voice/discord_bridge/bot.py` の `_build_directive_runtime` で `environment_available` が `False` 固定。

## 触らない方がよい箇所

- `neuro_voice/dialogue/directive.py` は純粋な状態に保つこと。LLM呼び出しやI/Oを入れるとテスト不能になる。
- `_MODE_GUIDE` に断定形の禁止事項を足さないこと（上記「失敗した試行」3）。守るべきは「相手のキャラの行動を決めない」の一点のみ。
- `stt/transcript_repair.py` の `bias_terms` に書き起こし由来の語を足さないこと（同1）。

## 憲法・ADRへの影響

- Constitution amendment required: なし
- ADR added/updated: ADRは未作成。`docs/DECISION_LOG.md` へ D-014〜D-017 を追加した（継続行動基盤の設計、判断のMind集約、モード既定の劣後、ローカルLLM直列前提）。ADR化は次担当へ提案
- Architecture/Codemap updated: `docs/CODEMAP.md`（directive/intent_plan/continuation/directive_runtime/transcript/segmentation/devices/errors/context_budget）と `docs/CURRENT_ARCHITECTURE.md`（文脈予算、ターン制の手番）へ反映
- Risks: `docs/KNOWN_RISKS_AND_DEBT.md` へ R-026〜R-029 を追加（実機未検証、長期セッションの筋の喪失、指名の表層依存、巨大classの肥大）

## 秘密・個人情報確認

- `.env`値を記録していない: はい
- raw audio/imageを添付していない: はい
- private transcriptを複製していない: 症状再現に必要な最小限の発話断片のみ引用（「何調べてたの?」等）。会話全文は複製していない

## 次の担当への一文

実機ログ（`logs/neuro_voice.log`）が今回の原因特定をすべて可能にした。挙動が怪しい時は推測せず、まず `Directive` / `finish=` / `初トークン=` をgrepすること。
