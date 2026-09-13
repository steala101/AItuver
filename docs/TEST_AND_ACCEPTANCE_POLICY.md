# テスト・受け入れ基準

- Version: 1.0
- Snapshot: 2026-07-26

## 1. 完了の定義

次だけでは完了としない。

- コードを書いた。
- import/buildが通った。
- 例外が一度出なかった。
- LLMが一度期待した返答をした。
- ログを追加した。
- test fileが存在する。

最低限、根本原因、対象test、既存回帰、期待stateの観測、設定、migration、rollback、handoffが必要である。実機が必要な項目はmanual acceptanceまで終えて初めて「実機確認済み」とする。

## 2. テスト層

| 層 | 目的 |
|---|---|
| Unit | pure function、validator、classifier、serialization |
| Integration | module間のcontract、factory、store、surface adapter |
| State Machine | Turn、Activity、Directive、Autonomyの遷移 |
| Concurrency | task/queue/lock、同時event |
| Cancellation | response/session世代と遅延結果 |
| Replay | 保存済みeventを同条件で再現 |
| Privacy | 保存前分類、retrieval ACL、group、output guard |
| Prompt Grounding | canonical facts、internal tag漏洩、Evidence区別 |
| Performance | p50/p95、first token、TTS first、queue待ち、GPU |
| Discord Manual | DAVE receive、join/leave、複数人、playback |
| Long Running | reconnect、memory growth、heartbeat、resource leak |
| Regression | 過去bugと横断経路 |

## 3. 共通不変条件

すべての変更で関連するものをtestする。

- cancel済み`response_id`のtext/PCMが表示・再生されない。
- stale `activity_session_id/state_version`をcommitしない。
- partial STTが記憶、tool、Activity commitを起こさない。
- groupで許可されないmemoryがpromptへ入らない。
- internal thought/planner/style tagを表示・TTSしない。
- optional service失敗で前景会話全体が沈黙しない。
- GUI closeでinput、task、LLM、owned TTS serverが停止する。
- local/Discordで共通意味論が同じになる。

## 4. 音声

### Unit/Integration

- final beam、partial beam、language、VAD設定がfactoryから反映される。
- PCM format、sample rate、channel変換。
- TTS chunk IDとplayed frame進捗。
- playback volume、pause、resume、discard。

### State/Cancellation

1. AI発話中に長い発話: VAD直後にpause、hard判定後にLLM/TTS/playbackをcancel。
2. 「うん」: 一度pause後、同位置からresume。
3. 短いnoise: 履歴へ入れずresume。
4. cancel済みTTSが遅れて完成: queueへ入らない。
5. resume直後の再割り込み: 二重再生・played text重複なし。
6. queue overflow: policyどおりdrop/backpressureし、deadlockなし。
7. echo: TTS類似度、timing、acoustic evidenceを使い、話者IDだけで決めない。

### Acceptance

- 発話開始から無音化のp95目標を定義し、現行目標100msに対して計測する。
- 不自然な語中pause、音切れ、robotic PCMがない。
- 30分連続会話でinput/output taskとaudio deviceが生存する。

## 5. 会話参加

- 明示呼びかけはrespond。
- 一対一VCは直前の呼びかけなしでも自然にfollow-up。
- 複数人では人間同士の発話すべてへrespondしない。
- assistant直後のdirect replyを見送らない。
- assistantの説明後の「なるほど、そういうことか」「たしかに、それ面白いね」はLLM/TTSを開始せず、自然に会話を閉じられる。
- 終了相槌を`active_topic`として残さず、直前の意味ある話題を保持する。
- 相槌に質問・訂正・依頼が続く場合、実行確認への肯定、Activity中の入力は沈黙へ誤分類しない。
- 一対一または明示的な感謝へ返す場合も、生成LLMを使わない短い応答に限定できる。
-曖昧な短文、独り言、他人宛て、human overlapを区別する。
- human speechでpending autonomous speechをcancel。
- AI speech ratioとcooldownが時間で回復する。
- `DO_NOTHING`後も次heartbeatで再評価する。

Acceptanceは録音/replay corpusでprecisionとrecallを分けて測る。「返事が多い/少ない」の一指標だけで調整しない。

## 6. 会話生成・記憶

- Working Memoryが前turnの目的・未解決疑問を引き継ぐ。
- vague referenceしかないmemoryは自発話へ使わない。
- recent conversation questionはWeb検索より会話履歴を優先する。
- 「何？」だけで検索しない。
- response planの長さ/質問/ユーモアが毎回固定にならない。
-同一構成・語尾・質問の反復をCriticが検知する。
- factual disagreementを全肯定しない。
- 記憶を「自身の経験」と誤表現しない。
- persona切替でmemory、relationship、speaker、heart stateが分離する。

## 7. Privacy

### 必須case

- API key、token、password: `DO_NOT_STORE`。
- owner-only memory: 一対一でのみretrieval。
- group memory: source audienceを超えたgroupへ出さない。
- legacy/scope不明: group promptへ入れない。
- forget/no-store command: 保存されず、既存対象を安全に削除。
- output guard: contact/credentialらしき文字列を遮断。
- search sanitizer: raw private transcriptをqueryへ出さない。
- log: secret、raw media、Base64を含まない。

