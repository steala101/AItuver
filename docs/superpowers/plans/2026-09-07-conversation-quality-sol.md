# AItuber 会話品質改善 — Sol向け実装指示書

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:executing-plans`を使用し、task単位で実装・検証する。実装担当はユーザー指定のSol一名。同一treeの並列編集・自動commitは禁止。チェックを付けるのは成果物と検証結果が揃った時だけ。

**Goal:** 検索・文脈・人格表現・音声応答を、根拠と比較可能な品質評価に基づいて改善する。

**Architecture:** 既存Mind、ConversationPlan、ActionEligibility、ExpressionPlan、配送ledgerを再利用する。検索入口と根拠選択を先に直し、両surfaceの判断契約を小さく揃える。文体・感情・モデル・遅延は別々の比較で効果を確認する。

**Tech Stack:** 現行Python / pytest / asyncio、既存LLM backend、既存TTS、Discord DAVE Node sidecar。依存追加なしを初期方針とする。

**Spec:** [ADR-0005（P0〜P2と独立M accepted / P3以降 proposed）](../../adr/ADR-0005-conversation-quality-recovery.md)。関連する承認済みADR-0001〜0004、Decision D-038〜045も読む。

**2026-09-10 status:** P0〜P2はdeadline/cancelを含めて`CODE_VERIFIED / HARDWARE_PENDING`。独立単位Mはtmp_pathでcode/reviewを閉じ、別のユーザー明示承認後にonline backup・隔離copy検証・canonical指紋照合を経て本番7 DBのschema/generation 8 sidecarへ昇格済み。容量20%警告も共有statusへ実装済み。P3以降と会話実機受入は未着手。永続`memory.write_enabled`は変更していない。

## 2026-09-07 ユーザー回答による確定方針

- 優先順位は **Local → Discord複数人**。Discord一対一は互換回帰として扱う。共通意味論のtestは両surfaceで維持するが、体感・遅延の最初の実機評価はLocal。
- 人格はほどほどに活発、からかい多め、製作者への当たりは強め。既存personaの継続性を保ち、状況と相手の反応で調節する。ネウロ様は存在感の参照であり、台詞・人格を複製する指示ではない。
- **速さ優先、内容の整合性は維持**。現在の目的に十分な短い一言・回答を先に成立させ、不要な説明や確認のLLM往復を増やさない。
- **会話モデルはLocal LLM限定**。クラウドAPI案、クラウドfallback、自動外部judgeを計画から除外する。
- 音声は現在の声を維持。Fish Audioは将来の比較候補として記録するだけで、導入・声変更・外部音声送信は実施しない。
- 状況を踏まえて最適な発話を選ぶ。長さ・質問・冗談・沈黙を全turnで固定せず、事実説明、短い反応、訂正、話題継続を使い分ける。
- 容量増加を許容して記憶を保持する。保存量、検索対象、prompt量を別管理する。既存の自動削除経路はP0で監査し、後述Mの保持契約を本番再開の前提にする。

この方針は確認済み。末尾の残る確認事項だけを保留し、同じ希望を再質問しない。

## Global Constraints

- 正本は `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`。入れ子の古いrepo・GitHub版を正本にしない。
- アプリコード変更はSolが担当。この文書作成ではアプリコード、DB、設定、過去Traceは変更していない。
- AGENTS.mdの読む順を守る。`_NEXT_SESSION.md`本文の2026-08-04情報は古いので、最新台帳・実コード・ユーザー結果を優先する。
- 本番Memory DBの変更、復元、削除、再移行、persona書換え、Memory書込み再開を含めない。全テストのstore/state/data rootはtmp_pathへ置く。
- 実行用venvを修理しない。分離済み `.venv-test\Scripts\python.exe` を使う。全pytestの過去件数を達成目標にしない。予期しないfailed=0、理由付きplatform skipを要求する。
- Git init/reset/commit/pushはしない。現treeで `git status` は今回も失敗した。編集範囲の事前hashと復元用コピーを機密を含めず保存し、無関係変更を上書きしない。
- メインLLMは一つのLocal LLM。通常turnは本文生成一回を基本とし、追加の常駐Intent/感情/Criticモデルを導入しない。既存Echoの最大一回repairは別計測する。クラウドへの暗黙fallbackも禁止。
- Local/Discordで同じ意味論をtestする。ただしLocal PRODのTool権限をDiscordへ無条件に移植しない。
- 全queue・timeout・cancelのownerを明示。cancel後の遅延結果はUI・TTS・Memory・履歴へcommitしない。
- Traceに本文、prompt、検索query/抜粋、raw音声、秘密値を追加しない。UIには必要な安全な結果だけを表示する。
- 検索内容のLLM要約はD-041変更承認までOFF。抽出的説明とsource表示は既存方針内で改善する。
- 実機発話・Discord操作・外部検索・有料推論をCodex側から自動実行しない。synthetic fixture＋fake依存で先に検証する。

## 0. 最初に読む要約と着手範囲

最初の実装依頼では **P0 → P1 → P2のみ** を完了させ、結果を報告する。P3以降を同じ巨大diffへ入れない。P3/P4へ進む根拠はP0の評価差と、P1/P2の受入結果。

追記: P0には保持監査を含む。**本番DBを用いた起動・Memory書込み再開の前にMの非削除契約を検証する。** Mの実装は独立diffで行い、検索修正とDB変更を混ぜない。本番への適用・移行は別確認とする。

今回確定した入口バグ:

```text
input: 饅頭こわいを検索して、短く教えて
is_search_request: false
should_search: false
build_query == 饅頭こわい: false
対照: 饅頭こわいを検索して → true
```

検索の実機不合格を再現する材料であり、最新ASR・実効設定・policy・途中returnまで確認済みという意味ではない。検索の精度低下には、汎用queryへ攻略語を足す処理、160文字へ先に切る処理、body優先、query非依存の結果選択も関与し得る。

最新ユーザー報告ではDAVE音楽継続・新質問への切替・退出は改善。この3点は回帰保護し、今回の検索修正と無関係なmix/leave再実装はしない。

## 1. 既存ファイルと新規境界

| 目的 | 既存ファイル・入口 | 新規ファイル（この計画で作る場合） |
|---|---|---|
| 検索Intent/query | `neuro_voice/search/intent.py::is_search_request`、`deepsearch.py::should_search/build_query/_expand_queries` | なし。共有判定をここへ戻す |
| 検索結果契約 | `search/deepsearch.py`、`cognition/search_result_presenter.py` | `neuro_voice/search/contracts.py`：I/Oなしの型のみ |
| 両surface到達性 | `pipeline.py`のTool/検索呼出し、`discord_bridge/bot.py::_handle_utterance/_grounded_search_reply/_maybe_search` | `tests/test_search_surface_contract.py`：両入口のfake統合 |
| 評価 | `dialogue/conversation_critic.py`、`intelligence.py::quality_metrics`、`tools/check_reply_avoidance_ab.py` | `tests/fixtures/conversation_quality.json`、`tools/evaluate_conversation_quality.py`、`tests/test_conversation_quality_eval.py` |
| Action/Move整合 | `dialogue/planner.py`、`conversation_planner.py`、`selection_kernel.py`、`cognition/action_constraints.py`、`kernel.py` | `tests/test_conversation_decision_contract.py` |
| 文脈・prompt | `dialogue/kernel.py`、`working_memory.py`、`surface_realizer.py`、`mind/mind.py`、`memory/conversation.py`、`llm/prompt_layout.py`、`realtime/context_assembler.py` | `tests/test_conversation_quality_context.py` |
| 感情・声 | `dialogue/expression_plan.py`、`conversation_planner.py`、`mind/personality.py`、`tts/style.py`、`tts/style_bert_vits2.py` | `tests/test_expression_delivery_contract.py` |
| 計測 | `utils/latency.py`、`llm/prompt_metrics.py`、`llm/openai_compat.py`、`cognition/trace.py`、`turn_tracker.py` | 原則既存を拡張 |

新規名は提案インターフェースであり実在APIと誤解しないこと。着手時に既存の同責任型が見つかればそれを拡張し、この文書の呼出し・テスト名も同じdiffで合わせる。新しい汎用runtime/イベントbusを別途設計しない。

## P0. 基準線と評価の正直さ

**成果物:** 再現可能な評価fixture、実行経路一覧、実測と未測定を区別した評価結果。アプリの通常挙動変更はまだ不要。

**評価器の新規契約:** `EvaluationReport` は `explicit_search_successes: int`、`unmeasured_axes: set[str]`、`failed_delivery_turns: int` とcase別結果を持つ。本文を読まずに自然さを採点しない。入口fixtureは現在の実際のmethodを呼び、専用の簡略会話エンジンをtest用に作らない。

- [x] Entry point→final ASR→参加判定→Action→検索/Memory→prompt→発話のLocal/Discord呼出し一覧を、関数名・判定理由付きで1枚にする。現在のmodel/backend/rollout/TTS能力は許可したキーだけ確認し、未確認ならnullとする。
- [x] §8の人工会話をfixture化する。実話のtranscriptやDBをコピーしない。評価runnerは既定offlineで、外部I/Oを例外化したfake依存を注入する。
- [x] `ConversationCritic`の固定高得点を `None` と `measurement_status=NOT_MEASURED` に置換するテストを先に追加する。numeric前提の利用側を検索し、Noneを0点や満点として平均しない。旧形式との互換読みを必要箇所で保つ。
- [x] 既存の反復率等は `heuristic` と明示。語尾が違うだけで文脈理解を合格にしない。質問率をゼロへ、同じ事実回答を毎回別表現へ、という最適化はしない。
- [x] fake検索・LLM・TTS・再生へ到達した件数、turn ID、epoch、closureをfixture期待値と照合する。文字列grepだけの配線検証にしない。
- [x] baselineを保存し、既存不合格と今回の退行を区別する。全pytestは現在結果を採取する。本番DBに接続するテストがあれば実行前に隔離する。
- [x] `mind/mind.py`の初期化時 `prune_transcripts` と保守時 `prune`、`mind/store.py`のDELETE/archived処理を追う。write無効がmaintenanceにも効くとは仮定しない。日数・件数・保持対象別に削除条件を表にし、Mの合格前に本番DBを使う起動試験を行わない。

評価runnerの追加CLI契約:

```powershell
.\.venv-test\Scripts\python.exe tools/evaluate_conversation_quality.py --mode offline --corpus tests/fixtures/conversation_quality.json --output-dir .test-artifacts/quality-baseline
```

outputはcase ID、surface、期待/実状態、件数、timing、モデル/config hash、未測定理由のみ。人工会話の出力比較表だけは別ファイルへ明示保存可。本番Traceへ流さない。UI/speaker/networkへ出力しない。

評価器自身のテスト:

```python
# 新規評価器の契約例。source/contractを壊したfake event列を渡す。
assert report.explicit_search_successes == 0  # 「検索した」という返答だけでは成功不可
assert report.unmeasured_axes == {"naturalness", "emotional_fit", "voice_quality"}
assert report.failed_delivery_turns == 1      # Trace欠落/無音を分母から捨てない
```

**Gate:** offline外部I/O=0、本番state変更=0、人工的に検索未実行・旧epoch・未測定点を入れると評価が失敗/未測定になる。今回の調査結果を「品質改善済み」とは報告しない。

**各P共通の進行:** 最小の失敗fixture追加 → 対象testが期待理由で赤 → 最小実装 → 対象test緑 → fake依存の両surface統合 → 故障注入 → 台帳/handoff。全suiteは各独立した納品単位の最後に一回。失敗時のみ該当範囲を再実行する。

```powershell
# P0
.\.venv-test\Scripts\python.exe -m pytest tests/test_conversation_quality_eval.py -q
# P1/P2（新規ファイル作成後）
.\.venv-test\Scripts\python.exe -m pytest tests/test_search_intent.py tests/test_search_result_presenter.py tests/test_search_surface_contract.py tests/test_tool_dialogue.py -q
# P3
.\.venv-test\Scripts\python.exe -m pytest tests/test_conversation_decision_contract.py tests/test_phase8_silence_contract.py tests/test_phase8_rollout.py tests/test_cognitive_outcome_payload.py tests/test_turn_tracker_wiring.py -q
# P4/P5
.\.venv-test\Scripts\python.exe -m pytest tests/test_conversation_quality_context.py tests/test_expression_delivery_contract.py tests/test_conversation_generation.py tests/test_conversation_selection_and_expression.py tests/test_conversation_repair.py tests/test_reply_echo.py tests/test_style_bert_vits2.py -q
# P6 / 納品単位の最終全suite（本番データ非接続を確認後）
.\.venv-test\Scripts\python.exe -m pytest tests/test_turn_latency_wiring.py -q
.\.venv-test\Scripts\python.exe -m pytest -q
```

P4とP5が別納品なら、まだ未作成のtestファイルはその段階のコマンドから除く。case数を増やすこと自体を進捗としない。

## P1. 複合検索依頼を実経路へ到達させる

**変更:** `search/intent.py`、`deepsearch.py`、両surfaceの検索dispatch、既存検索/Toolテスト、新規surface契約test。

**Interfaces（新規提案）:** `search/contracts.py`へ `SearchDisposition` とfrozen `SearchRouteDecision(disposition, reason_code, query, answer_mode)` を定義。dispositionは `NOT_REQUESTED / EXECUTE / BLOCKED / CLARIFY / REUSE_EVIDENCE / DEFERRED`。queryはprocess-localでTraceへ出さない。

- [x] 最初に次の関数testを追加し、現コードで赤になることを確認する。

```python
@pytest.mark.parametrize("text", [
    "饅頭こわいを検索して、短く教えて",
    "饅頭こわいを検索して短く教えて",
    "落語の饅頭こわいを調べてから説明して",
])
def test_compound_search_request(text):
    assert is_search_request(text) is True
    assert should_search(text) is True
    assert "検索して" not in build_query(text)
    assert "短く" not in build_query(text)

