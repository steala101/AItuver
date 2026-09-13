# 🩺 スモークテスト確認値の説明 — 引き継ぎ

- 日時: 2026-08-04 01:20 JST
- 担当: Codex
- 状態: 完了（Python pytest は実行環境不備で未実施）

## 変更

`neuro_voice/ui/assets/index.html` の「スモークテスト確認」は、値だけのチップではなく既存の診断行と同じ表示へ変更した。各行に項目名、技術キー、役割、現在値を出す。

- `memory.retrieval_enabled`: 現在のペルソナの過去記憶を必要時に検索する。
- `turn_integrity.persona_scope_enabled`: 他ペルソナの私的記憶を検索時点で除外する。
- `turn_integrity.turn_frame_enabled`: 1ターンを追い、重複の発生層を記録する。
- `cognition.trace.enabled`: 診断値をJSONLへ記録し、会話本文は残さない。
- `active_persona_id`: 設定上の選択中ペルソナであり、切替後に目的IDかを確認する。

検索（技術キーと`key = value`）は維持している。設定、ペルソナ切替、Memory、Trace、DBには変更なし。

## 検証

- Node の `--check` による構文確認済み。
- Node で Trace と active persona の説明・現在値を含む表示を直接確認済み。
- `pytest` は `.venv\\Scripts\\python.exe` が存在しない Python 3.11 本体を参照しており起動不可。環境復旧後に実行する。

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_migration_ui.py -q -p no:cacheprovider
```

## 継続事項

- Phase 7D の実機スモークテストはチビ担当。結果が来るまで、分離・移行・設定へ触れない。
- Phase 8 には進まない。
