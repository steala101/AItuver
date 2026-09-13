# Handoff: 会話Move候補競争・反応学習・意味連想・統一表現計画

- Date: 2026-07-30 02:26 JST
- Agent: Codex
- Status: completed
- Scope: Local一対一とDiscord groupの共有会話意味論。Avatar/配信commentは将来境界のみ。

## User goal

ネウロ様/Evil様に近い、人間味があり面白く流暢な会話へ進めるため、次を優先順に実装する。

1. Conversation Selection Kernel
2. Response Candidate Competition
3. Reaction Learning
4. Semantic Association Graph
5. Unified Expression Plan

現在Avatarはなく、配信もしない。身体表現は将来接続可能な機能だけを持ち、配信commentは
今後の拡張点に留める。

## Design

### 単一生成の候補競争

候補ごとに返答本文をLLM生成するとOllamaの初回応答が悪化するため行っていない。
`ConversationSelectionKernel`は本文でなくMove候補を比較する。通常候補は
`DIRECT_ANSWER / BRIEF_REACTION / SHARE_OPINION / PLAYFUL_REACTION /
EXTEND_TOPIC / ASK_QUESTION`。訂正、支援、履歴想起はhard obligation。

scoreはrelevance、continuity、social fit、novelty、直近Move、学習重みから作り、
selected MoveだけをPlanner promptへ渡す。全候補はdeveloper traceとAdaptive
candidate evaluationへ残すが、会話本文/TTSへ出さない。

### 反応学習

次のユーザー発話を、明示肯定/否定、反復指摘、笑い、会話継続、終了相槌へ分類する。
Move重みは0.70〜1.30、明示alpha 0.08、implicit alpha 0.025。沈黙や割り込み単体は
評価にしない。Discord groupではraw reactionを保存せずstructured aggregateだけを保存する。

### 意味連想

`SemanticAssociationGraph`はpersona別`adaptive_<persona>.db`のgeneric recordへ
短いtopic nodeと`co_occurs` / `corrected_to` edgeを保存する。事実DBではない。
directは話者key内だけ、groupはcurrent store sessionかつ4時間TTLだけ。
PrivacyManagerが`should_store=false`としたturnは追加しない。

### 表現

`UnifiedExpressionPlan`はvoiceとfuture avatarを同じresponse IDへ束ねる。
voice deliveryは既存StyleManager/TTSへ利用される。Avatarは`NullExpressionSink`で、
Local/Discord eventも`avatar_connected=false`。本文へtagは付けない。

配信commentは`ConversationAudienceMode.BROADCAST`、`CommentCluster`、
`BroadcastCommentClusterer` protocolのみ。runtime readerは存在しない。

## Changed files

- `neuro_voice/dialogue/selection_kernel.py`
- `neuro_voice/dialogue/reaction_learning.py`
- `neuro_voice/dialogue/semantic_graph.py`
- `neuro_voice/dialogue/expression_plan.py`
- `neuro_voice/dialogue/audience.py`
- `neuro_voice/dialogue/conversation_planner.py`
- `neuro_voice/dialogue/intelligence.py`
- `neuro_voice/dialogue/adaptive_store.py`
- `neuro_voice/dialogue/__init__.py`
- `neuro_voice/mind/mind.py`
- `neuro_voice/pipeline.py`
- `neuro_voice/discord_bridge/bot.py`
- `config/config.yaml`
- `tests/test_conversation_selection_and_expression.py`
- shared architecture/decision/risk docs and ADR-0003

## Verification

- Python 3.11 `py_compile`: success for all changed runtime Python files.
- New tests: 8 passed.
- Focused conversation regression: 223 passed.
- Final full suite: 1131 passed / 2 skipped in 55.21s.
- Existing skip: `pyflakes` is not installed for `test_no_undefined_names.py`.

No additional LLM call was added. Avatar and broadcast readers perform no external action.

## Runtime acceptance

Restart the GUI so new modules/config are loaded, then:

1. Local一対一で「うん」「普通の質問」「私は少し違うと思う」「詳しく説明して」を混ぜて
   8〜15turn話す。短い相槌が長文化せず、質問に直接答え、Moveが一種類へ固定しないこと。
2. 同じ返し方が続いた時に「また同じこと言ってる」と明示し、その後のdeveloper
   `conversation_plan`で同Moveが絶対禁止でなく弱く減点されること。
3. Discord 3人以上で、他人同士の会話へ毎回質問せず、指名・直接follow-up・訂正には応答すること。
4. `expression_plan` eventのresponse ID/sourceがconversation planと一致し、
   `avatar.enabled=false`であること。TTS本文に内部labelが出ないこと。
5. directで話した私的topicがDiscord groupのsemantic associationへ出ないこと。

## Remaining risks

- 弱いimplicit reactionは内容以外の理由で起こるため、人格や事実の確定には使えない。
- 日本語topic抽出は短い字種単位で、関係の意味は粗い。graphは関連候補に限定する。
- Moveの選択改善が実モデルの表層へ反映される量は、実会話のA/Bが必要。
- Local/Discord response runtimeは依然別実装。共有Mind契約とテストで意味論を維持している。

## Rollback

1. 候補競争だけ止める: `dialogue.selection_kernel.enabled: false`。
2. graphだけ止める: `dialogue.semantic_graph.enabled: false`。
3. SQLiteの新recordはgeneric table内なので、停止時に削除migrationは不要。
4. Expression eventが外部consumerと衝突する場合はLocal/Discordのemitだけ外し、
   `UnifiedExpressionPlan`とvoice deliveryは残す。

## Related

- ADR-0001
- ADR-0002
- ADR-0003
- D-031
- R-038
- R-041
