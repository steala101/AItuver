# 改修計画 2026-07-19 — AI Project Constitution V2 との差分分析

GPT5.6 sol改修後のコードベースを憲法V2(全17条)と照合した結果と、理想へ近づけるための優先度付き改修案。

## 1. 現状評価: 憲法との適合度

結論から言うと、現在の実装は憲法V2にかなり忠実。以下は条項別の適合状況。

| 条項 | 状態 | 備考 |
|---|---|---|
| §3 対話ルール | ◎ | barge-in / VAD / 宛先判定 / 相槌 / 声紋話者識別 / 割り込み時の破棄が実装済み。turn_manager + playback_tracker がイベント駆動で管理 |
| §4 真実性 | ◎ | persona.py の事実性・非迎合ルールがキャラ設定より優先。略語ハルシネーションのターン毎ガードあり |
| §5 アーキテクチャ | ◎ | Planner → Critic → Surface Realizer → Response Director → TTS の層構造が憲法の図とほぼ一致。LLM/STT/TTS はファクトリで交換可能 |
| §6 会話設計 | ○ | candidate_count=4 の候補比較 + style_repeat_penalty で定型パターンを回避。initiative / curiosity あり |
| §7 記憶 | ○ | 3層記憶 + 重要度prune + ペルソナ別分離。**confidence カラムが未実装** (importance のみ) |
| §8 人格・関係性 | ◎ | 単一の好感度スコアではなく trust / psychological_safety / respect 等の多軸 + 減衰。1回の変化量クランプ済み |
| §9 感情音声 | △ | 感情タグ→style制御はあるが、**TTSがVOICEVOX中心で表現力が頭打ち**。Style-Bert-VITS2 が未統合 |
| §10 マルチモーダル | ○ | rolling buffer (30s) / 変化検知 / latest-frame-wins / サーキットブレーカあり |
| §11 性能 | ○ | context_deadline 150ms / 想起60ms予算 / 内省の会話時中断など予算管理が明示的 |
| §12 信頼性 | ○ | enable/disable / timeout / fallback が設定面で統一されている |
| §13 可観測性 | ○ | developer_ui + latency計測。ステージ別レイテンシの常時ダッシュボードはない |
| データ保護 | ✕→◎ | **バックアップ機構が存在しなかった → 今回実装** |

## 2. 今回実装したもの (2026-07-19)

### 2.1 人格データのバックアップ (`neuro_voice/mind/backup.py`)
- 全ペルソナの人格・記憶・関係性・話者・時間認識データ (`personality_* / mind_* / dialogue_* / adaptive_* / relationships_* / temporal_* / speakers_*`) を zip で世代保存
- **定期自動**: 既定1時間毎 (`mind.backup.interval_minutes`)。内容が前回と同じ回はスキップ
- **終了時**: アプリ終了時に自動取得 (`mind.backup.on_exit`)
- **手動**: 設定 → ペルソナタブ → 「今すぐバックアップ」
- **復元**: 同タブの一覧から。復元前に現状を `pre_restore` として自動退避。反映は再起動後
- SQLite は online backup API でスナップショットするため、会話中でも壊れない
- 世代管理: 自動系14世代 / 手動20世代 (別枠)。エラーは会話へ波及させない
- テスト: `tests/test_persona_backup.py` (6件)

### 2.2 Neuro-sama風ペルソナ「ネル」 (config.yaml `persona.presets.neru`)
リサーチに基づく性格の核: 明るく好奇心旺盛 / 毒舌・からかい好きだが悪意はない /
自信過剰な冗談 (「私は天才AI」) / 気分屋 / 軽い実存ネタ / 愛情表現「ハート」 /
冗談のホラ話はOKだが事実と混同しない (憲法§4準拠)。
- 記憶・人格・話者データは `*_neru.*` として他ペルソナと完全分離 (既存機構)
- 初期人格シード `data/personality_neru.json`: 好奇心0.9 / いたずら心0.9 / 自信0.85
- **高成長設定**: `persona.presets.neru.growth_rate: 2.0` (今回、ペルソナ別成長率の仕組みを追加)

