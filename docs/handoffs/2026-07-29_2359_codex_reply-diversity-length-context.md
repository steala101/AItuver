# 引継ぎ: 反復回避A/B、長さ固定解除、対話context圧縮

- Agent: Codex
- Date: 2026-07-29 23:59 JST
- Status: 実装・自動A/B・全体回帰完了。実GUI受入は未実施

## 目的

前handoffの順序どおり、次を実施した。

1. 直近の実返答を具体的なturn-local反復回避材料にする最小A/B
2. UserModelの`response_length=long`が全turnを長文化する問題
3. `Mind.build_context`内で大きかった対話制御contextの重複削減

新しい会話エンジンやLLM呼び出しは追加していない。

## 実装

### 1. 直近返答の実験機能

`SurfaceRealizer.recent_reply_context()`は、Plannerがすでにpersona別に保持している
`recent_replies`から直近N件の冒頭・最初の一文だけを抜き出す。既定は4件、
1件48文字、全体280文字上限。別store、metricsへの本文、追加LLM呼び出しはない。

設定:

```yaml
dialogue:
  conversation_generation:
    recent_reply_avoidance:
      enabled: false
      count: 4
      max_chars: 48
      max_total_chars: 280
```

`enabled=false`は明確なcontrol armで、動的blockは一切入らない。
snapshotには本文でなく`enabled/examples/chars`だけを出す。
`Mind`はgroup privacy contextを渡し、有効化されていても複数人promptには注入しない。

### 2. 長さ制御

旧実装は`interaction_policy.response_length == long`なら、内容に関係なくほぼ全turnを
`long`にしていた。新実装は次の順で決める。

1. monologue、deep_dive、現在turnの明示的な詳細依頼 → long
2. concise、低momentum、短い相槌、現在turnの明示的な短文依頼 → short
3. 永続short好み → short
4. 永続long好みは、56文字以上または32文字以上の質問だけlongへ寄せる
5. その他 → medium

UserModelは「今回は詳しく」を永続化しない。「今後の説明はいつも長め」のような
一般嗜好が明示された時だけ保存する。既存profileに残る`long`もsoft biasとして動くため、
DB/JSON migrationは不要。

### 3. context圧縮

Pythonですでに選択済みなのにLLMへ再送していたものを削った。

- 候補scoreと棄却候補
- Persona Modulesの数値dump（Surfaceが日本語方針へ変換済み）
- Curiosity score/reason、Dialogue Act label
- 二重のAgreement説明
- Plannerのengine名、冗長な内部値

選択済みstyle、shape、話題、最大2件の任意表現、安全上必要な確度/Agreementは残した。

代表turn実測:

| 条件 | chars | 推定token |
|---|---:|---:|
| 変更前の対話制御context | 2515 | 1833 |
| 変更後・直近返答なし | 1574 | 1281 |
| 変更後・直近返答1件 | 1647 | 1374 |

`tests/test_conversation_generation.py`で対話制御contextを1500 token以下に固定した。
これはMind全体の最大値ではなく、検索、視覚、transcript想起等は別計測が必要。

## A/B結果

`tools/check_reply_avoidance_ab.py`を追加し、同じ8文、同じturn seedで
OFF 8turn / ON 8turnをOllamaへ送った。返答本文はファイルにも標準出力にも残さない。

| 指標 | OFF | ON |
|---|---:|---:|
| opening_repeat_ratio | 0.25 | 0.25 |
| ending_repeat_ratio | 0.00 | 0.00 |
| question_ratio | 0.50 | 0.25 |
| length mean | 121.9 | 107.2 |
| length stdev | 39.6 | 17.5 |

冒頭重複率は改善しなかった。質問は減ったが返答長の揺らぎも減ったため、
「自然さが改善した」とは判定していない。既定OFFとした。

過去の実アプリA/Bは冒頭重複率0.875相当、自動harness controlは0.25だが、
経路が完全同一ではないため圧縮による改善と断定してはいけない。

## 変更ファイル

- `neuro_voice/dialogue/surface_realizer.py`
- `neuro_voice/dialogue/intelligence.py`
- `neuro_voice/dialogue/conversation_planner.py`
- `neuro_voice/dialogue/user_model.py`
- `neuro_voice/dialogue/plan_metrics.py`
- `neuro_voice/mind/mind.py`
- `config/config.yaml`
- `tools/check_reply_avoidance_ab.py`
- `tests/test_conversation_generation.py`
- `tests/test_prompt_rules.py`
- `tests/test_plan_metrics.py`
- `docs/CONVERSATION_AB_SCRIPT.md`
- 共通文書一式

## 検証

- `py_compile`: success
- 対象テスト: 34 passed
- 会話・prompt・interaction統合: 173 passed
- 全体: 1074 passed / 2 skipped
- skip理由: `pyflakes`未導入
- 自動A/B: 16turn完走、本文非保存

## 次の受入

1. アプリ再起動
2. `recent_reply_avoidance=false`のまま8文台本をテキスト入力
3. `logs/conversation_metrics.jsonl`の
   `opening_repeat_ratio`、`planned_length_distribution`、初トークン時間を確認
4. 「うん」「なるほど」等の短い相槌、普通の質問、明示的な詳細依頼を各3件
5. short/medium/longが一種類に固定されず、実音声長にも反映されるか確認
6. 検索、OBS視覚、過去会話想起を各1件流し、Mind全体tokenと初トークンを計測

## Rollback

- 直近返答機能だけ: `enabled: false`（現在すでにfalse）
- 長さpolicyだけ戻す場合: `ConversationPlanner._target_length()`と
  `UserModel.observe()`の一般嗜好gateを旧条件へ戻す。ただし`long`固定が再発する
- context圧縮だけ戻す場合: Planner候補dumpとDialogueIntelligenceの削除行を戻す。
  token budget testも同時に意図を確認する

## 注意

- `.git`は空directoryで`git status`不能（R-001）。差分rollbackはGitに依存できない。
- 実験機能を「実装したから」既定ONにしない。計測差が必要。
- 返答本文をmetrics、handoff、changelogへ書き出さない。