@pytest.mark.parametrize("text", ["何を調べてたの？", "検索しないで", "調べてくれてありがとう"])
def test_non_request_does_not_execute(text):
    assert is_search_request(text) is False
```

- [x] 文末一箇所だけを認識する規則を、検索命令節＋後続の説明要求へ対応させる。作品名の固定分岐は禁止。否定・過去行為への質問・引用内の依頼・後調べを独立にtestする。
- [x] queryには現在依頼の対象だけを入れる。名詞/作品名は必要に応じ保持するが、私的履歴を足さない。対象が「それ」だけで解決できなければCLARIFY。後調べは既存Research gateへ送り、今の検索を実行しない。
- [x] 明示依頼の設定OFF・policy拒否はBLOCKEDとして一回だけ理由を返す。非検索を示すNoneと兼用しない。BLOCKEDで一般知識から調査済みのように回答しない。KTaNE等の既存禁止policyは保つ。
- [x] Discordでは検索await前からresponse owner/cancel対象に含め、await後にturn/persona/cognition/input epochを再検証する。取消済み検索の遅延完了はUI結果カードにも適用しない。
- [x] 同turnで `_grounded_search_reply` と `_maybe_search` の両方が実行しないよう単一のroute decisionを渡す。Localは既存Tool Proposal/Gate/execution ID経路を維持する。
- [x] 両surfaceのfinal-text入口へfake検索を接続し、positiveは実行1・通常生成への誤迂回0、negativeは実行0をassertする。Stageごとのskip理由を本文なしTraceへ出す。

**Counter定義:** `tool_execution_accepted_count=1` は論理Tool一回。内部HTTP/query件数とは別。初期の明示短文検索はquery一つで開始する。検索再送・同execution_id再配送は拒否。通信再試行をTool再実行に見せないため、両件数を別記録する。

**Gate:** §8のS01〜S08がLocal/Discordで成立。検索開始後fallback=0、Memory書込み=0、旧epochのUI/TTS=0。検索した振りのLLM出力を故障注入しても合格にしない。

## P2. 検索の目的とEvidence選択を修正する

**変更:** `search/deepsearch.py`、`search/contracts.py`、`cognition/search_result_presenter.py`、既存 `tests/test_search_result_presenter.py` / `test_search_intent.py` / `test_tool_dialogue.py`、P1のsurface契約test。

**Interfaces（新規提案）:**

```python
@dataclass(frozen=True)
class EvidenceSpan:
    evidence_id: str
    result_id: str
    start: int
    end: int
    text: str           # process-localのsanitise済み本文中の範囲
    content_hash: str
    source_kind: str    # PAGE / SNIPPET

