# 既存ツール経路の限定監査（Phase 7 前）

- 日時: 2026-08-02
- 範囲: **ツールを呼ぶ経路だけ**。全置換はしない

---

## 分類

| 分類 | 意味 |
|---|---|
| `REUSE` | そのまま使う |
| `EXTEND` | Tool Gate の下へ入れる |
| `UNSAFE_DIRECT_CALL` | **権限確認を迂回して呼んでいる** |
| `DEAD_OR_DISCONNECTED` | 呼ぶ側が無い |
| `NO_RESULT_VALIDATION` | 結果を確かめずに使っている |
| `UNKNOWN_SIDE_EFFECT` | 副作用が定義されていない |

---

## 結果

| 経路 | 場所 | 現状 | 分類 |
|---|---|---|---|
| **Tool Scheduler** | — | **存在しない** | `DEAD_OR_DISCONNECTED` |
| **Tool Registry** | — | **存在しない**。ツール名は文字列比較 | `DEAD_OR_DISCONNECTED` |
| **LLM の tool/function calling** | — | **使っていない**。JSONを読んで分岐するだけ | — |
| Directive のツール要求 | `pipeline._directive_run_tool` / `bot._directive_run_tool` | `web_search` / `game_state_read_only` / `vision_read_only` の3種。`dialogue_allows_tool` を通る | `EXTEND` |
| 会話中の Web 検索 | `pipeline._maybe_search` (2350) | `should_search` → `dialogue_allows_tool` を通る | `EXTEND` |
| Discord の Web 検索 | `bot.py` 4219 | 同上 | `EXTEND` |
| 自律調査 | `research/service.py` | 独立キュー。**検索結果を行動可能なプロンプトへ入れない**と明記 | `REUSE` |
| 記憶の読み書き | `mind/episodes.py` | ツールではなく内部処理。権限は不要 | `REUSE` |
| ゲーム（KTANE） | `games/ktane/` | **助言のみ。操作しない** | `REUSE` |
| ゲーム状態の読み取り | `video_service.state_context` | 読むだけ | `REUSE` |
| 話者名の変更 | `mind.rename_speaker` / UI | **UIからの明示操作のみ**。LLM からは呼べない | `REUSE` |
| 外部接続 | `llm/*` `discord_bridge/*` `music_recognition` | 基盤の通信。ツールではない | `REUSE` |
| ファイル操作 | — | **LLM から呼べる経路は無い** | — |

---

## 判定

**LLM から直接ツールを呼んで権限確認を迂回している経路は、見つからなかった。**

理由は構造的で、そもそも **tool calling / function calling を使っていない**。
LLM が返すのは文章か JSON で、それを読んだコードが分岐して呼ぶ。
副作用のある操作（送信・削除・購入・ゲーム操作）は**一つも接続されていない**。

つまり Phase 7 は「危ない経路を塞ぐ」作業ではなく、
**まだ何も繋がっていない所に、最初から門を付けて繋ぐ**作業になる。

---

## Phase 7 で埋めるべき穴

| 穴 | 現状 |
|---|---|
| ツールの能力と副作用の定義 | **無い**。`tool == "web_search"` の文字列比較だけ |
| 副作用の分類 | **無い**。読み取り専用かどうかも宣言されていない |
| Tool Gate | **無い**。各呼び出し側が自前で条件を書いている |
| 実行結果の検証 | **無い**。`str(result)[:600]` をそのまま渡す |
| タイムアウト / 取り消し | 検索のみ部分的（`timeout_s`） |
| 冪等性 / ロールバック | **無い** |

`Phase 6` で作った `decide_permission()`（7分類）は**判定できるが、
実行経路へ繋がっていない**——`NextAction` に判定が付くだけで、
その先でツールを呼ぶ仕組みが無い。ここを繋ぐのが Phase 7。

---

## 注意

`dialogue_allows_tool` は**検索専用の判断**で、汎用の権限層ではない。
Tool Gate を作る時に置き換えるのではなく、**検索の追加条件として
残す**のが安全（ゲーム中は検索しない、等の既存の意味論が入っている）。
