# 配線診断の検索 — 引き継ぎ

- 日時: 2026-08-04 00:44 JST
- 担当: Codex
- 状態: 完了（Python pytest は実行環境不備で未実施）

## 変更

`neuro_voice/ui/assets/index.html` の既存 🩺 配線診断パネルへ、画面内検索を追加した。

- 項目名、技術キー、説明、影響、関連フラグ、層を横断検索する。
- 各行に技術キーを小さく表示するため、検索語に何を入れるか分かる。
- 「要注意のみ」で `broken` / `error` / `disconnected` だけに絞れる。
- 一致件数を `一致 / 全件` で出す。0件時も明示する。
- 診断の3秒自動更新後も検索語とフィルタ状態を保持する。

バックエンド診断、設定、会話・音声経路、DBには変更なし。`tests/test_migration_ui.py` にUI回帰テストを1件追加し、`docs/CODEMAP.md` を更新した。

## 検証

- Node の `--check` で `index.html` 内スクリプトの構文を確認済み。
- Node で検索ヘルパーを直接実行し、技術キー検索と要注意フィルタを確認済み。
- `pytest` は `.venv\\Scripts\\python.exe` が存在しない Python 3.11 本体を参照しており起動不可。実行環境復旧後に以下を再実行する。

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_migration_ui.py -q -p no:cacheprovider
```

## 継続事項

- Phase 7D の実機スモークテストはチビ担当。結果が来るまで、分離・移行・設定へ触れない。
- Phase 8 には進まない。