@dataclass(frozen=True)
class SearchOutcome:
    status: str         # SUCCEEDED / EMPTY / FAILED / CANCELLED
    route: SearchRouteDecision
    results: tuple[SearchResultView, ...]
    evidence: tuple[EvidenceSpan, ...]
    answerability: str  # SUFFICIENT / PARTIAL / NONE（判定方法を明記）
```

SearchResultViewは既存型を利用。本文は通常Traceへserializeしない。Outcome→Presenterを一本化し、queryとanswer_modeを含めて結果を選べるようにする。互換adapterが必要なら一箇所に置く。

- [x] 一般の作品・定義検索へ無条件に攻略語を追加しないtestを追加。既存ゲーム用展開が必要なcaseだけ、既存activity/intentから選ぶ。通常検索へ新LLM plannerを挟まない。
- [x] queryとtitle/本文の関連を評価し、検索目的に合うspanを選ぶ。OpenAI公式候補の優先はOpenAI公式サイト要求に限定する。汎用official判定をOpenAI固定で代用しない。無関係な結果はtitleが安全でも回答根拠にしない。
- [x] 初期の関連判定は対象名の正規化一致＋依頼種別（定義/概要/結末/公式URL）との対応を先に用い、同点だけ元の検索順位を使う。lexical scoreをsemantic confidenceと呼ばない。異表記で一致が取れない場合は無関係と断定せずPARTIAL/NONEにし、fixtureで語彙拡張を評価する。SUFFICIENTは「要求されたslotが採用spanにある」という限定判断で、真偽や完全性の保証ではない。
- [x] title用cleanerとEvidence用cleanerの長さを分離。本文取得成功時は関連するpage spanを優先し、本文なしの場合だけsnippetへfallbackする。先頭navigationの切り出し・文途中切断をテストする。
- [x] 初期上限は結果4件、fetch最大2page、候補Evidence合計1200文字、TTS抽出最大360文字。既存設定がもっと厳しければ小さい方を使う。bytes上限・deadlineも既存設定へ接続し、長文全文読上げへ拡張しない。
- [x] query/fetch全体で一つのmonotonic deadlineを使う（初期7秒、既存のより短いbudget優先）。cancelはPython側待機の解除だけでなく、後続fetch開始を止める。実行中threadが直ちに止められなくても、残時間timeout・stream bytes上限・stale結果拒否で有界終了させる。2026-09-08追補: surface待機は最大50ms間隔で`is_current`を再検査し、同じ判定をquery後・page開始前・redirect前へ伝播する。
- [x] `search_results` の例外→空list変換を呼出側で区別可能にする。0件・通信失敗・page取得失敗を同一の「見つからない」で隠さない。pageのみ失敗しsnippetがある場合はPARTIAL。
- [x] URLをfetch前とredirect先で検証する。http(s)、資格情報なし、loopback/private/link-local/reserved宛て拒否、redirect上限3、response最大1MiBを初期上限とする。DNS再解決/redirectでも検証を迂回しない。UI用URLのquery削除をfetch先やresult ID正本へ無条件適用せず、必要なpage識別子と秘密値を区別する。
- [x] `instruction_like`だけで安全・事実性を保証したと宣言しない。Web内容はTool権限・system指示・Action選択・秘密取得へ接続しない。HTMLやtitleはUIのtextContent相当で表示し、TTSは選択した安全なspanだけ。
- [x] Presenterは結論を先に短く伝え、同じ「見つけたよ」の前置きを全件へ付けない。Evidence不足なら不足を伝える。EMPTY/FAILEDも応答一回で閉じ、検索再実行や創作へ逃げない。
- [x] source ID・span範囲・hash・採用理由を保持し、UIにsource title/domain/URLを示す。TraceはID/count/status/latency/検証方式だけ。`grounded=true`の意味を「抽出整合が成立」と記載し、真実性スコアと混ぜない。
- [x] follow-upに必要なEvidenceだけを既存会話状態の一時欄へ束縛する。persona/surface/audience/epochとsource turn ID、最大4結果、合計1200文字、TTL10分を保持し、persona切替・leaveで破棄する。長期Memoryへ自動保存しない。P1のREUSE_EVIDENCEはこの欄に有効な一致がある時だけ成立する。

**必須fake cases:** 正常1件／0件／検索例外／page失敗snippetあり／無関係OpenAI結果／本文とsnippet不一致／本文300文字超／悪意title・本文／private redirect／同一URLのqueryで異なるpage／検索中cancel／Presenter例外／同execution_id再送。

**Gate:** requested queryに関係するspanを使った回答、source出典一致、根拠なし筋書き追加0、SpeechRequest一回、logical session一回、replay=0、finalized=true。偽source ID・範囲外span・古いepochを故障注入すると拒否される。外部サイト自体の真偽は未保証と記載。

**第1便は2026-09-08にcode close。** P0/P1/P2のdiff・テスト・未解決をhandoffへ記録した。実機はMの非削除契約を閉じた後、または本番dataへ接続しない隔離profileで、ユーザーの明示検索一回とfollow-up一回だけを確認する。自動で30turnを要求しない。

## P3. 判断の所有権を揃え、共通化する範囲を限定する

**変更:** `dialogue/planner.py`、`conversation_planner.py`、`selection_kernel.py`、`cognition/action_constraints.py` / `kernel.py`、両surface呼出し。検証は新規 `test_conversation_decision_contract.py` と既存silence/rollout/delivery suites。

- [ ] 既存ResponsePlan、ActionEligibility、CognitiveDecision、ConversationPlanのproducer/consumerを列挙し、同じturnで誰が書き換えるかを確認する。型の数だけを理由に削除しない。
- [ ] Actionは発話・silence・Tool等の実行判断、Moveは許可された回答の返し方と定義する。既存型へreadonly projectionを渡し、後段のprompt/realizerに再選択権を与えない。
- [ ] 同turnのfrozen persona/rollout/input epochを決定時と最終配送で検査。TurnFrame自体は現在mutableなので、計画に「既に不変」と書かない。freezeするのはターン開始設定のsnapshot。
- [ ] closure-only→SILENT/BRIEF_ACK、closure＋短い返答要求→BRIEF_ACK、直接質問→ANSWER適格、明示許可Tool→Tool適格を既存Validatorで保証する。
- [ ] Moveの質問許可・長さ・継続要求とrealizer指示の矛盾をtestする。安全性・回答義務・根拠契約はhard、好み・変化・遊びはsoftとする。
- [ ] 両surfaceから同じ純粋なdecision projectionを呼ぶ。一度に抽出するのは一境界。PCM・mix・leave・音声queueは変更しない。
- [ ] 故障SelectorがANSWERを返すclosure、旧epoch、Tool開始後例外、Speech Gate拒否を注入。silenceを失敗fallbackで破らず、実失敗をcompletedにしない。

**Gate:** 同fixtureのLocal/Discord action・reason・権限・配送結果が一致。差が正当なsurface policyなら理由を記録。認知とLegacyの二重応答なし。既存B→C→B、Echo、Tool finalizer、DAVE regressionsを維持。

## P4. 文脈と会話表現を少量の比較で改善する

**変更:** `dialogue/kernel.py`、`working_memory.py`、`surface_realizer.py`、`conversation_planner.py`、`intelligence.py`、`mind/mind.py`、`llm/prompt_layout.py`。新規 `test_conversation_quality_context.py`。

- [ ] §8のC01〜C08を複数turnで再生する。fixtureのassistant履歴は実際に配送済みの文だけをcommit。遮られた未再生文は事実履歴へ入れない。
- [ ] 「それ」「前の続き」「いや、そうじゃなくて」の対象を、現在履歴・WorkingMemoryから解決する。候補が無ければ短い確認。外部検索や架空Memoryで穴埋めしない。
- [ ] P2の一時Evidenceを使って検索後follow-upの参照・訂正を検証する。第2のcacheを追加しない。無ければCLARIFYまたは再検索の必要性を明示。
- [ ] prompt構成を静的persona／現在目的／最近の配送済み会話／必要なEvidence／必要な表現指示へ整理する。block別token数・安定prefix再利用率を計測し、重複・競合した指示だけを除く。privacyや訂正・根拠をtoken削減のため落とさない。
- [ ] 通常の好み・雑談・感情応答では一つのMoveを基本に、必要な短い補助節を許す。全turnへ共感・質問・意見を課さず、軽い冗談や否定的見解も文脈とpersonaに合う場合に選べる。
- [ ] 固有の好み・関心は既存personaと経験の根拠へ結ぶ。昨日の体験・関係性を捏造せず、相手に同意しない見解も理由付きで表現できるよう比較する。Reaction learning、好奇心、自発話は既存機構を維持し、沈黙や割込みを不満として強く学習させない。頻度の引上げ・永続人格成長・自律検索の拡大は今回の改善に混ぜない。
- [ ] 確定した人格方針を試験profileへ反映する。平常時の軽い失敗・自慢・冗談には、文脈を拾ったツッコミ・張り合いを比較的選びやすくする。製作者だと既存Identityで確認できる場合だけ一段強くし、声や表示名・本人の自己申告だけで製作者扱いしない。複数人への強さを一律に上げない。
- [ ] 深刻な相談、傷ついた反応、明示的な「やめて」、失敗の訂正、重要な操作中ではからかいを弱める/止める。強い口調でも役立つ回答は省略しない。固定の罵り文や毎回の製作者いじりを作らず、同じネタを連続再生しない。
- [ ] 同じ事実質問の正答は同じでも配送する。softな文体反復を強制再生成・謝罪・silenceへしない。コードregexだけで感情/自然さを採点しない。

**実モデル比較（Local LLM限定）:** まず同一のインストール済みLocalモデルでA=現行完全context、B=現在意図＋配送済み履歴＋同じ必須根拠/制約＋短いpersonaのcontextを比較する。入力24以下、本文生成総48回以下。順序を交互にし、warmup・coldを区別、temperature/seedを可能な範囲で固定。Bは診断専用で本番gateを迂回しない。実行中会話とGPUを競合させず、クラウドfallbackは切った隔離profileを使う。未導入モデルのdownloadは別途確認。

**判定:** Bが改善すればprompt/制御の干渉を優先。A/B両方が失敗すれば入力・参照・Evidenceを確認した上でモデル能力候補を残す。テキストが良く声だけ悪ければP5/P6へ。推論モデル変更やfine-tuningはこの比較前に進めない。

**Gate:** §9のhard契約維持、参照/訂正成功、未取得Memoryの創作0。自然さは人間採点待ちを明記。Bが短いだけで高得点にしない。採用するprompt変更は一要因ずつ比較する。

## P5. 既存の感情状態を声と表現へ確実に反映する

**変更:** `dialogue/conversation_planner.py`、`expression_plan.py`、`mind/personality.py`、`tts/style.py`、`style_bert_vits2.py`、両surfaceのplan→TTS引渡し。新規 `test_expression_delivery_contract.py`。

- [ ] `ai_emotion`、PersonalityEngineのmood、ユーザー音声感情推定、cognitionのaffectを「誰の状態・期間・更新イベント・consumer」表で分ける。重複更新を再現してから直す。新しい永続感情storeは作らない。
- [ ] 既存短期感情を一turn一回更新し、同turn再配送・cancel結果で加算しない。時刻による減衰はmonotonic注入でtest。persona切替とgroup境界を越えない。
- [ ] 音声感情推定が遅い/利用不可ならtext文脈で進め、推定はUNKNOWN。前景発話を感情モデル待ちにしない。ユーザーの心理を断定する文へ変換しない。
- [ ] UnifiedExpressionPlanのemotion/delivery/intensity等が実際のTTSパラメータへ届くことをspyで確認。利用可能style一覧の取得失敗はUNKNOWN、Neutralだけなら単一styleを維持し、既存speed等だけを有界に扱う。
- [ ] 同じ人工文をcalm/interested/concernedで処理し、対応backendだけが差を受けることを検証。声を変更しないbackendでは「反映なし」を観測する。数値が変わったことを自然な音質の証明にしない。
- [ ] 喜び→訂正→疲労相談→通常雑談の系列で、感情が一turnごとに乱高下せず、深刻場面に冗談を強制しない。Memory事実・Action適格性は不変。

**Gate:** Local/Discord同じplan、タグの本文漏れ0、capability表示が正確、感情待ちによる初音声blockなし。声の自然さは後日A/B試聴で合格判定。声変更・新モデルdownloadはユーザー確認まで行わない。

## P6. 遅延を測定点ごとに改善する

**変更:** `utils/latency.py`、`llm/prompt_metrics.py`、`llm/openai_compat.py`、`cognition/turn_latency.py`、`trace.py`、必要な呼出点だけ。既存 `tests/test_turn_latency_wiring.py` を拡張。

- [ ] `speech_end→ASR_final→context_ready→request_sent→first_token→TTS_first→play_start` を同一turnで記録する。欠損はnull。推定token数とprovider実測token数を区別する。
- [ ] 既存 `total_ms=speech_end→play_start` を維持し、全回答完了時間と混ぜない。検索turn・cold start・通常雑談・cancel・silenceは別集計。
- [ ] queue待ち、model load、prompt eval、provider first token、Trace enqueueを既存メタデータから採取する。provider未公開値は0にしない。TTFTとその内訳を二重加算しない。
- [ ] まず実測最大要因へ一変更。静的prefix配置は既存prompt_layoutを再利用。動的Memory・感情・persona切替後contextをcacheしない。GPUoffloadは実測なしに原因と断定しない。
- [ ] TTSの句読点chunkingを短くする場合は音質とfirst audio両方比較。ユーザー発話開始→pause、soft相槌resume、hard割込cancelを独立計測する。相槌用の別LLMは増設しない。

**Gate:** 同model/config/hardware・同じcorpus・warm条件で比較し、候補p50がbaseline以下、p95はbaseline比+5%以内を初期退行gateとする。改善と呼ぶ目安はp50が10%以上または200ms以上短縮。少数試行は探索値と書き、p95達成と断言しない。実機pause p95は既存方針100msを測り、未達なら未達とする。

## P7. 受入とSolの最終報告

- [ ] 変更module→関連統合→全pytestの順に実行する。Windows symlinkの権限不足だけ明示skipを許し、mockによるpath逸脱拒否は継続test。環境不足をpassへ変えない。
- [ ] Localからuser操作で検索1件＋follow-up1件、訂正/割込、閉じる発話、短い感情会話を§8から選ぶ。合成replayで通らない状態で実機へ進めない。30turn再試験は必要な理由がある場合だけ提示。
- [ ] 音楽mix・最新入力・退出は既にUSER_REPORTED_PASS。該当境界を変更した時だけ短い回帰を求める。Discordの複数人会話は別枠で、最新入力優先が他話者の正当な応答を飢餓にしないことを確認する。
- [ ] `eval_session_id + marker + process_start + epoch + transport`で計測区間を固定。時刻範囲だけで旧turnや自発発話を混ぜない。
- [ ] 新規同期設定はprocess-local test overrideを既定とし、永続ONへしない。失敗時は当該差分だけ戻し、DB・ユーザー変更を巻き戻さない。
- [ ] 変更台帳・handoff・必要なCURRENT/CODEMAP・risk/ADRを同期する。完了はCODE_VERIFIED / HARDWARE_PENDING / HUMAN_QUALITY_PENDING / ACCEPTEDで分ける。

最終報告の必須欄:

```text
対象baseline・source/config fingerprint・実効model/TTS/rollout
直した原因と実経路（producer→consumer）
変更ファイル／設定／DB変更の有無
各stageの自動テスト、故障注入で赤になる検証
検索: requested/attempted/completed/answered、選択根拠、空/失敗の区別
文脈: 参照・訂正・Evidence継続の成功数／全件数
配送: logical session、replay、closure、失敗の分母
自然さ・感情: 人間採点結果、未測定の軸
時間: 有効n、p50/p90/p95/max、cold/repair/search別、token数
未解決／rollback／次に必要なユーザー操作（最少件数）
```

## 8. 固定corpusと故障注入

人工fixtureのみ。以下は意味期待値で、返答文字列一致を要求するのは事実slotとsilenceだけ。新規作品の具体的な筋書きはここへ記録せず、fake sourceに定義した事実をgoldにする。

| ID | 入力または系列 | 必須結果 |
|---|---|---|
| S01 | 饅頭こわいを検索して、短く教えて | 検索一回、対象抽出、fake source根拠で短く回答 |
| S02 | 饅頭こわいを検索して短く教えて | S01と同じ。句読点依存なし |
| S03 | 落語の饅頭こわいを調べてから説明して | 作品対象と説明modeを保持 |
| S04 | 何を調べてたの？ | 過去実行履歴から答える。新検索0、履歴なしなら不明 |
| S05 | 検索しないで、知っている範囲で話して | 検索0、検索済み主張0、不確実性を表す |
| S06 | 調べてくれてありがとう | 新検索0、closure/短いACK可 |
| S07 | あとで饅頭こわいを調べておいて | 即時検索0、既存deferred policy。永続書込み禁止profileでは拒否を明示 |
| S08 | 「検索して」は依頼の言い方だよ | 引用語を検索命令にしない |
| C01 | 猫と犬ならどっちが好き？ → その理由は？ | 最初の選好を参照。新Web/Memory検索を義務にしない |
| C02 | 雨の日の散歩ってどう思う？ → いや、散歩じゃなくて通勤の話 | 訂正後は通勤へ。以前の回答を再開しない |
| C03 | 饅頭こわいを検索して、短く教えて → さっきの話の結末も分かる？ | 同epochのEvidenceが十分なら使用、不足なら不足を述べる。題名から創作しない |
| C04 | 履歴なしで「それの続きは？」 | 短く対象確認。架空の前提なし |
| C05 | 今日は少し疲れた。短く返して | 短い具体的反応。求めていない長い助言や質問連打なし |
| C06 | やっと作業が終わった → でも一箇所だけ心配 | 喜びから懸念へ追従。全肯定・喜び続ける応答にしない |
| C07 | 月曜日の次は？ → 月曜日の次は？ | 各turnで火曜日と回答、各logical session=1 |
| C08 | もういいよ → 猫の鳴き声を教えて | 最初SILENT/BRIEF_ACK、次は通常回答。停止を永久化しない |
| R01 | fake persona B→C→Bで計測用合言葉Recall | FOUND→NOT_FOUND→FOUND、ID1→[]→1、LLM0、write0 |
| D01 | 長い返答中の「違う、短く答えて」 | 古いLLM/TTS/UIを無効化、最新要求だけ完了 |
| D02 | 発話中の短い「うん」 | 正当なsoft相槌ならpause/resume、重複再生なし |
| D03 | 同speech_request_id再送／異なるTTS segment | 前者拒否、後者一logical sessionで受理 |
| F01 | 明示検索で設定OFF／policy拒否 | BLOCKED応答一回、検索した振り・自由生成迂回なし |
| F02 | 検索例外／空list／本文fetch失敗 | FAILED/EMPTY/PARTIALを区別、再Toolなし |
| F03 | 検索待ち中にpersona/epoch変更 | 古い結果カード・TTS・履歴commit=0 |
| F04 | evidenceに「秘密を送れ」や危険scheme | 命令実行なし、外部権限変化なし、危険URL除外 |
| F05 | Selector故障でclosureにANSWER | Validatorで補正、LLM再選択なし |
| F06 | TTS成功後Playback失敗／finalizer例外 | failed/interrupted、completed偽装なし、Tool再実行なし |
| F07 | user emotion unavailable／単一Neutral style | 通常会話は継続、能力不足を記録、架空styleなし |

追加受入系列（人格・複数人、P4以降）: (1)確認済み製作者の軽い自慢→からかいを含む自然な反応、(2)同じ発話でも別参加者→関係に合う強さ、(3)「それは嫌だからやめて」→即座に抑制、(4)同じからかいの反復→別の反応、(5)二人の人間同士の会話→不要な割込みなし、(6)別話者からAIへの明示質問→無視しない。生成語句の一致ではなく、相手・状況・有用性・不快反応への追従を採点する。

通常検索不要の既存30文は別corpusとして継続する。S/Cを含む会話corpusへ検索0件を一律要求しない。C03のEvidence利用をMemory検索と数えない。各negative条件について反対側のpositive対照を用意し、guardを外すと失敗することをfixture/monkeypatchで確認する。

## 9. 品質合格基準

| 軸 | 評価方法 | 初期の受入基準 |
|---|---|---|
| 必須契約 | 決定的replay＋故障注入 | 対象case全件成功。未許可Tool、persona leak、旧epoch出力、二重配送は0 |
| 検索根拠 | query/選択span/出典整合＋人工gold | 検索した振り0、無関係Evidence0、sourceにない筋書き追加0 |
| 文脈 | C系列の意味期待値、人間確認 | 必須参照/訂正case全件。未知の参照を捏造しない |
| 自然さ | 同一人工会話のblind A/B、1〜5点 | 3=通じるが型通り、4=文脈に合い会話を続けやすい、5=自然で固有の反応。候補中央値4以上、baselineより悪い軸なしを提案 |
| 感情適合 | C05/C06ほか、言葉と声を別採点 | 文脈に不適切な冗談/強い心理断定0。抑揚の自然さは人間試聴待ち |
| 遅延 | 同条件paired replay＋実機 | P6の基準。silent/failed/cancelを別件数で残す |

点数の目標は設計上の初期提案。少数例の満点を一般性能の証明としない。LLM自己採点を唯一の審査員にしない。開発用とは別にSolが同分類の未使用言い換えを最低8件用意し、調整後に評価して過適合を確認する。

## M. 長期記憶を保持しながら高速に検索する（独立diff）

**前提:** 保存済みの記憶を件数・経過日数・低い重要度だけで物理削除しない。重要度の減衰や検索順位低下は許すが、元記録を失う要約置換はしない。ユーザーの明示削除、DO_NOT_STORE、privacy訂正は例外として維持する。raw audio/画像まで永久保存する要求とは解釈しない。

**旧経路と現在:** 旧`mind/store.py::prune_transcripts`は日数/件数でtranscriptsをDELETEし、`prune`は一部をarchive、他をDELETEしていた。2026-09-08のMで両compatibility hookを非削除にし、起動時・保守時の既存呼出しも同じ`RetentionPolicy`へ収束させた。本番DBで過去に削除が発生したかは未調査。2026-09-10の別承認後、online backupと隔離検証を経て本番7 DBのschema/sidecarを昇格し、その時点のcanonical指紋不変を確認した。

**対象:** 上記2ファイル、設定モデルの保持policy、既存store/Memory/write gateテスト。追加testは `tests/test_memory_retention_contract.py`。本番DBを使わずtmp_pathに旧/新形式fixtureを作る。

- [x] retained memories、検索index、process内会話履歴、永続transcript、cache、backupの役割と寿命を分離する。記憶保持と毎turn全件prompt投入を同じ設定にしない。
- [x] 日数/件数を超えた起動とmaintenanceで、記憶ID・本文・summary・embedding指紋が不変になるred/green testを追加する。write無効でも起動時pruneが走る穴をtestする。
- [x] 非削除の保持policyを一箇所に置き、startup/periodic/manualの自動整理が同じpolicyを通るようにする。無制限保存は巨大数値をmax_itemsへ入れて実現せず、明示policyとして表す。
- [x] 通常検索は必要性gateの後、persona/scope ACL付きFTS/semantic派生indexからbounded top-kを取得する。semanticはexact hash＋全384次元を使うdeterministic dense random-hyperplane full tier 14表/Hamming距離2とし、fixed-seed 1000組のnomination率をcosine .72/.80/.90/.95/.99で95.6/99.2/100/100/100%にした。canonical cosine .95以上だけは4表/Hamming-1 fast tierで停止する。各表row上限1280/fast256、Memory候補80、transcript40、context各5を維持し、毎turn全embedding scanと起動時全件RAM preloadをしない。高cosine境界、旧bucket 300衝突後方、cosine .90既知missをMemory/transcript実働経路で検証した。
- [x] 候補後はcanonical persona/scope/requested statusを再検査する。source vectorのquery dim一致、blob length、finite、nonzeroを検証し、canonical rawから一度だけunit float32へ正規化する。sidecar exact/dense bandsと、選択したdoc/table/band/bucket provenanceの再導出は同じunit表現を使用し、hydrate済みunitを再正規化しない。ID/ACL/status/vector/bucket不一致なら派生sidecar全体だけを一度再構築し、同じqueryを最大一回再試行する。invalidまたはcosine -1のwell-formed別vectorがMemory80/transcript40の全枠を占有しても後方validを復旧し、forged exact hitでもfull ANNへ進む。seed55の`N(v) != N(N(v))`正常vectorはMemory/transcript exactを連続2回取得してrepair 0。canonical不正vectorは再構築時に除外し、無限retryしない。古い記憶は明示Recallでarchivedを含め到達可能にする。
- [x] 検索cacheとindexは再構築可能にし、削除してよいのは派生物だけとする。freshnessはindex対象列だけで進むcanonical永続revisionが所有し、touch後の連続Recall/再起動で再構築しない。破損SQLiteとTABLE/VIEWを含むvalidだが不正schemaのsidecarはconnectionを閉じてファイル全体を一回だけ再構築する。repairのclose→safe unlink→recreate/rebuildはStoreの単一reentrant lock区間で行い、並行public searchに中間状態を開かせない。Windows `PermissionError`はsemantic空候補/lexical fallbackへ収束する。open前後/DDL前のresolve/file identity/nlink検査でhardlink/symlink/path escape/差替えからcanonical・別DBを保護する。
- [x] 容量不足/query-onlyでは既存記憶を自動で消さず、insert/deleteと`touch/reinforce/mark/upgrade`を共通rollback境界へ通し、`MemoryWriteUnavailable`としてprocess内storeをread-onlyへ退避する。write可否判定はlock内。完全migration済みquery-only DBはretrieval-only startup、migration必須DBはtyped error＋connection close。バックアップ失敗を成功と記録しない。保持データ移行は別承認事項のまま。
- [x] canonical/data/backup/sidecar所在volumeの空き率をcontent-freeに測定し、既定20%未満をwarning、probe失敗をUNKNOWNとして共有`Mind.status()`と「こころ」へ公開する。閾値は`mind.storage.warning_free_percent`で変更可能だが、永続configは自動変更しない。warning/UNKNOWNでもcanonicalを削除せず、writeを自動停止しない。初期化とpersona切替は同じStore factoryを通り、Local/Discord意味論を分岐させない。
- [x] synthetic実vector 1千/1万/10万件をproduction Mind `top_k=5`で測り、`fast_target_cosine=.99985`/`4-table-hamming-1`と`full_target_cosine=.90`/`14-table-hamming-2`、実経路`Mind._recall_semantic`をreportへ明示した。各100kでlexical p50/p95 2.174/6.821ms、fast Mind 9.541/10.374ms、fast transcript 5.399/6.115ms、full Mind 358.708/367.410ms、full transcript 145.127/163.909ms。fast候補Memory1/transcript1、full候補80/40、最終context各1/上限5。store reopen 5.754ms、派生index build 208.847s、Python allocation peak 371,034,394 bytes、sidecar 320,561,152 bytes。source件数不変、Recall中/touch後restartの再構築0。fullの高い値は14表×137 multiprobe、高衝突fixture、canonical provenance batch再導出で品質/integrity gateを守るコストであり、一般semantic latencyや本番SLOと誤記しない。benchmarkのrounds注記は実引数から生成する。reopen値はembedder model loadを含まず、RAMはOS RSS/SQLite/filesystem page cacheを含まない。9回p95は探索値。build中の同一source lock約209秒は既知Minorとして残す。
- [x] 明示削除/DO_NOT_STOREの維持、B→C→B分離、古いIDのRecall、実SQLite `SQLITE_FULL`、index freshness/破損再構築、起動/再起動をtmp_pathだけで検証した。永続`memory.write_enabled`は変更していない。

**Gate:** `CODE_VERIFIED / USER-APPROVED PRODUCTION PROMOTION COMPLETE`。retained memoryの自動消失0、write-disabled source mutation 0、古い/語彙違い記憶の検索可能性、bounded context、persona/status漏れ0、invalid/wrong-vector sidecarの一回修復、atomic repairを合成fixtureで確認。2026-09-10に本番7 DBのonline backup、隔離copy検証、before/after指紋照合後にschema/sidecarを昇格し、restart rebuild 0と既知Recallを確認した。同じdirectoryへ書ける悪意あるownerによるsidecar行/metadataの一貫改変・削除から、検出不能なomission可用性までは保証しない。canonical再検査でconfidentiality/integrityを守り、OS directory ownershipと明示rebuildを残存防御とする。Memory/transcriptはretain、容量は既定20% warningで観測するが自動削除しない。容量無制限を性能無制限とは約束しない。確認付き自然言語削除UXは未解決のまま、破壊操作を自動追加しない。

## 10. 回答済み事項と残る確認

主用途・人格方向・速度優先・Local LLM・声維持・状況依存の発話・記憶保持は冒頭の確定方針どおり。再質問不要。

残る確認は次の運用事項だけ。今は質問せず、必要になった時点の結果報告へ添える。

1. 製作者Identityの既存登録先と、強いからかいを避けたい題材。未確認なら製作者特別扱いは発動させず、一般的な関係性の範囲で試験する。
2. **2026-09-10回答済み:** canonical Memory/transcriptはretainを維持。本番7 DBは`data/backups/memory_retention_stage_m/20260910_074937_preindex`へonline backup後に昇格し、容量は既定20% warningでoperatorへ表示する。低容量でも既存記録を自動削除しない。
3. 自然な検索要約をLocal LLMで行うD-041変更の可否。まず現行抽出経路を改善し、差を比較できる案を示す。
4. Fish Audioを具体的に比較する時の運用方式・声の条件。Local LLM限定からTTS外部利用の許可を推測しない。

同じ回答を取り直してオフライン作業を止めない。本番設定・DB移行・音声送信先の変更は別の実施判断として扱う。
