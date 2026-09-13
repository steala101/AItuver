# 🩺 スモークテスト確認値を検索対象へ統合 — 引き継ぎ

- 日時: 2026-08-04 00:59 JST
- 担当: Codex
- 状態: 完了（Python pytest は実行環境不備で未実施）

## 原因と変更

前回追加した「スモークテスト確認」は表示専用で、検索対象は従来の配線プローブ行だけだった。そのため以下の表示値は検索に掛からなかった。

- `cognition.trace.enabled = true`
- `active_persona_id = b`

`neuro_voice/ui/assets/index.html` で確認値を通常検索と同じ検索関数へ通し、一致件数にも加えた。技術キー単体と、画面どおりの完全表記（`active_persona_id = b`）の両方で検索できる。要注意のみ表示では、正常な確認値を混ぜない。

設定、ペルソナ切替、Memory、Trace、DBには変更なし。

## 検証

- Node の `--check` による構文確認済み。
- Node で以下を直接確認済み。
  - `cognition.trace.enabled` が1件へ絞られる。
  - `active_persona_id = b` が1件へ絞られる。
  - 要注意のみでは確認値が混ざらない。
- `pytest` は `.venv\\Scripts\\python.exe` が存在しない Python 3.11 本体を参照しており起動不可。環境復旧後に実行する。

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_migration_ui.py -q -p no:cacheprovider
```

## 継続事項

- Phase 7D の実機スモークテストはチビ担当。結果が来るまで、分離・移行・設定へ触れない。
- Phase 8 には進まない。
