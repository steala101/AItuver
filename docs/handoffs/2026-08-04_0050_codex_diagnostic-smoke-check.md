# 🩺 スモークテスト確認値の表示 — 引き継ぎ

- 日時: 2026-08-04 00:50 JST
- 担当: Codex
- 状態: 完了（Python pytest は実行環境不備で未実施）

## 変更

配線診断の一般フラグ一覧では、`cognition.trace.enabled` が「トレース記録」という表示名に埋もれ、`active_persona_id` はペイロードに無かった。実機スモークテストで確認する値を独立表示した。

- `neuro_voice/ui/webview_app.py`
  - `get_diagnostics()` が設定の正本から `active_persona_id` を読み、`payload["persona"]` として返す。
- `neuro_voice/ui/assets/index.html`
  - 🩺 の「スモークテスト確認」に以下を `技術キー = 現在値` で表示する。
    - `memory.retrieval_enabled`
    - `turn_integrity.persona_scope_enabled`
    - `turn_integrity.turn_frame_enabled`
    - `cognition.trace.enabled`
    - `active_persona_id`

この変更は表示専用。設定保存、ペルソナ切替、Memory、Trace出力、DBには触れない。

## 検証

- Node の `--check` でスクリプト構文を確認。
- Node で5値の表示ヘルパーを実行し、各`key = value`（`active_persona_id = b`を含む）を確認。
- `pytest` は `.venv\\Scripts\\python.exe` が存在しない Python 3.11 本体を参照しており起動不可。環境復旧後に実行する。

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_migration_ui.py -q -p no:cacheprovider
```

## 継続事項

- Phase 7D の実機スモークテストはチビ担当。結果が来るまで、分離・移行・設定へ触れない。
- Phase 8 には進まない。
