# AI作業Handoff

- 担当AI: Claude (Cowork / claude-opus-5)
- Role: investigation / instrumentation
- Task: 初トークンまでの固定費（約1106ms）の切り分け
- Started At: 2026-07-28 (JST)
- Finished At: 2026-07-28 (JST)
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: 実働ルート直接 (R-001のとおりGit未管理)

## 目的

実測式のうち、傾き（プロンプト評価）ではなく**切片**を説明する。

```
初トークン ≈ 1106ms + 0.244ms × (キャッシュされなかったトークン数)
```

未キャッシュ464トークン（評価はおよそ113ms）のリクエストでも1138ms待っており、`同時実行=1` `待ち=0ms` なので我々のリクエスト同士の競合でもプロンプト評価でもない。

## これまでに否定できたこと

| 仮説 | 根拠 | 判定 |
|---|---|---|
| 我々のリクエストの競合 | 全ターンで `同時実行=1` `待ち=0ms` | 否定 |
| プロンプト評価 | 未キャッシュ464tokでも1138ms | 否定（切片としては） |
| 接続と生成の切り分けで分かる | `生成=0ms`。サーバは最初のトークンまで応答を返さない | **計測の設計ミス。分離できなかった** |

## 変更

計測のみ。会話経路の外で起動時に1回走るだけで、送信するプロンプトは変更していない。

| File | Change | Why |
|---|---|---|
| `neuro_voice/llm/ollama_probe.py` | 新規。`probe_ollama()` / `ProbeResult` / `ProbeCall` | OpenAI互換エンドポイントが捨てている `load_duration` / `prompt_eval_duration` / `eval_duration` を、Ollamaのネイティブ `/api/chat` から直接読む |
| `neuro_voice/pipeline.py` | `_probe_llm_latency()` を起動3秒後に1回 | 会話経路の外。ログとステータスへ判定を出す |
| `config/config.yaml` | `llm.probe_on_start` / `llm.probe_delay_s` | 原因が判明したら false にしてよい |
| `tests/test_ollama_probe.py` | 新規18件 | 下記テスト節 |

### なぜこの形にしたか

`send_ollama_options: false` にして再起動し、体感で比べる、という進め方もあった。しかしそれでは

- 1回の再起動で1つの仮説しか試せない
- 「モデルのロード」と「optionsによる再構成」を区別できない
- 判断が体感になる

ので、**極小のリクエストを options 付き／なしで2回ずつ投げ、サーバ自身の内訳を読む**形にした。プロンプトは「こんにちは」の5文字で、評価時間が結論に影響しない大きさにしてある。設定を書き換えず、再起動1回で両方の仮説を判定できる。

### 出るログ

```
LLM遅延の切り分け:
読み込み済みモデル: gemma4:12b-it-qat(9216MB)
  options=有 往復=1180ms (ロード=980 / プロンプト評価=12[5tok] / 生成=45 / 説明なし=143)
  options=無 往復=1160ms (ロード=970 / プロンプト評価=11[5tok] / 生成=44 / 説明なし=135)
  options=有 往復=1175ms (...)
  options=無 往復=1150ms (...)
  判定: モデルのロードに980ms。常駐が切れて毎回読み直している
        （OLLAMA_KEEP_ALIVE と、他モデルによる追い出しを確認）
```

判定はステータス行にも出るので、ログを開かなくても分かる。

### 判定の読み方

| 出るもの | 意味 | 次にやること |
|---|---|---|
| ロードが300ms以上 | 常駐が切れて毎回読み直している | `OLLAMA_KEEP_ALIVE` を長くする。視覚・埋め込みが同じGPUからモデルを追い出していないか確認 |
| options付きが300ms以上遅い | リクエストごとの `num_ctx` でランナーが再構成されている | `llm.send_ollama_options: false`（`OLLAMA_CONTEXT_LENGTH` で担保される） |
| 説明されない時間が300ms以上 | サーバのスケジューラ側 | `OLLAMA_NUM_PARALLEL`、他モデルとの競合を見る |
| 小さいリクエストは速い | 固定費は会話リクエスト固有の何か | プロンプト長以外の差分（media添付、reasoning_effort等）を疑う |

