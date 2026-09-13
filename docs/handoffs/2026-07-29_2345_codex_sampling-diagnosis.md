# AI作業Handoff

- 担当AI: Codex
- Role: test / diagnosis / minimal fix
- Task: 台帳の現在地を読んで`tools/check_sampling.py`を実行する
- Started At: 2026-07-29 23:30 JST
- Finished At: 2026-07-29 23:45 JST
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`

## 目的

同一入力が同一出力へ収束する原因が、temperature無効、OpenAI互換層、Ollama native、または実アプリ入力側のどこにあるかを対照実験で切り分ける。

## 調査結果

### 初回実行は無効

`python tools/check_sampling.py`の初回結果は4条件とも「全部同じ」だったが、表示された本文は全件空だった。

本文を複製せずresponse fieldと文字数だけ確認すると:

- OpenAI互換: `finish_reason=length`、`content_len=0`、messageに`reasoning`あり
- Native: `done_reason=length`、`content_len=0`、`thinking_len=502`

診断器は実アプリの`OpenAICompatBackend.request_extra_body()`が送る`reasoning_effort: none`を送っていなかった。native対照にも`think: false`がなく、120 tokenを内部思考だけで使い切っていた。

### 修正後の有効な結果

モデル既定:

```text
temperature 1
top_k 64
top_p 0.95
```

| 条件 | 24文字以内の書き出し種類 |
|---|---:|
| OpenAI互換 temp=0.7 + options（実アプリ同等） | 3/3 |
| OpenAI互換 temp=0.7 optionsなし | 3/3 |
| OpenAI互換 temp=1.0 | 3/3 |
| Ollama native temp=0.7 / think=false | 3/3 |

全条件でsamplingは機能している。temperature 0.7、OpenAI互換層、`options`、Ollama nativeのいずれも「毎回同じ」の原因ではない。

## 変更

| File | Change | Why |
|---|---|---|
| `tools/check_sampling.py` | `reasoning_effort=none` / `think=false` | productionと対照の思考条件を一致 |
| `tools/check_sampling.py` | 空本文を判定不能にする | invalid sampleを「同じ」へ数えない |
| `tools/check_sampling.py` | stdout errors=replace、CP932非対応ダッシュをASCII化 | Windowsで診断を最後まで実行 |
| `docs/SHARED_CHANGELOG.md` | 現在地・結果・次の一手を更新 | Codex/Claude共通引継ぎ |
| `docs/KNOWN_RISKS_AND_DEBT.md` | R-037へ誤診断パターンを追加 | 同型再発防止 |

会話本体、設定、DBは変更していない。

## テスト

| Command | Result |
|---|---|
| `python tools/check_sampling.py` | 成功。4条件すべて3/3種類 |
| `python -m py_compile tools/check_sampling.py` | 成功 |
| `pytest tests/test_no_undefined_names.py -q` | 1 passed / 2 skipped（pyflakes未導入） |

## 結論

単調さの主因はsampling機構ではない。bare promptは同じrequest parameterで揺れるため、実アプリが毎ターン構成する長い入力、具体的な直近表現を反復回避へ参加させていないこと、または両方が候補になる。

次は新しい会話エンジンや抽象styleを追加せず、`recent_replies`の直近N件から具体的な書き出しをturn-localに提示し、「これらとは別の入り方」を求める最小A/Bを行う。同じ8文の冒頭ハッシュで効果を判定する。

## Rollback

`tools/check_sampling.py`の`reasoning_effort`、`think`、空本文guard、stdout guardを戻す。ただし戻すと診断が空本文をsampling不全と誤判定する。

## 憲法・ADRへの影響

- Constitution amendment required: なし
- ADR added/updated: なし
- Architecture/Codemap updated: CURRENT runtime不変のため不要
- Risk updated: R-037

## 秘密・個人情報確認

- `.env`値を記録していない
- raw audio/imageを記録していない
- private transcriptまたは診断応答本文を文書へ複製していない

## 次の担当AIへの一文

samplingは正常。次は直近の具体的な発話表現を反復回避へ参加させる最小A/Bであり、会話エンジンやprompt規則を増やしてはいけない。
