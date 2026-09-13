# 会話訂正・割り込み後の同一回答ループ修正

- Date: 2026-07-30 01:47 JST
- Agent: Codex
- Status: completed

## 症状と原因

実ログでは、AIの誤回答をユーザーが途中で遮って意味を訂正した直後、AIが同じ誤回答を完全に繰り返した。

原因は三つあった。

1. 遮られたassistant本文は通常履歴へまだcommitされておらず、既存のLocal reply echo guardの比較対象外だった。
2. `InterruptedTurn`の未発話内容が訂正turnのdeferred contextへ再注入され、ユーザーが否定した古い解釈を強く再提示した。
3. 重複を全て抑止した場合、検索していないturnでも「検索結果は取れたけど」と言う固定fallbackがあった。Discordには同等のopening echo guardもなかった。

同じ時刻帯に、384 msの音声が「ご視聴ありがとうございました。」へ誤認され、無関係なtopic/historyを作る事象も確認した。

## 実装

- `ConversationRepair`を追加し、明示的な意味訂正、置換対象、正本、内部system contextを構造化した。
- 訂正が直前解釈を置換する時はdeferred topicを破棄する。
- 遮られたassistant本文は履歴や正本にせず、直後の重複検知証拠としてだけ保持する。
- Local/Discordのopening echo guardを通常履歴・割り込み本文・ユーザー発話へ拡張した。
- 抑止後のtail再流入を止め、訂正turnでは短い訂正确認を返す。検索失敗文は実際に検索contextがある時だけ使う。
- 既知のWhisper末尾幻覚は、文字列に加えて時間的不可能性または低confidenceがある時だけ棄却する。

## 検証

- focused: `41 passed`
- related: `229 passed`
- full: `1123 passed, 2 skipped`
- skipはpyflakes未導入による既存skip。

## 実機受入

1. ポッポに意図を誤解した長めの返答をさせ、再生途中で遮る。
2. 「AっていうのはBっていう意味」と訂正する。
3. 古い回答の続き、同一回答、Web検索、検索失敗文が出ないことを確認する。
4. Bを理解した短い修復またはBに沿う回答になることを確認する。
5. LocalとDiscordで各一回確認する。

## 未解決

主語や対象を大きく省略した暗黙訂正はR-040。patternを無制限に広げず、実会話fixtureを追加して段階的に扱う。
