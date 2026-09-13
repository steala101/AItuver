# 🩺 配線診断のお気に入り — 引き継ぎ

- 日時: 2026-08-04 01:24 JST
- 担当: Codex
- 状態: 完了（Python pytest は実行環境不備で未実施）

## 変更

`neuro_voice/ui/assets/index.html` の配線診断へお気に入りを追加した。

- 各配線診断行とスモークテスト確認行に ☆ / ★ ボタンを表示する。
- ☆ を押すと技術キーをお気に入りへ登録し、★ を押すと解除する。
- 「★ お気に入りのみ」で登録項目だけを表示する。検索語・要注意フィルタと組み合わせられる。
- 未登録でお気に入り表示を選んだ場合は、☆で登録する案内を出す。

保存先はブラウザの `localStorage`（キー: `neuro_voice.diagnostic_favorites.v1`）。保存値は診断キーの配列だけで、会話本文、Memory本文、Trace本文、設定値は保存しない。設定、ペルソナ切替、Memory、Trace、DBには変更なし。

## 検証

- Node の `--check` による構文確認済み。
- Node で診断キーがローカル保存されることと、「お気に入りのみ」で該当キーだけが残ることを確認済み。
- `pytest` は `.venv\\Scripts\\python.exe` が存在しない Python 3.11 本体を参照しており起動不可。環境復旧後に実行する。

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_migration_ui.py -q -p no:cacheprovider
```

## 継続事項

- Phase 7D の実機スモークテストはチビ担当。結果が来るまで、分離・移行・設定へ触れない。
- Phase 8 には進まない。
