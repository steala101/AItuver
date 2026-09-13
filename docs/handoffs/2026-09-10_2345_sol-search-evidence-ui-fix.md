# 検索Evidence・追質問mode・TTS/UI回帰修正 handoff

- Date: 2026-09-10 23:45 JST
- Updated: 2026-09-11 00:05 JST（Astra仕様レビュー修正）
- Agent: Sol（実装）
- Status: `CODE_VERIFIED / LOCAL HARDWARE RE-ACCEPTANCE PENDING / ASTRA RE-REVIEW PENDING`

## 起点と原因

変更前のLocal PROD turn `ec51afe7204e`（`まんじゅうこわいを検索して、短く教えて。`）と`18848aa4a39e`（`さっきの話の結末もわかる?`）は、同じWikipedia navigation boilerplateを返した。最初はTool実行一回・4結果正規化、二回目は検索なしでcached Evidenceを再利用しており、一回実行契約自体は維持されていた。

原因は四つだった。

1. `DeepSearch._fetch_page_text`が全文HTMLの`h1/h2/h3/p/li/dd/dt`を順に集め、本文より前のWikipedia nav `li`で上限を埋めた。
2. Presenterがtitle/query一致でnavigation本文を選び、先頭360文字をBRIEFとして読んだ。UI excerptだけでは後続ENDINGを支える本文も失われた。
3. Local/Discordの`REUSE_EVIDENCE`がcached outcomeの元modeで提示し、現在turnのENDING modeを適用しなかった。
4. `_speak_loop`がHTTP例外文字列と全文発話をUI/logへ渡し、発話入りGET URL/queryが露出した。`.sys`はbodyの`user-select:none`を継承し、長いerror/sourceも折り返せなかった。

## TDD RED

production codeより先に次の12件を追加した。

- realistic Wikipedia HTML: nav `li`より後ろの`main`/`mw-parser-output`本文を抽出し、menuを除外。
- generic HTML: mainなしでも見出し・段落を抽出し、nav listを除外。
- navigation-only pageとclean relevant snippetのranking。
- BRIEFは完全な事実文1〜2文・190文字以下、Outcomeは後続用の有界支持本文を保持。
- ENDINGは支持された結末spanだけ、不支持なら不足。
- Local/Discord follow-upは現在modeを使用し検索/Toolを二度実行しない。
- Tool境界の二重正規化後も、短いUI excerptより後ろの支持されたENDINGを利用。
- TTS HTTPErrorのUI/logに全文URL/query/発話/bodyを含めず、例外型とHTTP 422を保持。
- `.sys`は選択可能・pre-wrap/anywhere・幅上限、`addSys`とsource表示は`textContent`経路。

初回REDは`10 failed / 1 passed`で、passは既存`textContent`だけだった。TTS fixtureのfakeへ実経路必須callbackを補い、raw URL/utterance露出のREDも確認した。さらに二重正規化fixtureを追加し、支持文をsnapshotから一時的に外すとENDINGが不足へ落ちるRED、復元後GREENを確認した。

## Astra仕様レビュー修正

Astraの初回仕様レビューはCritical 0 / Important 4。production codeを変える前に追加7件を実経路へ入れ、7件すべて期待理由でREDを確認した。

1. Discord `_speak`のHTTPErrorはUI safe eventなし、logに全文発話・URL・tracebackを出していた。Local同様`safe_exception_summary`を通し、UI/logは例外型＋有効HTTP statusのみ、log追加情報は文字数/hashのみとした。
2. `_ArticleTextExtractor`は全start tagをpushし、end tagでtopを盲目的にpopしていた。header内void、mismatched header、main内nav＋voidの3fixtureで後続本文が消えるREDを再現。void elementをstackへ積まず、closing tagと一致するframeまで安全にunwindする。
3. ENDING evidenceが全resultをscanし、primary topicと無関係な桃太郎の結末を借りた。ENDINGをBRIEF primary relevant resultと同一`result_id`へ束縛した。
4. 最初の`結末が有名`というmeta文をactual outcomeとして返した。meta説明語を棄却し、`最後に`等のactual cueと終了actionを優先する。meta-onlyは正直な不足へ落とす。

追加RED/GREENはparser 3、ENDING 3、Discord TTS 1。既存のcurrent-route mode、一回Tool/search、CSS property契約は維持した。

## 実装

- `neuro_voice/search/deepsearch.py`
  - dependency追加なしの`HTMLParser` extractorを追加。
  - `main`、`article`、`role=main`、MediaWiki content ID/classを優先。
  - nav/header/footer/aside/script/style/noscript/template、navigation role、aria-hiddenを除外。
  - generic fallbackはh1/h2/h3/p/dd/dtだけ。global `li`は使わない。
