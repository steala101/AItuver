# AI作業Handoff

- 担当AI: Claude (Cowork / claude-opus-5)
- Role: investigation / instrumentation
- Task: 応答テンポの遅さの原因を特定するための計測（送信内容は変更しない）
- Started At: 2026-07-27 (JST)
- Finished At: 2026-07-27 (JST)
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: 実働ルート直接 (R-001のとおりGit未管理)

## 目的

ユーザーの評価: 「ネウロ様やEvilと比べると見劣りする。会話のテンポ、受け答えのセンスが全く届いていない。今はまだこちらの投げかけた質問に答えるだけ」。

センスの問題として人格プロンプトを触る前に、まず遅延を測った。

## 調査結果

### 遅延の内訳（`logs/latency.log`、2026-07-27 20:28〜20:36、実機）

```
LLM初回   p50=2540ms  p95=3144ms   ← 合計の 80〜85%
STT       200〜300ms
Context   150〜300ms
Speaker    60〜130ms
TTS初回   230〜490ms
合計      3500ms 前後
```

人間の会話のターン間隔は約200ms、参照目標のGPT Live級は1秒前後（憲法1.3）。3.5秒の間があると、内容の質に関わらず「答えている」以外の質感にはならない。**「センスが届いていない」と感じられている現象の相当部分がテンポである**というのが現時点の見立て。

### プロンプトの大きさ

```
文脈長のため古い会話を38件除外 (見積10062tok / 予算6912tok / ctx=8192)
```

直近53件の平均見積は9652トークン。切り詰め後は**毎ターン予算上限の約6900トークン**を送っている。ローカル12Bで7000トークンの評価はおよそ2.5秒で、観測値と一致する。

### 常時ONの指示文の内訳（本セッションで静的計測）

| ブロック | 文字 | tok |
|---|---:|---:|
| persona (`build_system_prompt`) | 1933 | 1789 |
| `ConversationPlanner.prompt` | 1120 | 738 |
| `SurfaceRealizer.prompt` | 664 | 608 |
| 確信度（確信時） | 67 | 71 |
| 継続依頼ブロック（物語メモ込み） | 1085 | 916 |
| **常時ONの合計（確信時）** | | **約3200** |

プロンプトの約半分が、毎ターン同じ内容の指示文である。

### 仮説: KVキャッシュが一度も効いていない

推論サーバは、前回リクエストと**先頭から一致している範囲**についてKVキャッシュを再利用できる。ところが `pipeline.py` は毎ターン変わるブロックを index 1（persona の直後）へ挿入している。

```python
# Placed last so it sits closest to the user turn: ...   ← コメント
position = 1 if messages and messages[0].get("role") == "system" else 0
messages.insert(position, {"role": "system", "content": asr_block})   ← 実装は先頭
```

**コメントと実装が食い違っている。** 実際は先頭付近に入るため、persona 以降が毎ターン別物になり、再利用できる範囲が persona だけに縮む。さらに履歴の切り詰めが毎ターン異なる位置で行われるため、履歴部分の前置きもずれる。

これが正しければ、**内容を削らずに順番を変えるだけ**でprefillが大幅に減る。正しくなければ別の原因を探す必要がある。どちらであるかを確かめるのが本作業。

## 変更

**計測のみ。送信するプロンプトの内容・順序・量は一切変更していない。**

| File | Change | Why |
|---|---|---|
| `neuro_voice/llm/prompt_metrics.py` | 新規。`PromptProfiler` / `PromptProfile` / `label_of` | プロンプトの内訳と、前回から変わっていない先頭部分のトークン数を測る |
| `neuro_voice/pipeline.py` | `_profile_prompt()` を `_fit_prompt` の直後に呼ぶ | 実際に送るものを測る。`llm.profile_prompt: false` で無効化 |
| `config/config.yaml` | `llm.profile_prompt: true` | 原因が判明したら false にしてよい |
| `tests/test_prompt_metrics.py` | 新規17件 | 下記テスト節 |

### 出るログ

```
プロンプト内訳: 合計=6880tok 再利用可=1789tok (26%) 最初に変わった所=system:今回の確信度 | system:あなたはポッポ=1789 / ...
LLM生成: 初トークン=2540 ms / ...
```

この2行が隣り合って出る。**`再利用可` の割合と `初トークン` の相関**が、仮説の成否をそのまま示す。

- 再利用可が 25% 前後なら → 仮説どおり。並び替えで大きく改善する見込み
- 再利用可が既に 80% 以上なら → 仮説は誤り。prefill以外（モデルのロード、GPU競合、num_ctx確保）を疑う

## 変更しなかったもの

- プロンプトの並び、内容、量。**遅延だけを切り出して見るため**、意図的に何も変えていない。
- Discord側。今回はローカルでの計測が目的で、意味論の変更ではないため片側のみ。順番の修正を行う段階では第19条どおり両方へ入れる。
- `_MODE_GUIDE`、persona、planner、realizer の文言。

