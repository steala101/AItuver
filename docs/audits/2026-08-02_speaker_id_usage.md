# speaker_id 参照箇所の限定監査（Phase 6E 前）

- 日時: 2026-08-02
- 目的: **主キー・外部キーとして使っている箇所だけ**を洗い出す
- 方針: 必須移行対象は **Relationship のみ**。他は境界に Resolver を挟むだけ

---

## 分類

| 分類 | 意味 |
|---|---|
| `MIGRATE_NOW` | 今回 person_id へ移す |
| `RESOLVE_AT_BOUNDARY` | 保存形式は変えず、読む時に Resolver を通す |
| `KEEP_LEGACY` | speaker_id のままが正しい |
| `UNRELATED` | 同じ語だが別物（TTS の話者スロット等） |
| `UNSAFE_DIRECT_REFERENCE` | 直接DBを読んでいて、増やしてはいけない |

---

## 結果

| 箇所 | 使い方 | 分類 | 判断 |
|---|---|---|---|
| `mind/relationship.py` `RelationshipStore` | `_states[speaker_id]` が主キー | **MIGRATE_NOW** | ここだけを移す |
| `mind/mind.py` `_relationship_speaker_key()` | 関係値の鍵を作る**唯一の場所** | **MIGRATE_NOW** | Resolver の差し込み口 |
| `mind/mind.py` `speaker_status()` 他 UI 向け | `f"speaker:{id}"` で直接 snapshot | `UNSAFE_DIRECT_REFERENCE` | Resolver 経由へ寄せる |
| `mind/speakers.py` `SpeakerRegistry` | 声紋プロファイルのID | `KEEP_LEGACY` | **Voice Identity の管理層として残す** |
| `mind/internal.py` `apply_state_delta` | `delta.participant_id` を鍵に更新 | **MIGRATE_NOW** | Write Resolver 経由へ |
| `dialogue/state.py` / `kernel.py` | `_dialogue_user_key()` = `speaker:N` | `RESOLVE_AT_BOUNDARY` | 会話状態の鍵は据え置き |
| `dialogue/events.py` | イベントの発話者 | `RESOLVE_AT_BOUNDARY` | 参照のみ |
| `cognition/episodic.py` (Phase 3) | `participant_ids` | `RESOLVE_AT_BOUNDARY` | **過去の記憶は書き換えない** |
| `cognition/reflection.py` | 記憶の集計 | `RESOLVE_AT_BOUNDARY` | 同上 |
| `cognition/world.py` / `observation.py` | `speaker:N` を `external_id` に | `RESOLVE_AT_BOUNDARY` | 世界状態は接続/声紋の層 |
| `cognition/goals.py` | `owner_ids` | `RESOLVE_AT_BOUNDARY` | 文字列の持ち主IDのまま |
| `cognition/trace.py` | 記録するだけ | `RESOLVE_AT_BOUNDARY` | 両方を残す |
| `autonomy/state.py` / `types.py` | 発話者の目印 | `RESOLVE_AT_BOUNDARY` | 参照のみ |
| `discord_bridge/bot.py` | 話者識別の受け口 | `RESOLVE_AT_BOUNDARY` | Resolver へ渡す |
| `tts/style_bert_vits2.py` / `voicevox.py` / `factory.py` | **TTSモデルの話者スロット番号** | `UNRELATED` | 人物とは無関係 |
| `audio/loopback.py` | 音声経路の話者分離 | `UNRELATED` | 同上 |
| `ui/webview_app.py` | 画面表示・手動操作 | `RESOLVE_AT_BOUNDARY` | 表示は speaker のままでよい |
| `remote/*.py` | 遠隔操作API | `UNRELATED` | 起動制御のみ |

---

## 結論

**移すのは `RelationshipStore` の鍵だけ。**

`_relationship_speaker_key()` が唯一の鍵生成点になっているので、
そこへ Resolver を挟めば、呼び出し側（20箇所以上）を書き換えずに
段階的な切り替えができる。

**全DBの一括移行はしない。** 記憶・会話状態・世界状態・目標は
speaker_id を持ったまま、必要な時に Resolver で人物を引く。
