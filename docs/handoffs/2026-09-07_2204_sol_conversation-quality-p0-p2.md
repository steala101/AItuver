# Sol handoff — 会話品質 P0〜P2

> 2026-09-08追記: 下記「単一7秒deadline未実装」は[完了handoff](2026-09-08_0845_sol_conversation-quality-p0-p2-close.md)で解消済み。この文書のテスト数・残件は2026-09-07時点の履歴として保持する。

## 結果

P0 offline評価、P1複合検索route、P2 query-aware Evidence/Presenterを実装し、Local/Discord fake統合まで`CODE_VERIFIED`。外部検索、実機、Local LLM、Discord、本番DBは使っていない。

## 主な変更

- `dialogue/conversation_quality_eval.py`、27件のsynthetic fixture、offline CLIを追加。未測定品質はnull。
- `search/contracts.py`へfrozen route/outcome/evidence/cache契約を追加。
- `search/intent.py`で「饅頭こわいを検索して、短く教えて」等からsubjectだけを抽出。否定・過去質問・後調べ・曖昧指示を分離。
- `search/deepsearch.py`の一般query攻略語展開を停止。provider error区別、public URL/redirect/1MiB制限を追加。
- `cognition/search_result_presenter.py`をquery-awareにし、PAGE優先、hash/span整合、部分回答・不足・失敗を創作なしで返す。
- `pipeline.py` / `discord_bridge/bot.py`へ共有route/outcome/presenterとepoch-bound一時Evidenceを配線。

## TDD・検証

- RED: 新API欠落4 collection error、複合Intent/query、固定品質点、Local full utterance、PAGE選択、private URL、provider失敗区別。
- GREEN: 最終P0〜P2 targeted 163 passed / 1 expected Windows symlink skip。offline evaluator 27 cases。
- 故障注入: command判定削除、Evidence検証常時true、stale guard迂回で各testが赤化。復元後5 passed。
- 最終全suite: `3005 passed, 1 skipped in 102.10s`。skipは`tests/test_tool_dialogue.py:663`のWindows symlink権限のみ。

## 残存穴・次順

1. P2のquery+fetch全体を一つにするmonotonic 7秒deadlineは未実装。既存request timeout/bytes/stale guardだけを完了と誤認しない。
2. 期限をTDDで閉じた後、Localで「饅頭こわいを検索して、短く教えて」「結末まで検索して教えて」を各1回。結果根拠、創作なし、1配送を確認。
3. その後Discord複数人で同じ2件と、新入力によるstale検索破棄を確認。P3以降へ先行しない。
4. 記憶保持Mは独立diff。本番DB起動試験/Memory write再開前に非削除契約を実装する。

## 禁止維持

Memory DB/persona/config/過去Trace/実行用venv、DAVE mix/leaveは未変更。M/P3以降、クラウドAPI、外部judge、実機自動操作へ範囲を広げない。

## 後日確認事項

- P2の7秒deadlineを第1便の追加修正として閉じてから実機へ進む方針でよいか。
- 実機でEvidenceがPARTIALの場合、現在の抽出文を許容するか、それとも検索失敗扱いで短く不足だけ伝えるか。
