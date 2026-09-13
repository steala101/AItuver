# AI作業Handoff

- 担当AI: Claude
- Role: implementation
- Task: `docs/PROMPT_RULE_AUDIT.md` の残り候補（A・D）を実施し、規則をコードへ移す
- Started At: 2026-07-29 22:00 JST
- Finished At: 2026-07-29 22:40 JST
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: 既存作業ツリー（Git操作なし）
- Work Lock: Codexは上限のため停止中。単独編集

## 目的

同日の対照実験で、`ConversationPlanner` の738tokを丸ごと外しても
冒頭ハッシュが7/8で一致することが分かった。返答を決めているのは
**常時ONのプロンプト**である。そこを削る。

ユーザーから「personaは変更されても問題ない（まだ理想のpersonaに辿り着けていない）」
との明示的な許可を得た。

## 調査結果

- 根本原因: 規則が過去の不具合1件につき1文ずつ積み上がっており、
  同じことを複数箇所で言っていた。個々は正当だが誰も読み返していない
- 責任の所在（憲法第2条）で分類した。`response_shape=` が発話に混ざったか、
  返答がユーザー発話の繰り返しか——これらは**文字列を見れば分かる事実**であり、
  モデルへ頼む筋のものではない
- privacy/data: ログへ残すのは検出した印だけ。周辺の文は会話なので残さない（第12条）

## 変更

| File | Change | Why |
|---|---|---|
| `neuro_voice/dialogue/leakage.py`（新規） | `has_internal_leak` / `leak_markers` / `strip_internal_leak` | 内部値の漏れを発話直前に検査する。**意図的に狭い**——ASCIIの項目名、角括弧の内部タグ、自前のセクション見出しだけ。誤検出は実発話を黙らせるので漏れより悪い |
| `neuro_voice/pipeline.py` | `_without_internal_leak` を `_speak_loop` へ。全producerを1箇所で覆う | 合成の直前が唯一の絞り所 |
| `neuro_voice/discord_bridge/bot.py` | `_speak` へ同じ検査（第19条） | Localだけ直して完了としない |
| `neuro_voice/memory/persona.py` | `_OUTPUT_LANGUAGE_INSTRUCTION` から列挙を削除（155→約85tok相当） | 数え上げても漏れていた。一言で足りる |
| `neuro_voice/dialogue/echo.py` | `is_echo` / `ReplyEchoGuard` が複数の照合元を受け付ける | 「何も新しいことを言っていない」は2通り——自分の繰り返しと、相手の繰り返し。同じ計測で照合先が違うだけ |
| `neuro_voice/pipeline.py` | `_reply_echo_guard` が直前のユーザー発話も渡す | persona の「オウム返しはしない」の移設先 |
| `neuro_voice/memory/persona.py` | 「オウム返しはしない」を削除 | 上記でコードが担う |
| `neuro_voice/dialogue/surface_realizer.py` | 「毎回『なるほど』…から始めない」を常時ONから削除 | `ConversationCritic` が実際に検出した時だけ `feedback_prompt` が言う。**起きてもいない違反を毎ターン先回りで禁じるのをやめた** |
| `neuro_voice/dialogue/conversation_planner.py` | 人格モジュールの数値ダンプを削除 | `SurfaceRealizer._persona_expression_rule` が同じ数値を読んで日本語指示へ変換済み。二重だった |
| `neuro_voice/dialogue/conversation_planner.py` | 話題候補を上位2件・score なしへ | Plannerは既に最上位を選んでいる。残りは「選ばれなかったもの」 |
| `tests/test_leakage.py`（新規28件） | 半分は**誤検出のテスト** | 漏れは恥ずかしいが、実発話を黙らせる方が悪い（誰も気づけない） |
| `tests/test_reply_echo.py` | 複数照合元の6件を追加 | |
| `tests/test_prompt_rules.py` | 移設後の状態へ更新。上限を 3400→**3000** tok へ | 削れた分だけ上限も下げないと同じ山が積み上がる |
| `tests/test_conversation_generation.py` | 数値ダンプが消えたことを検証する形へ | |

## 変更しなかったもの

- **B 分類（判断が要る規則）**: 事実性・非迎合ルール622tok、ASR誤変換の扱い184tok、
  キャラクター設定187tok、system_prompt 335tok。**憲法第1条のINVARIANTで、
  コードでは判定できない**
- 感情タグの指示（114tok）: パーサが依存している
- `Mind.build_context` の4700〜5800tok: 今回の対象外。**プロンプト側に残る最大の余地**

## 設計判断

- **`leakage.py` は狭く作った。** 「なるほど」を落とすかどうかは*意味*であって
  事実ではない。それはモデルの側に残す（第2条）
- **文単位で落とす**。1行の漏れのために回答全体を捨てるのは、それ自体が失敗（第17条）
- **定型書き出しは削除ではなく降格**。既存のCriticフィードバック経路が
  条件付きで同じことを言うので、常時ONの一文は要らない

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| `pytest tests/test_leakage.py -q` | 28 passed | サンドボックス |
| `pytest tests/test_prompt_rules.py tests/test_conversation_generation.py -q` | 29 passed | サンドボックス |
| 全体回帰（`--ignore` 5件は既知の実機依存） | **1023 passed / 11 subtests** | サンドボックス |
| 常時ONトークン実測 | persona 1916 / planner 547 / surface 436 = **2899** | `estimate_tokens` |

- 未実施: 実機での8文再走（次のセッション）
- Performance: プロンプト評価は 0.244ms/tok なので **−278tok ≒ 68ms**。
  **テンポへの効果はこの程度しかない。期待しないこと**

## 設定・Migration

なし。config は触っていない。

## 失敗した試行

なし。

## 残課題

1. **8文を流して冒頭ハッシュが割れるか見る。** 割れなければ −743tok では
   足りないということで、次は `Mind.build_context` の5000tokへ行く
2. `target_length` がプロファイル `interaction_policy.response_length` で
   `long` に固定される件（Planner側の実問題。まだ直していない）
3. num_ctx 8192 に対し system 5000〜6000。**履歴の予算が1000tok強しかない**

## Rollback

各変更は独立している。`leakage.py` の配線を外し、persona/surface/planner の
削除箇所を戻せばよい。DBもconfigも触っていない。

## 次に触るべきファイル

- `neuro_voice/mind/mind.py` `build_context`（残る最大のブロック）
- `neuro_voice/dialogue/conversation_planner.py:382-388`（長さの固定）

## 触らない方がよい箇所

- persona の事実性・非迎合ルール（憲法第1条のINVARIANT）

## 憲法・ADRへの影響

- Constitution amendment required: なし
- ADR added/updated: R-038 の棚卸しが完了。`docs/PROMPT_RULE_AUDIT.md` へ実施結果を追記
- Architecture/Codemap updated: **`dialogue/leakage.py` の追記が必要**（未実施）

## 秘密・個人情報確認

- `.env`値を記録していない: はい
- raw audio/imageを添付していない: はい
- private transcriptを複製していない: はい（漏れのログも印だけ）

## 次の担当への一文

−743tok（20%）削ったが、**これで足りるとは思っていない**。
判定は8文の冒頭ハッシュが割れるかどうかで、割れなければ次は
`Mind.build_context` の5000tokである。