ACL testでは「最終出力に出なかった」だけでなく、「LLM inputへ渡っていない」ことをassertする。

## 8. Activity

- 初手をActivity inputとしてroutingできる。
- proposalがrule違反ならcommitもTTSもしない。
- duplicate word/moveを拒否。
- turn ownerが違えば拒否。
- stale version/sessionを拒否。
- validation後だけledgerへcommit。
- TTS失敗でcanonical commitを二重実行しない。
-訂正時に誤状態をrollback/補正し、会話文だけ直して終わらない。
- restart後にsnapshot/ledgerから同じ状態へrecover。
- concurrent inputで一手だけcommit。

しりとりは、かな正規化、濁点/長音/小文字、語尾、重複、固有名詞policy、`ん`、訂正、AI自身の手を個別testする。

## 9. 自律行動

- heartbeatは定周期で生存する。
- silenceだけではspeechしない。
- current event、specific topic、utility理由があるときだけcandidate speech。
- vague old memory候補をreject。
- `DO_NOTHING`はnormal resultで、schedulerを停止しない。
- busy/floor/group/privacy gate。
- human inputでcancel。
- localとDiscordで同じcandidate意味論。
- surface leave/shutdownでtaskが残らない。
- autonomous speechが通常response ID/cancel pathを通る。
- 「一旦終わろう」「もう終わったよ」等の停止表現は、ラジオ語を含んでも新しいDirectiveを作らない。
- 停止はLocal/Discord双方でplan task、segment task、LLM、未再生音声を取消し、遅延完了した古い結果を再生しない。
- 停止後quiet中はsilence/open-thread起点の自発発話をせず、期限後は現在文脈から再評価できる。

## 10. 自律研究

- reason codeなしはblock。
- PII/credentialをsanitizerがblock/redact。
- high-riskはapproval待ち。
- duplicate windowとquota。
- busy中はworkerが前景を妨げない。
- single workerで同一questionを重複実行しない。
- search resultのprompt injectionをreject。
-単一sourceはPROVISIONAL、公式または複数domainでVERIFIED。
- EvidenceとKnowledgeを別recordに保存。
- retentionでactive Knowledgeを消さない。
- history UI/statusにactive/queued/errorが出る。
- completion時に必ず割り込み発話しない。

## 11. Vision/OBS

- OBS source list/auth/error。
- current frame requestは古いqueueを再利用しない。
- background inference中もcaptureが継続する。
- latest-frame-wins、bounded raw history。
- source非表示/OBS停止でdesktopへsilent fallbackしない。
- stale ageをcontextに含め、currentと断定しない。
- explicit visual turnがbackground taskより優先。
- vision failure後もtext/audio conversationが継続。
- image/Base64がhistory/log/dataへ残らない。
- 1fps 30分でqueue、RAM、GPUが増加し続けない。

## 12. Discord

### Manual matrix

| Case | Expected |
|---|---|
| BotのみVC参加 | sidecar receive ready、host client不要 |
| 1 user | 名前なしfollow-upが自然 |
| 3 users | addressee/floorで過剰応答しない |
| 新規join | 参加者state更新、挨拶はcooldown/flowを尊重 |
| leave/rejoin | receive/playback/task復旧 |
| sidecar disconnect | bounded reconnect、UI/logに状態 |
| AI speaking中のhuman speech | direct inputを受けbarge-in |
| music中 | voice/STTとmusic mixが破綻しない |
| text echo off | 一般channelへ投稿しない |
| account/voiceprint mismatch | 人物を一方だけで確定しない |

## 13. GUI、shutdown、Remote

- 設定画面のopen latency。
- backend switch後にtop表示、speaker list、実TTSが一致。
- persona switchでlast selected voiceへ切替。
- heart/research/status APIは一部errorを隔離しJSON-safe。
- GUI close後にmic input、autonomy、Discord、LLM、TTS processが残らない。
- Remote APIはtokenなしで非localhostへ公開しない。
- rate limit、session expiry、invalid input。

## 14. Performance budget

ハードウェア実測値をbaselineとして記録する。暫定指標:

- STT final。
- Context build。
- speaker recognition。
- LLM first token/body。
- TTS first audio。
- playback start。
- total time。
- vision capture/queue/inference。
- autonomy/researchによる前景遅延。

p50だけでなくp95を計測する。前景会話中のbackground task有無を分ける。性能改善でaccuracy/privacy/cancel safetyを落とさない。

## 15. Test report format

```text
Environment:
Commit/tree:
Config profile:
Tests run:
Passed:
Failed:
Skipped:
Not run and reason:
Manual cases:
Latency p50/p95:
Logs inspected:
Privacy artifacts checked:
Known limitations:
```

## 16. 現在の検証制約

2026-07-26の文書作成時点では、global Pythonがなく、`.venv`は存在しないPython 3.11 pathを参照していたためpytestを実行できなかった。この状態で現在のsuiteを「pass」と扱わない。環境復旧後にsyntax、targeted tests、full suiteの順でbaselineを保存する必要がある。
