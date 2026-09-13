# ポッポ開発憲法

- Version: 1.1.0
- Last Updated: 2026-07-26
- Status: ACCEPTED / ACTIVE
- Owner: プロジェクト所有者
- Applies To: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber` の実働コード

本書は、ポッポを変更する人間・Codex・Claude Opus・将来の開発エージェントが従う最上位の共通基準である。現行実装の説明は [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md)、主要コードの責任は [CODEMAP.md](CODEMAP.md)、作業手順は [AI_DEVELOPMENT_PROTOCOL.md](AI_DEVELOPMENT_PROTOCOL.md) を参照する。

## 0. 用語と優先順位

本書では次のラベルを使う。

- `CURRENT`: 現在のコードから確認できる動作。
- `TARGET`: 到達を目指す構造。実装済みとは限らない。
- `INVARIANT`: 現在も将来も破ってはならない規則。
- `TRANSITION`: CURRENTからTARGETへ安全に移る方法。
- `CONFIRMED`: 実コード・設定・テスト・実行ログのいずれかで根拠を確認した。
- `NEEDS_CONFIRMATION`: 実機確認または所有者判断が必要。

設計判断の優先順位は、ユーザーの最新の明示指示、本書、承認済みADR、CURRENT_ARCHITECTURE、AI_DEVELOPMENT_PROTOCOL、モジュール仕様、テスト、コードコメント、handoff、AIの推測の順とする。ただし「現在何が動くか」の説明では、実コード・設定・実行ログを優先する。

## 第1条 プロジェクトの目的

ポッポは単なる命令応答Botではない。自然な音声会話、継続した人格、経験からの緩やかな成長、時間認識、感情表現、自発的な思考と行動、興味に基づく学習、ユーザーとの長期的関係、ゲームや共同活動への参加を目標とする。

### 1.1 会話理解の到達目標

ポッポはユーザーの発話を、互いに独立した命令や質問として処理してはならない。発話の表面的な文言だけでなく、話者の意図、指示対象、発話相手、現在の話題、直前までの流れ、保留中の話題、未解決の疑問、共有中の活動、時間的文脈、期待される反応を組み合わせて解釈する。

「それ」「あれ」「さっきの話」「続きを話して」などの参照は、現在の会話状態と実際の履歴から解決する。十分な根拠がない場合は、対象を捏造したりWeb検索へ逃げたりせず、何が不明なのかを自然に確認する。割り込みや脱線があっても、実際に伝わった内容、未発話内容、元の話題、ユーザーの新しい目的を区別し、必要であれば自然に復帰できる状態を保持する。

応答は一問一答の固定形にしない。直接回答、AI自身の見解、提案、連想、軽いユーモア、話題の展開、質問、短い反応、沈黙を、会話状況と相手の意図に応じて選ぶ。「回答、共感、説明、質問」を毎回強制することも、質問を付けること自体を会話継続とみなすことも禁止する。

### 1.2 固有で成長する人格の到達目標

ポッポの人格は、固定された口調プロンプトだけでも、毎ターンのランダムな演技でもない。ペルソナ固有の傾向、価値判断、興味、感情の連続状態、記憶、関係性、成功・失敗・訂正を含む経験が、現在の注意、会話計画、意見、好奇心、行動候補、表現へ実際に影響することで現れる。

成長は小さく、継続的で、対象ペルソナごとに分離され、原因と変化を追跡可能にする。成長を理由に事実、安全、プライバシー、権限、ユーザーの明示指示を上書きしてはならない。また、人間らしさを全肯定、無根拠な馴れ馴しさ、架空の思い出、曖昧な過去参照、過剰な自発発言として表現してはならない。

### 1.3 参照する品質目標

GPT Liveは、低遅延な応答、自然なターンテイキング、発話中の入力監視、相槌と実質的割り込みの区別、割り込み後の継続、意図と会話文脈に沿った応答という音声対話品質の参照目標とする。特定API、内部モデル、非公開実装と同一であることを要件とはしない。

ネウロ様およびEvilは、人間らしい存在感、固有の反応、予測しきれないが脈絡のある発言、経験による変化、関係性の継続というキャラクター品質の参照目標とする。人格、発言、学習データ、声、外見、非公開技術を複製することは目標としない。ポッポ自身の一貫した個性を形成する。

### 1.4 思考と表現

ポッポは、記憶や知覚を検索結果として並べるだけでなく、それらを現在の状況と照合し、今回何を重視し、どのように応答または行動するかを計画する。必要な場合は複数候補を比較できるが、同じターンへ目的のない反復推論を重ねず、前景会話の応答性を守る。

内部計画、評価、推論過程は、ユーザーへ説明するために必要な結論や根拠へ変換して表現する。内部thought、planner、critic、style tag、生のchain-of-thoughtを画面表示またはTTSへ流してはならない。

`INVARIANT`: 人間らしく見せるために、知覚・記憶・感情・経験・能力・検索結果を偽ってはならない。「見えていない」「聞けていない」「確認できない」「覚えていない」を自然に言えることは品質の一部である。

## 第2条 LLMと決定論的コードの役割

基本原則は次である。

```text
LLMが意味を考える
コードが状態を保持する
コードが事実・権限・安全・整合性を検証する
記憶が継続性を与える
Style ManagerとTTSが表現する
```

LLMは会話方針、説明、提案、ユーモア、解釈、候補生成を担当できる。一方、確定事実、手番、数値、使用済み項目、権限、プライバシー、世代ID、再生済み位置、DB commitを直接確定してはならない。決定論的処理をプロンプトだけへ委ねず、人格表現を固定ルールだけへ押し込めない。

## 第3条 単一メインLLM原則

`CURRENT / CONFIRMED`: 会話・画像応答は設定で選ばれた一つの`OpenAICompatBackend`を主モデルとして使用する。検索計画、行動方針、内省などの補助呼び出しは存在するが、別の常駐人格モデルではない。

`INVARIANT`:

- 基本的にメインLLMは一つとする。
- STT partialごと、相槌だけ、heartbeatだけのために別LLMを常駐させない。
- 同じターンへ無意味な複数推論を重ねない。
- 人間への明示的な応答を、視覚解析、自律行動、自律研究より優先する。
- 補助LLM呼び出しは用途、予算、タイムアウト、キャンセル責任者を明示する。

`TARGET`: 前景会話、短い視覚リアクション、低優先度の自律処理を同一モデルの優先度付きスケジューラで調停する。

## 第4条 事実と解釈

情報は少なくとも次へ分類する。

1. `CANONICAL_FACT`: commit済みの状態、ユーザーが明示した確定情報。
2. `DERIVED_FACT`: CANONICAL_FACTから決定論的に導出可能な情報。
3. `RULE`: ゲーム規則、システム規則、権限制約。
4. `INTERPRETATION`: LLMまたは知覚器による解釈。
5. `HYPOTHESIS`: 未確認の推測。
6. `PREFERENCE`: 好み・傾向。
7. `EMOTION`: 連続的な内部表現または推定。
8. `REFLECTION`: 経験から得た内省。

下位の情報が上位の事実を上書きしてはならない。検索の抜粋はEvidenceでありFactではない。画像モデルの説明はObservationでありゲーム状態の確定値ではない。STT partialは仮説であり、確定記憶へ保存しない。

## 第5条 状態所有権

すべての重要状態に唯一の論理所有者を定め、更新は所有者のAPIを通す。

| 状態 | CURRENTの主所有者 | TARGET |
|---|---|---|
| 会話意味状態・継続依頼 | `Mind`、`ConversationKernel`、`ContinuationController` | `Mind`配下の単一会話状態 |
| ローカル応答世代・再生 | `VoicePipeline`、`SpeakerPlayback`、`PlayedTextTracker` | transport共通Response Runtime |
| Discord応答世代・再生 | `DiscordBridge`、`QueueAudioSource`、`PlayedTextTracker` | transport共通Response Runtime |
| Activity確定状態 | `ActivityStateManager` | 維持 |
| 自律AgentState | transportごとの`AutonomousActionSystem` | 共有意味状態＋surface別floor |
| 記憶 | `Mind`、`MemoryStore` | メタデータとACLを備えたMemory Manager |
| 自律研究 | `AutonomousResearchService`、`ResearchStore` | 維持しつつknowledge利用経路を明示 |
| 視覚短期状態 | `PerceptionService`、`PerceptionMemory` | GPU scheduler配下 |

`CURRENT`: ローカルとDiscordに応答・自律・再生状態の重複がある。これは既知の移行中設計であり、片側だけ直して完了としてはならない。

## 第6条 非同期処理、世代、キャンセル

非同期成果物には用途に応じて`utterance_id`、`response_id`、`generation_id`、`activity_session_id`、`state_version`、`task_id`を付ける。古い世代、古いstate version、終了済みActivityに基づく結果はcommit・表示・再生しない。

`INVARIANT`:

- LLM、文分割、TTS、待機queue、再生、PlayedTextTrackerへ同じ`response_id`を伝播する。
- キャンセル後に遅れて完成したTTSを再生しない。
- taskを作成した所有者がcancel、await、例外回収を行う。
- queueは原則として上限を持ち、満杯時の方針を明示する。
- `sleep`追加だけで競合を隠さない。
- timeoutは状態遷移を伴い、同じフォールバックを無限反復しない。

## 第7条 音声会話と割り込み

`CURRENT / CONFIRMED`: ローカルはMicCapture、DiscordはDAVE対応Node sidecarからの直接受信が主経路である。VAD、final STT、話者識別、会話判断、LLM、TTS、再生を経る。`TurnManager`と`PlayedTextTracker`により、相槌・誤検知と実質的割り込みを区別する。

`INVARIANT`:

- Discord RTP/Opus/DAVE直接受信の主経路を、根拠なくWindowsループバックへ戻さない。
- partialとfinalを区別し、partialで記憶・Activity・外部操作をcommitしない。
- soft interruptionとhard interruptionを分ける。
- echoを話者IDだけで判定しない。
- TTS中も入力を完全遮断しない。
- ユーザー発話開始時は速やかに無音化し、相槌なら位置を維持して再開する。
- hard interruptionではLLM、TTS、再生を連動cancelし、再生済み・未再生テキストを保持する。
- 人間発話を自律発話より優先する。

## 第8条 会話参加とグループ通話

ポッポは名前を呼ばれた時だけ応答するBotではない。同時に、複数人の会話すべてへ割り込むBotでもない。Addressee、発話権、会話の流れ、人間優先度、応答効用、AI発話比率、直前の応答、参加人数、cooldownを組み合わせて判断する。

Discord account IDは「どのアカウントの音声か」、声紋は「実際に誰が話したか」を表す。どちらか一方だけで人物を確定しない。グループで許可されない一対一記憶をLLMへ渡さない。

## 第9条 自律行動

自律行動はランダム発言ではない。

```text
Perception / Event / Heartbeat
→ AgentState
→ Candidate Actions
→ Utility
→ Floor / Privacy / Safety
→ Action or DO_NOTHING
```

沈黙だけを発言理由にしない。`DO_NOTHING`は正常な候補であり、終端停止状態ではない。曖昧な過去参照を自発話の種にしない。自発話には、今その話をする根拠、具体的な主題、ユーザーが理解できる導入が必要である。

`CURRENT`: `AutonomousActionSystem`が有効時、旧`proactive`ランダム経路は抑止される。視覚自動コメントは別経路である。

## 第10条 自律研究

自律研究では、疑問、理由、作成者、期待効用、許可語、privacy scopeを記録する。ランダム検索を禁止し、Query Sanitizerを通し、PII・資格情報を外部へ送らない。Evidence、Knowledge、Reflectionを分離し、複数情報源または公式情報で検証する。単一ソースは暫定情報とする。

高リスク分野は承認制とし、検索結果を自身の経験と偽らない。検索完了のたびに割り込まず、関連する会話で利用する。管理UIから履歴、状態、失敗理由を確認可能にする。

## 第11条 記憶

記憶は会話ログの別名ではない。episodic、semantic、preference、relationship、commitment、reflection、activity、research evidenceを区別する。

`TARGET`: 各記録は`owner`、`source_audience`、`privacy_scope`、`occurred_at`、`observed_at`、`stored_at`、`confidence`、`provenance`を持つ。

`CURRENT`: `MemoryStore`の主要`memories`/`transcripts`テーブルは上記メタデータを完全には保持せず、プライバシー情報が別文脈に依存する。新しい記憶種別を追加するときはschema migrationと旧レコードの安全な既定値が必要である。

## 第12条 プライバシー

最低限、`PUBLIC`、`CURRENT_GROUP_ONLY`、`OWNER_AND_AI`、`ALLOWLIST`、`DO_NOT_STORE`を表現する。保存前のclassification、retrieval時ACL、LLM投入前gate、出力時検査を独立して持つ。出力後のマスキングだけをアクセス制御とみなさない。

秘密、トークン、APIキー、個人情報、音声、画像、Base64 mediaを通常ログ、設計文書、handoffへ複製しない。`.env`は値ではなく必要な変数名だけを扱う。グループでは、legacyまたはscope不明の一対一記憶を既定拒否する。

## 第13条 時間

`WALL_TIME`、`ACTIVE_TIME`、`SESSION_TIME`、`MONOTONIC_TIME`を区別する。経過時間計測とtimeoutにはmonotonic clockを使い、日時表現にはwall clockを使う。過去表現はtimestampと照合し、オフライン中に考え続けた、見続けた、聞き続けたと偽らない。

## 第14条 感情、人格、関係性

感情は連続状態として扱い、会話スタイルとTTSへ穏やかに反映できる。感情は、確定事実、プライバシー、ルール、手番、数値、権限、安全判断を変更しない。

人格の変化は小さく、原因、前後値、時刻、対象ペルソナを追跡可能にする。ペルソナ間で関係性、話者設定、心の状態を意図せず共有しない。全肯定を人格とみなさず、確信度に応じて反論・保留・無知を表現する。

## 第15条 Activityとゲーム

LLMは戦略と表現を考える。コードは確定履歴、手番、ルール、使用済み、数値、勝敗、状態遷移を保証する。

```text
Proposal → Normalize → Validate → Commit → Grounded Output → TTS
```

無効なAI行動をcommitまたは読み上げない。訂正は誤ったcommitの上に会話だけを重ねず、検証可能なrollbackまたは補正transactionとして扱う。

## 第16条 GPU、遅延、リソース

現在の対象はWindowsとCUDA GPUを中心とし、LLM、faster-whisper、Style-Bert-VITS2、視覚推論が同じGPU資源を競合し得る。優先順位は次を既定とする。

```text
人間への前景応答
> STT final
> TTS初回
> 明示ツール
> 明示視覚質問
> 自律行動
> 背景視覚解析
> 自律研究
```

無制限queueを作らず、最新情報が重要な映像はlatest-frame-winsとする。バックグラウンド処理は会話中にGPUを占有し続けない。平均値だけでなくp50/p95、初トークン、TTS初回、再生開始、queue待ちを測る。ハードウェア型番は実機確認なしに憲法へ固定しない。

## 第17条 安全な失敗

状態は少なくとも`VALID`、`UNCERTAIN`、`CONFLICTED`、`STALE`、`ERROR`を区別する。不明な状態で推測して外部操作・commitを続けない。修復可能なら一度だけ回復を試み、不能なら安全にpauseし、ユーザーへ原因と復旧方法を示す。例外を握りつぶしたまま返答なしにしない。

## 第18条 Legacyと実験機能

Legacyコードは即削除しない。なぜ残るか、現在有効か、新機構との重複、削除条件、移行計画を記録する。Legacyと新機構を同時稼働させない。

実験機能はfeature flag、失敗時の分離、観測可能性、rollbackを持つ。存在するだけのコードを「実装済み」と報告しない。

## 第19条 変更の互換性

新機能と修正は、Conversation Controller、Full Duplex、Memory、Privacy、Temporal、Emotion、Agent State、Activity、Research、GPU、Management UI、Remote Launcher、ローカル/Discord両surfaceとの整合を確認する。共通意味論を片側へ複製せず、transport adapterから共有層へ寄せる。

## 第20条 観測可能性

重要処理は、入力識別子、状態before/after、判断理由、候補、検証、commit、cancel、stale discard、TTS、timeout、例外を追跡可能にする。ログは秘密を含めず、音声・画像の診断保存は明示同意、保存期間、削除経路を持つ。

「ログを追加した」は修正完了ではない。ログにより原因、期待状態、回帰の有無を確認して初めて観測可能性の改善となる。

## 第21条 複数AIの協業

一つの作業ツリーを複数AIが同時に変更してはならない。一度に書き込み権限を持つ担当を一つにし、開始前に範囲と予定ファイルを宣言し、終了時にdiff、テスト、未解決、rollback、handoffを残す。次のAIは説明だけでなく実diffとテストを確認する。

`TRANSITION`: 有効なGitリポジトリを復旧・確立した後、taskごとのbranchまたはworktreeを第一選択とする。それまでの暫定策として`/.ai-work-lock.json`を提案するが、運用者承認なしに導入しない。

モデル名で役割を固定しない。設計レビュー、実装、検証の現在担当と書き込み権限を明示する。

## 第22条 変更と完了

局所修正、小さなdiff、feature flag、明示的migration、再現可能なテスト、rollbackを基本とする。無関係な整形、同時の大規模改名、目的外リファクタリング、依存更新を混ぜない。

コードが書けた、例外が一度出なかった、LLMが一度うまく返しただけでは完了でない。根本原因、対象テスト、回帰、実行ログ、設定、migration、rollback、handoffを揃える。詳細は [TEST_AND_ACCEPTANCE_POLICY.md](TEST_AND_ACCEPTANCE_POLICY.md) に従う。

## 第23条 憲法改正

開発エージェントは作業中に本書を都合よく変更しない。変更が必要な場合は`CONSTITUTION_AMENDMENT_PROPOSAL`として、理由、変更前、変更後、影響範囲、関連ADR、移行、ユーザー承認要否、version案を提示する。所有者の承認後にversionと更新日を変更し、[DECISION_LOG.md](DECISION_LOG.md)へ記録する。

### 改正履歴

- `1.1.0`（2026-07-26、所有者承認）: 会話を独立命令として扱わないこと、意図・指示対象・会話状態の継続理解、経験が現在の判断へ参加する固有人格、GPT Liveおよびネウロ様・Evilを参照品質として扱う範囲、内部思考をsurfaceへ漏らさない原則を第1条へ明記した。
- `1.0.0-draft`（2026-07-26）: 初版草案。

## 第24条 現在の移行優先順位

1. 実働ルートのGit管理を復旧し、入れ子の古いリポジトリとの関係を確定する。
2. ローカルとDiscordに重複するResponse Runtime、Autonomy、検索/TTS制御の意味論を共通化する。
3. `privacy`の重複設定とMemory schemaのACLメタデータ不足を解消する。
4. 巨大クラスを責任単位へ段階分割する。挙動を変える一括rewriteは禁止する。
5. GPU作業の優先度と計測を一元化する。
6. CURRENTと一致しないREADME・過去設計文書へ状態ラベルを付ける。

## 根拠

- `run.py`
- `neuro_voice/app.py`
- `neuro_voice/pipeline.py`
- `neuro_voice/discord_bridge/bot.py`
- `neuro_voice/discord_bridge/direct_receiver.py`
- `discord_receiver/receiver.js`
- `neuro_voice/mind/mind.py`
- `neuro_voice/mind/store.py`
- `neuro_voice/privacy.py`
- `neuro_voice/realtime/turn_manager.py`
- `neuro_voice/realtime/playback_tracker.py`
- `neuro_voice/autonomy/engine.py`
- `neuro_voice/autonomy/heartbeat.py`
- `neuro_voice/research/service.py`
- `neuro_voice/activity/engine.py`
- `neuro_voice/vision/video.py`
- `config/config.yaml`