- `neuro_voice/cognition/search_result_presenter.py`
  - navigation boilerplate quality gateとtitle-only score抑制。
  - BRIEFを支持された完全な文1〜2文、最大190文字に限定。
  - UI excerptとは別にbounded/sanitised `supporting_text`をprocess-local Outcome snapshotへ保持し、Tool境界の再正規化を可能にした。UI result、LLM、Trace、永続Memoryへは出さない。
  - normal EvidenceとENDING Evidenceをhash/source bounds付きで作成。ENDINGはprimary relevant resultと同じresultに限定し、meta cueを棄却したactual outcomeだけを提示。なければ不足を明示。
  - `present_search_outcome(..., answer_mode=...)`でcurrent route modeをoverride可能にした。
- `neuro_voice/pipeline.py` / `discord_bridge/bot.py`
  - cached reuse時に現在routeのanswer modeを共有Presenterへ渡す。再検索なし。
  - Local/DiscordともTTS失敗logをsafe summary・文字数・hashへ変更し、UIはsafe summaryだけ。
- `neuro_voice/utils/errors.py`
  - `safe_exception_summary()`を追加。例外型と有効なHTTP statusだけを返す。
- `neuro_voice/ui/assets/index.html`
  - top-level runtimeの`.sys`だけを選択可能、pre-wrap、anywhere折返し、幅上限へ変更。`textContent`を維持。

## GREENと検証

- 新規受入: 合計`19 passed`（初回12＋review 7）。
- focused（search intent/presenter/fetch/Local+Discord surface/TTS error/UI/tool/injection/outcome/delivery）: `222 passed, 1 expected Windows symlink skip in 3.76s`。
- broader relevant（上記＋conversation eval/rollout/trace/Discord/各TTS delivery）: `447 passed, 1 expected skip in 10.45s`。
- changed Python/tests `py_compile`: exit 0。
- full pytest: `3097 passed, 2 expected Windows symlink skips in 130.18s`、failed 0。

skipは`tests/test_memory_retention_contract.py:1934`と`tests/test_tool_dialogue.py:663`で、このWindows accountにsymlink privilegeがない既知環境制約。

## 変更ファイル

- `neuro_voice/search/deepsearch.py`
- `neuro_voice/cognition/search_result_presenter.py`
- `neuro_voice/pipeline.py`
- `neuro_voice/discord_bridge/bot.py`
- `neuro_voice/utils/errors.py`
- `neuro_voice/ui/assets/index.html`
- `tests/test_search_fetch_safety.py`
- `tests/test_search_result_presenter.py`
- `tests/test_search_surface_contract.py`
- `tests/test_error_display.py`
- `tests/test_ui_system_messages.py`
- `docs/SHARED_CHANGELOG.md`
- `docs/CURRENT_ARCHITECTURE.md`
- `docs/CODEMAP.md`
- `docs/DECISION_LOG.md`
- `docs/KNOWN_RISKS_AND_DEBT.md`
- `docs/handoffs/_NEXT_SESSION.md`
- このhandoff。

## 非変更対象

- production DB、backup、config値、persona、既存Trace、raw audio/image、永続`memory.write_enabled`は不変。
- nested stale copy `AItuber/AItuber`は未変更。
- アプリ/Discord/外部検索/実TTSは起動していない。
- Git init/reset/commit/pushなし。top-level `.git`は引き続きrepoとして無効。

## 残存リスクと次の最小手順

- HTML root判定とENDING cueは保守的なbounded heuristic。providerが根拠本文を返さなければ不足回答になり、meta誤採用を避けるため本物の結末文も保守的に不足へ倒す場合がある。抽出整合はWeb内容の真実性を保証しない。
- 実際の検索provider分布、TTS HTTP成功、音声自然さ、UIでのcopy操作、Discord複数人は未再受入。UI testはstatic CSS/DOM source契約で、browser automationはこのbounded fixでは行っていない。
- ユーザー操作でLocalを再起動し、次の2発話だけを一回ずつ確認する。
  1. `まんじゅうこわいを検索して、短く教えて。`: 検索一回、navigationなしの完全な本文短答、source一件、TTS成功または短い安全error。
  2. `さっきの話の結末もわかる?`: 再検索なし、支持された結末だけ、または正直な不足。intro/nav反復なし。
- error/source chipの選択・コピーと横overflowなしも同じprobeで確認する。これが通るまでP3やDiscord実機へ拡張しない。