### 2.3 ペルソナ別成長率
`mind.growth_rate` を `persona.presets.<key>.growth_rate` で上書き可能に (`Mind._growth_for`)。
ネルだけ2倍速で性格・好みが変化し、他ペルソナは従来通り。

## 3. 理想 (Neuro-sama級) へ近づける改修案 — 優先度順

「普通の会話でユーザーが体感できるか」(§2) で並べている。

### P1: 感情音声の表現力 (§9) — 最大のギャップ → ✅実装済み (2026-07-19)
`neuro_voice/tts/style_bert_vits2.py` + `style_bert_vits2_launcher.py` を追加。
設定→機能→読み上げ音声で VOICEVOX と切替可能 (反映は再起動、選択時にサーバーは即時ウォームアップ)。
起動時に `server_fastapi.py` を裏で自動起動し、終了時に自動終了。
感情タグ→スタイル写像 (joy→るんるん / sad→よふかし / soft→ささやき等、config.yamlで変更可)、
話速=感情連動、感情強度=style_weight連動、音量=波形ゲイン。VOICEVOXはフォールバックとして残存。
テスト6件 (`tests/test_style_bert_vits2.py`)。実機音声は次回起動時に要確認。

### P2: 記憶への confidence 追加 (§7)
`memories` テーブルに `confidence REAL` を追加し、内省プロンプトで
「確かな事実 / 推測」を区別して保存。想起時に低confidenceは「〜だったっけ?」と
不確かさ付きで言及。体感: 記憶違いの断定が減り、人間らしい「うろ覚え」表現になる。

### P3: 自発発話 (proactive) の段階有効化 (§3)
実装済みだが `proactive.enabled: false`。まず `min_relevance: 0.8` の保守設定で有効化し、
未解決話題の蒸し返しだけ許可 → 問題なければ緩める。
体感: 「そういえばさっきの話だけど」と自分から話し出す。ネルの性格と相性が良い。

### P4: 観測ダッシュボードの充実 (§13)
ステージ別レイテンシ (STT→Planner→LLM→TTS→再生開始) を developer_ui に常設表示し、
「時間がかかった時になぜかが分かる」状態にする。改善の計測基盤になる。

### P5: 軽量判断のLLM外し (§11)
宛先判定 `use_llm_for_ambiguous` は将来もルールベース優先を維持。
相槌選択・感情タグ検証など決定的コードで足りる箇所をLLM呼び出しから外し、初回音声までの時間を守る。

### P6: バックアップ先の外部化
`mind.backup.dir` を別ドライブ (例: `D:/aituber_backups`) やクラウド同期フォルダへ
向けるとPC故障にも耐える。設定1行で可能 (実装済み機構の運用改善)。

## 4. 変更ファイル一覧 (今回)

- 新規: `neuro_voice/mind/backup.py`, `data/personality_neru.json`, `tests/test_persona_backup.py`, `docs/kaizen_plan_20260719.md`
- 変更: `neuro_voice/mind/mind.py` (バックアップ統合・ペルソナ別成長率), `neuro_voice/ui/webview_app.py` (バックアップAPI 3件), `neuro_voice/ui/assets/index.html` (ペルソナタブにバックアップUI), `config/config.yaml` (mind.backup / neruペルソナ)

## 5. 既知の制限

- 復元の反映にはアプリ再起動が必要 (実行中エンジンのメモリ状態を安全に差し替えるAPIは未実装)
- バックアップは同一PC内が既定。外部保存は §P6 の設定変更で対応
- ネルの「毒舌の匙加減」は実会話での調整が必要。強すぎ/弱すぎは `character` 欄と
  `growth_rate` で調整可能
- 検証はロジックテストのみ。音声実機での全体動作は未確認 (次回起動時に要確認)
