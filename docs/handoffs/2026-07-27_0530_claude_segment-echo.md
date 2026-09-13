# AI作業Handoff

- 担当AI: Claude (Cowork / claude-opus-5)
- Role: implementation / test
- Task: ラジオトークで次の区間が前区間の締めを言い直す問題の修正
- Started At: 2026-07-27 (JST)
- Finished At: 2026-07-27 (JST)
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: 実働ルート直接 (R-001のとおりGit未管理)

## 目的

ユーザー報告「ラジオトークをさせると、たまに同じ台詞を次のチャットで言ってしまう」。

## 調査結果

画面の実例（連続する2つのチャットボックス）:

```
区間N   …ニュースを見ていると、多様性を認めるっていうのは、ただ「混ぜる」だけじゃなくて、
        お互いの安全や尊厳を守り合うことがすごく大事なんだなって改めて感じるよ。
区間N+1 お互いの尊厳を守り合うことがすごく大事なんだなって改めて感じるよ。
        だからこそ、誰かが傷つくことを目的とした行為には…
```

**2つの文は同一ではない**（「安全や」が落ちている）。文字列一致では検出できない。

原因は指示の組み合わせ:

- `_MODE_GUIDE[MONOLOGUE]` に「**前の区間の最後の考えから**自然につながる新しい角度を出し」
- 同じプロンプトの `## 直近で自分が話した区間 (繰り返さない)` に、その前区間の本文が入る

ローカル12Bは前者に素直に従い、示された締めの一文から書き始める。後者の「繰り返さない」は守られているつもりで、モデルとしては「続きを書いた」。プロンプトの指示同士が競合しており、**どちらも文面としては正しい**ため、文言の調整では安定しない。

一方、これは「直前に話した文をもう一度言っているか」という**文字を比べれば判定できる機械的な性質**である。憲法第2条の「コードが事実・整合性を検証する」に該当する。

## 変更

| File | Change | Why |
|---|---|---|
| `neuro_voice/dialogue/directive.py` | `sentences()` / `trim_echoed_opening()` を追加 | 区間冒頭が前区間の言い直しなら落とす。純粋関数 |
| `neuro_voice/dialogue/continuation.py` | 発話前に `trim_echoed_opening` を適用。全部が echo なら loop 扱いで `waited` | 聞き手に同じ台詞を二度流さない |
| `neuro_voice/dialogue/continuation.py` | `segment_echo_trimmed` メトリクスとログ | 頻度を観測できるようにする（第20条） |
| `tests/test_segment_echo.py` | 新規16件 | 下記テスト節 |

### 判定方法

対称な類似度（`SequenceMatcher.ratio`）は使えない。前区間は候補文より遥かに長いことが多く、逐語の言い直しでも比率が下がるため。実例で 0.61 程度にしかならない。

代わりに **包含率** を使う: 候補文の文字のうち、どれだけが前区間の中に順序を保って現れるか。

```
containment = Σ(matching blocks) / len(candidate)
```

実例では約 1.0。閾値 0.85、対象は冒頭から最大2文まで、8文字未満の短文は対象外（「うん。」「そうだね。」は言い回しであって繰り返しではない）。

全文が echo だった場合は空になるので、発話せず `waited` として扱い、ループ検出のカウンタを進める。

## 変更しなかったもの

- `_MODE_GUIDE[MONOLOGUE]` の文言。「前の区間の最後の考えから続ける」という指示自体は、話が飛ばないために必要。禁止文を足して抑え込む方向は D-016 が戒めているところで、しかも今回は文言では安定しない。
- `SegmentLoopDetector`。冒頭n-gramと話題の繰り返しを見る既存の検出はそのまま。今回のケースは「冒頭が前区間の**末尾**と一致する」形なので既存の検出範囲外だった。

## 設計判断

1. **プロンプトの競合はプロンプトでは直さない。** 「前の考えから続けて」と「繰り返すな」は文面上どちらも正しく、モデルは両方を満たしたつもりになる。判定できる性質はコード側で確定させる。
2. **対称な類似度ではなく包含率。** 「候補文がすでに言われているか」が知りたいことで、「2つの文が似ているか」ではない。
3. **短文は対象外。** 「うん。」「そうだね。」を落とすと逆に不自然になる。8文字の下限を置いた。

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| `pytest tests/ -q`（sandbox / Python 3.10 + StrEnum shim、音声・GUI依存4件除外） | 780 passed, 3 skipped, 11 subtests | 全実行ログ確認 |
| `tests/test_segment_echo.py`（新規16件） | pass | 修正を戻すと**2件が失敗**することを確認済み |
| 実際に観測された文のペア | pass | `test_a_reworded_echo_is_detected` に実例をそのまま使用 |
| `test_local_audio_liveness.py` + `test_discord_screen_share.py`（sounddeviceスタブ下） | 21 passed | 回帰なし |
| `python3 -m compileall neuro_voice` | exit 0 | — |

誤検出しないことを確かめる側のテストも入れた: 同じ話題語を含むだけの文、短い相槌、まったく新しい話題。

- 未実施: 実機での3分ラジオ通し確認
- Performance: `SequenceMatcher` は短い文字列に対して1区間あたり1〜2回。無視できる

## 設定・Migration

- Config: 変更なし。閾値はコード内定数（`threshold=0.85` / `max_dropped=2`）。
- DB schema: 変更なし。
- 再起動: **GUI再起動が必要**。

## Rollback

`continuation.py` の `if action.spoken_content and previous:` ブロックを削除すると従来動作。`trim_echoed_opening` は残っていても副作用がない。

## 次の担当への注意

- 閾値 0.85 を下げる場合、`test_a_shared_topic_word_is_not_an_echo` と `test_a_short_opening_is_never_trimmed` が落ちないか確認すること。話題語の共有まで echo と見なすと、話が続かなくなる。
- `_MODE_GUIDE` へ「繰り返すな」系の文言を追加しないこと。今回の原因はプロンプト同士の競合であり、文言を足すと悪化する。
- 実機で `区間の冒頭が前区間の繰り返しだったため除去` のログ頻度を見ること。多発するようなら `_MODE_GUIDE` の「前の区間の最後の考えから」という表現自体を見直す価値がある。
- 実機受入項目: (a) 3分ラジオで同じ台詞が二度流れないか、(b) 話が細切れにならないか（過剰に落としていないか）、(c) ログの `segment_echo_trimmed` の回数。
