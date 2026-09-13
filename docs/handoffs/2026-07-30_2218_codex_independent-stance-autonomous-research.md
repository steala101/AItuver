# Handoff: 非迎合会話・自律研究producer・履歴可視化・発話metadata防漏

- Date: 2026-07-30 22:18 JST
- Agent: Codex
- Status: completed
- Scope: Local一対一の会話姿勢、Local/Discord共有Mindの自律研究、GUIこころ履歴、共通出力guard。

## User goal

1. ポッポがユーザーへ肯定・好意的に擦り寄りすぎないこと。
2. personaの興味などを起点に、裏で自発的な検索が実際に行われること。
3. 自律研究・検索履歴をGUIで確認できること。
4. `[emotion: joyful]`や日本語中の`definitely`を発話しないこと。

## Root cause

- `AutonomousResearchService.heartbeat()`はpersist済みpending queueの保守だけで、課題producerがなかった。
- Local/Discordの`set_proactive(false)`がHeartbeat自体をpauseし、自発発話を切ると裏の研究まで停止していた。
- 明示的な「あとで調べて」だけがResearchQuestionを作っていた。
- `get_research_history()` APIは存在したが、GUIから呼ぶ要素がなかった。
- persona promptがUnifiedExpressionPlan導入後もLLM本文へ感情/style tagを要求していた。
- `EmotionTagParser`は既知labelだけを除去したため、`joyful`等の未知型付きtagが漏れた。
- Spoken outputの英語self-talk guardは長い推論を対象とし、単語一つの混入を扱っていなかった。
- 事実Agreement Gateはあったが、関係性質問で好意・親密さを盛らない明示方針が不足していた。

## Implementation

### 会話姿勢と表現

- `ResponseDirector`へ`Independent Social Stance`を追加。温かさと同意を分け、関係状態より
  好意・称賛・親密さを盛らない。
- 関係性・感情確認を一般質問より先に分類し、関心・信頼・親しさを分けて答える。
- persona promptは本文tagを禁止し、UnifiedExpressionPlanをmetadata正本とした。
- `EmotionTagParser`は任意labelの`[emotion: ...]` / `[style: ...]`を除去し、
  対応済みlabelだけcallbackへ渡す。一般の`[Python]`は残す。
- `SpokenOutputFilter`は日本語文脈中の限定的な英語下書き語だけをstream token境界込みで置換する。
  技術語・製品名は保持する。

### 自律研究

- `Mind.research_heartbeat()`がqueue保守後、active personaの公開`interests`を候補として渡す。
- serviceは起動45秒、seed間180分、失敗再試行10分、queue idle、quota、duplicate、privacy、
  low-risk gateを満たす一件だけを`PERSONAL_CURIOSITY / PERSONA_CURIOSITY`として作成する。
- 一対一会話の事実質問へ率直な非回答をした場合は`KNOWLEDGE_GAP / CONVERSATION_GAP`を作る。
  group、関係、感情、声、顔、知覚、記憶確認は除外。
- event loop外のenqueueはResearchQuestionをDB/queueへ残し、次のHeartbeatでworkerを開始する。
- snapshotにseed enabled、last/next attempt、last reason、created/skipped metricsを追加。
- Local/Discordとも自発発話設定ではHeartbeatをpauseせず、発話callbackだけをinactiveとして研究を継続する。Discord所有中のLocal suspendは二重tick防止として維持する。

### GUI

- こころ画面へ「自律研究・検索履歴」カードを追加。
- 初期表示はMind statusの直近5件。「履歴を更新・全件表示」で既存APIから100件取得。
- 日時、状態、理由、title、サニタイズ済み検索語、失敗理由を表示する。
- raw conversation、秘密情報、raw evidence本文は表示しない。

## Configuration

```yaml
autonomous_research:
  self_seed_enabled: true
  self_seed_delay_seconds: 45
  self_seed_cooldown_minutes: 180
  self_seed_retry_minutes: 10
```

既存Research SQLite schemaを利用し、migrationはない。

## Verification

- Focused: `175 passed`
- Prompt上限修正後: `5 passed`
- 履歴診断追加後: `34 passed`
- 全persona興味カテゴリ追加後: `12 passed`
- Heartbeat所有権を含む最終focused: `26 passed`
- Final full suite: `1140 passed / 2 skipped in 57.51s`
- 変更Pythonの`py_compile`成功。
- Skipは既存の`pyflakes`未導入のみ。

## Runtime acceptance

GUIを再起動し、次を確認する。

1. 自律研究ON、自発モードは任意（OFFでも研究継続）。起動後60〜90秒待つ。
2. 「こころ → 自律研究・検索履歴」を開き、persona興味由来の課題が表示される。
3. statusが`調査中 / 確認済み / 暫定 / 失敗`のいずれかに進み、reasonが`興味`になる。
4. 一対一で低リスクの事実質問へポッポが本当に答えられない例が出た時、`知識不足`履歴が増える。
5. 関係性を質問し、実際の関係を超えた「大好き」「もっともっと知りたい」の定型へ飛躍しない。
6. `[emotion: joyful]`と単独の`definitely`が字幕・TTS・final履歴へ出ない。

## Remaining risks

- 外部Web検索が失敗すればFAILED履歴となる。これは沈黙より観測可能だが、接続修正は別途必要。
- 広い興味は広い検索結果になる。query templateを変える前に履歴とEvidenceを実機評価する。
- 社会的な非迎合は意味判断を要するため、regexで発話を強制置換せずPlanner/Surface方針で行う。
- Local/Discord response runtimeは別instanceだが、Mind research storeと出力guardは共有実装。

## Rollback

1. persona興味seedだけ止める: `autonomous_research.self_seed_enabled: false`。
2. 自律研究全体を止める: `autonomous_research.enabled: false`。
3. 履歴UIはread-onlyなので、非表示にしてもDB/workerへ影響しない。
4. Output metadata guardは安全境界のため、tag出力promptだけを戻さない。

## Related

- ADR-0002
- ADR-0003
- ADR-0004
- D-032
- R-038
- R-042