## 設計判断

1. **削る前に測る。** ユーザーから「犠牲にして良いものが解らない」という回答があった。それは当然で、各ブロックが何トークン使い何を買っているかを示さずに選ばせたのが誤りだった。数字を出してから相談する。
2. **測る対象は「大きさ」ではなく「再利用できる割合」。** プロンプトが大きいこと自体は、キャッシュが効いていれば問題にならない。ボトルネックの正体は大きさか順番かで対処がまったく違う。
3. **計測を先に入れて、効果を後から証明できる形にする。** 憲法第20条の「『ログを追加した』は修正完了ではない。ログにより原因、期待状態、回帰の有無を確認して初めて観測可能性の改善となる」。今回はその前半にあたる。

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| `pytest tests/ -q`（sandbox / Python 3.10 + StrEnum shim、音声・GUI依存4件除外） | 843 passed, 3 skipped, 11 subtests | 全実行ログ確認 |
| `tests/test_prompt_metrics.py`（新規17件） | pass | 先頭挿入で再利用が壊れること／同じ内容を後ろへ置くと保たれること／履歴の切り詰め位置がずれると壊れることを固定 |
| `test_local_audio_liveness.py` + `test_discord_screen_share.py`（sounddeviceスタブ下） | 21 passed | 回帰なし |
| `config.yaml` のYAML妥当性 | OK | `llm.profile_prompt` を読み出して確認 |
| `python3 -m compileall neuro_voice` | exit 0 | — |

`test_ordering_alone_changes_how_much_is_reusable` が今回の中心。**同じブロック・同じ内容で順番だけ変えると再利用量が変わる**ことを、計測器の側で先に固定してある。

- 未実施: **実機での計測**。GUIを再起動し、数ターン会話してログを見る必要がある。

## 設定・Migration

- Config: `llm.profile_prompt: true`（新規）。DB変更なし。
- 再起動: **GUI再起動が必要**。

## Rollback

`llm.profile_prompt: false`。ログが止まるだけで、他に影響はない。

## 追記: 実機での計測結果（2026-07-27 21:20〜21:25）

仮説は当たっていた。そして原因は想定より単純だった。

```
合計=7313tok 再利用可=2387tok (33%) 最初に変わった所=system:CONVERSATION KERNEL
  system:CONVERSATION KERNEL   = 4720tok   ← 全体の約60%
  system:あなたは「ポッポ」…      = 2296tok
  system:会話方針                =   86tok
```

再利用可は 11ターンで 24〜33%（1件だけ62%、これはKERNELブロックが無いターン）。

### 原因

`realtime/context_assembler.py`:

```python
position = 1 if assembled and assembled[0].get("role") == "system" else 0
assembled.insert(position, {"role": "system", "content": context})
```

`Mind.build_context()` の戻り値は、kernelの判断・話者・関係性・会話要約・記憶・自律研究・対話制御を `"\n\n".join(parts)` で連結した**1本の文字列**で、4700〜5800トークンある。これが persona の直後へ入るため、persona 以外のすべてが毎ターン再評価される。

ブロックが散らばっているのではなく、**巨大なものが1つ、最悪の位置にある**。

### 併発している別の問題

```
合計=9766tok   （num_ctx=8192）
```

`fit_messages` は system を絶対に落とさない（人格と当ターンの決定を失わないため）。この設計自体は正しいが、systemブロックだけで9700トークンに達すると**予算6912どころかnum_ctx 8192も超え**、切り詰めようがない。超えた分はサーバ側で切られ、切られるのは先頭＝persona である可能性が高い。

計測に num_ctx 超過の警告を足した。

### 次の一手（未実施）

`build_context` の戻り値をセクション単位で分解し、

- **毎ターン変わらないもの**（対話制御の規則文、話者、関係性など）→ persona の直後
- **毎ターン変わるもの**（kernelの判断、想起した記憶、作業記憶）→ ユーザー発話の直前

へ振り分ける。**内容は1文字も削らない。** どのセクションがどちらかは `プロンプト内訳(詳細)` の実測で決める。

## 次の担当への注意

- **この作業だけでは何も速くならない。** 原因を特定するための計測であり、`プロンプト内訳` と `LLM初回` を突き合わせて初めて次の判断ができる。
- 並び替えを行う際は、`ConversationPlanner.prompt` / `SurfaceRealizer.prompt` の中で**毎ターン変わる部分と変わらない部分を分ける**必要がある。今は一つの文字列に混ざっているため、丸ごと後ろへ動かすと再利用できる量が減る。
- 履歴の切り詰めは、毎ターン違う位置で切ると前置きがずれる。塊単位で切り、必要な時だけ切り直す形にすること。
- 実機受入項目: (a) `再利用可` の割合、(b) その割合と `初トークン` の相関、(c) 継続依頼中とそうでない時の差、(d) 会話が長くなるにつれ割合がどう変わるか。