**最初の1回は判定から除外している。** 起動直後のコールドな1発でモデルのロードを毎回結論にしてしまうため。

## 変更しなかったもの

- `send_ollama_options` の既定（true のまま）。**判定が出る前に変えない。**
- プロンプトの内容・順序・量。
- `OLLAMA_KEEP_ALIVE` などの環境変数。判定を見てから。

## 設計判断

1. **推測で1つずつ試すのをやめた。** 今日は「プロンプト評価が主因」「配置が原因」と2回外している（R-037）。サーバが持っている数字を読むのが最短で、しかも証拠が残る。
2. **1回の起動で両方の仮説を判定する。** 設定を書き換えて再起動するサイクルは、ユーザーの手間であると同時に、条件が揃わず比較にならない危険がある。
3. **会話経路の外で1回だけ。** 毎ターン測ると、測定自体が待ち行列を作って測定対象を変えてしまう。

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| `pytest tests/ -q`（sandbox / Python 3.10 + StrEnum shim、音声・GUI依存4件除外） | 906 passed, 3 skipped, 11 subtests | 全実行ログ確認 |
| `tests/test_ollama_probe.py`（新規18件） | pass | ナノ秒→ミリ秒、`/v1`除去、options有無の両方を試すこと、判定4種、コールド1回目を除外すること、サーバ停止時に例外を投げないこと |
| `test_local_audio_liveness.py` + `test_discord_screen_share.py`（sounddeviceスタブ下） | 21 passed | 回帰なし |
| `python3 -m compileall neuro_voice` | exit 0 | — |

- 未実施: **実機での計測**。

## 設定・Migration

- Config: `llm.probe_on_start: true` / `llm.probe_delay_s: 3.0`（新規）。
- DB schema: 変更なし。
- 再起動: **GUI再起動が必要**。

## Rollback

`llm.probe_on_start: false`。起動時のリクエスト1組が消えるだけ。

## 失敗した試行: プローブが動かなかった

初回の実機起動（2026-07-27 23:07）でログに1行も出なかった。

原因は設定キーの読み間違い。

```python
base_url = self._cfg.get("llm.ollama.base_url", "")     # ← None
```

実際の構造は `llm.backends.ollama.base_url` で、`factory.py` は最初から
`cfg.section("llm.backends")` と正しく読んでいる。**すでに動いている読み方が
すぐ隣にあったのに、確認せずに書いた。**

さらに悪いことに、値が読めなかった場合の分岐が黙って `return` していたため、
失敗したことすら分からなかった。省略した時は理由をINFOで出すよう直した。

今日3回目の同じ形の誤り（R-037）。「1回の遷移は正しいが定常状態を見ていない」
「コメントと実装の食い違いを確認しない」に続いて、今回は
**既存の正しい実装を確認せず、キー名を推測で書いた**。

## 次の担当への注意

- **判定が出るまで `send_ollama_options` や `OLLAMA_KEEP_ALIVE` を触らないこと。** 同時に複数変えると、どれが効いたか分からなくなる。
- ロードが原因だった場合、`gemma4:12b-it-qat` は会話と視覚の両方で使われている。視覚推論や埋め込みが別モデルを載せて追い出していないか `/api/ps` で確認すること。
- このプローブは会話経路の外にある。毎ターン測る形へ変えないこと（測定が待ち行列を作り、測定対象を変える）。
- 実機受入項目: (a) `LLM遅延の切り分け` の4行、(b) 判定文、(c) `読み込み済みモデル` に会話用以外が載っていないか、(d) 判定に従って1つだけ変更し、`初トークン` の p50 が2464msからどう動いたか。
