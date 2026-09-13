# ユーザー回答を会話品質指示書へ反映

- 日時: 2026-09-07 10:04 JST
- 担当: Codex、文書更新のみ。コード担当はSol。
- 成果物: [更新した指示書](../superpowers/plans/2026-09-07-conversation-quality-sol.md)、[ADR-0005](../adr/ADR-0005-conversation-quality-recovery.md)、D-043、R-049、共通台帳、次session入口。

## 確定方針

Local優先、次にDiscord複数人。ほどほどに活発、からかい多め、製作者に当たり強め。速さ優先で内容の破綻を防ぐ。Local LLMのみ、クラウドAPIは対象外。現在の声を維持しFish Audioは検討候補。状況に合う最適な発話と人に近い話し方、容量増を許容した記憶保持を目指す。

回答済みの7質問を指示書で確定要件へ移した。製作者Identity/避ける題材、transcript全文の保持範囲/backup、Local検索要約のD-041変更、Fish Audioの具体的運用だけを残る確認事項にした。今すぐ再質問しない。

## 記憶の新しい注意点

静的に `mind/store.py::prune_transcripts` の日数/件数DELETE、`prune`の一部DELETE/一部archive、Mindからの起動/保守呼出しを確認。現在データの削除を確認したわけではない。write無効だけでpruneも無効と仮定しない。

指示書にP0保持監査と独立M（非削除policy・検索top-k/index・古い記憶の到達性・容量不足・隔離試験）を追加。全件保存と全件prompt投入を分ける。起動/maintenanceの指紋不変をtmp_pathで確認し、本番起動試験と書込み再開の前提にする。raw音声/画像の永久保存は含まない。

## 非変更と検証

アプリコード、DB、persona、config、過去Trace、実行用venvは未変更。全pytest、推論、実機、外部サービスは実行なし。主要3文書のリンク/fenceエラー0、8段階＋M、Local LLM限定、回答済み節への更新を検査済み（exit 0）。CURRENT/CODEMAPの実装説明は変更なし。

rollbackはこの追記の文書差分のみ。過去の台帳とhandoffは保持。次担当はP0〜P2を初回納品単位とし、Mのコードを検索修正と同じdiffへ混ぜない。
