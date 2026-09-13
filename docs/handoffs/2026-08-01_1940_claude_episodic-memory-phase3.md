# 認知カーネル Phase 3: エピソード記憶・想起・Reflection

- 日時: 2026-08-01 19:40 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-01 19:40 の項
- 状態: **自動回帰済み・実機未検証**。機能フラグ3つとも既定 false

---

## 1. 通したループ

```
出来事 → 候補抽出 → 保存判断 → エピソード記憶
      → 必要時に想起 → 行動候補の点数へ反映
      → 複数経験から Reflection → 小さな行動補正
```

**「記憶をプロンプトへ渡して完了」にしないこと**が今回のいちばんの狙い。
渡すだけだと、過去に同じ質問をして怒られていても次のターンでまた質問できる。
点数として効かせて、どの記憶がどのスコアを動かしたかをトレースに残す。

## 2. 既存機能の分類

| 分類 | 対象 |
|---|---|
| **REUSE** | `MemoryStore`（SQLite・embedding・importance・access_count）、`LocalEmbedder`、`PrivacyManager`、`RelationshipStore`、`TemporalSelf`、`Mind._reflect` のアイドル待ち |
| **EXTEND** | `memories` テーブル（列10個追加）、`MemoryStore.prune`、`CognitiveKernel._score`、`CognitiveTrace` |
| **NEW** | `cognition/episodic.py`、`cognition/recall.py`、`cognition/reflection.py`、`mind/episodes.py` |

**新しいベクトルDBへ移行していない。** 既存の `mind_<persona>.db` をそのまま使う。

## 3. スキーマ

`memories` へ **`ALTER TABLE ADD COLUMN` だけ**で10列。既存の
fact / preference / episode はそのまま読める（`test_case8_an_old_database_still_opens`）。

```
information_type  TEXT   observation / user_statement / inference / reflection / system_fact
event_type        TEXT   conversation / preference / correction / promise /
                         strong_affect / self_failure:<pattern>
confidence        REAL
status            TEXT   active / low_priority / archived / superseded / contradicted / deleted
superseded_by     INTEGER
speaker_key       TEXT
support_count     INTEGER
contradiction_count INTEGER
occurred_at       REAL
meta              TEXT   JSON: session_id / participant_ids / topic_ids /
                         source_event_ids / 各salience
```

`meta` を JSON にしてあるのは、**問い合わせと順位付けに使う値だけを実列にする**ため。
全部を列にすると、1つ増やすたびにマイグレーションが要る。

新設は `reflections` テーブルのみ。

## 4. 保存条件

保存する（5種）:

- 明示された好み（`_PREFERENCE`）→ `USER_STATEMENT` / confidence .95
- 訂正（`_CORRECTION`）→ `USER_STATEMENT` / confidence .95。**いちばん強い**
- 約束・未完了（`_PROMISE`）→ future_utility .95
- 強い感情（`_STRONG_AFFECT`）→ emotional_salience .8
- 自分の失敗（`self_failure_candidate`）→ パターン名で持つ

保存しない:

- 挨拶・相づち・2文字以下（`is_trivial`）
- 確信 0.55 未満のASR
- 既存とほぼ同一（`duplicate_score >= .78` → REINFORCE / UPDATE）
- 機微情報（`restricted` / `secret` / **`high`**）。`high` には住所・本名・病歴・
  年収・借金が入る。会話の役には立つが**長期に持ち続ける必要が無い**
- 価値 0.45 未満（`MemoryCandidate.value` は importance だけで決めない）

判断は `MemoryWriteGate.evaluate` **1箇所だけ**。規則から来た候補も、
将来LLMから来る候補も同じゲートを通す。入口を2つにすると片方だけ
重複チェックが漏れる。

## 5. 訂正と重複

**過去の記憶を物理削除しない。** 古い方に `status` を付け、`superseded_by` で
新しい方へ繋ぐ。

矛盾の検出は文字列類似度では**できない**。「短い方がいい」と「詳しい方がいい」は
ほとんど1文字も重ならない。だから軸を明示的に持つ:

```python
AXES = {
    "response_length":   (短い/簡潔/手短…, 詳しい/詳細/丁寧…),
    "conversation_style": (一問一答/単調…, 話を広げ/自分から…),
    "humor":             (冗談/ボケ/ジョーク…, —),
    "proactivity":       (勝手に/余計な…, 提案/先回り…),
}
```

向きは、軸の語の**後ろ16文字**に好意/否定の言い回しがあるかで決める。
「短いのが好き」と「短ければいいわけじゃない」を分けるにはこれが要る。

**ここに無い話題は矛盾判定に掛からない。** 分からないものを矛盾と決めつけない
方が安全側。増やす時は行を足す。

重複判定は Jaccard と包含の大きい方。語尾が5文字増えるだけで Jaccard が
.64 まで落ちるため（「一問一答は嫌い」vs「一問一答は嫌いなんだよね」）。
包含は**短い断片が長文へ吸い込まれない**よう、6バイグラム以上の時だけ使う。

## 6. 想起

引き金（`retrieval_trigger`）が無いターンは**DBを開かない**。挨拶で 0.03ms。

```
action_retrieve_memory / past_reference / unresolved_obligation /
similar_to_past_failure / relationship_event / named_entity
```

順位付けの重みは `recall.RANKING_WEIGHTS` の1箇所だけ。半減期14日。
上限3件。過去の失敗は**パターン名の一致**で引く——「もういいよ」と
「終了の合図の後に説明を続けた」は1文字も重ならない。

Planner へ渡すのは `{memory_id, type, summary, confidence, relevance}` の
4件まで。**生の会話履歴は渡さない。**

## 7. Action Selector への接続

`CognitiveKernel(memory_influence=...)` → `_score` で `memory_adjustment` として
加算し、`reasons` に `memory:<id>` を残す。

| 記憶 | 影響 |
|---|---|
| `self_failure:kept_talking_after_end_signal` | `CONTINUE_PREVIOUS_TOPIC` −.12 |
| `self_failure:asked_a_question_already_answered` | `ASK_CLARIFICATION` −.15 |
| `self_failure:joked_at_a_bad_moment` | `MAKE_LIGHT_JOKE` −.18 |
| `promise` | `CONTINUE_PREVIOUS_TOPIC` +.12 |
| `preference`（質問について） | `ASK_CLARIFICATION` −.10 |

**1行動あたり ±0.30 で頭打ち**（`MAX_ADJUSTMENT`）。上書きではなく補正なので、
記憶がいくら積み上がってもゲームの危険警告は押しのけられない
（`test_memory_cannot_override_a_safety_warning`）。

## 8. Reflection

`CONVERSATION_STRATEGY` **1種類のみ**。ユーザーの人格分析全般へは広げていない
——仮説の当たり外れを本人が確かめられない領域は、間違ったまま固定される
リスクが高い。

- `MIN_SUPPORT = 3`。**1件では作らない**
- 文は「ユーザーは怒りっぽい」ではなく
  「終了の合図の後に説明を続けると、会話がそこで途切れる場面が複数回観測された」
- 確信は `min(.75, .25 + .10 * support)` で頭打ち。積み上げだけで 1.0 になると
  明示的な否定で覆せなくなる
- 反証（`revise`）で −.35。`.25` を下回ると `RETIRED`。**レコードは消さない**
  ——「一度そう考えて外した」ことも記録として意味がある
- 実行は `Mind._reflect` のアイドル待ちの**後**、ロックの外。
  **リアルタイム応答をブロックしない。次のターン以降にしか効かない**

## 9. テスト

```
tests/test_episodic_memory.py   46件
全体                             1589 passed / 17 subtests
pyflakes                         clean
```

必須シナリオ10件はすべて対応するテストがある（ケース番号でコメント）。
特に落としたくないもの:

- `test_case1_*` — 挨拶・相づちを保存しない（候補側とゲート側の**両方**）
- `test_case5_the_selected_action_actually_changes` — 記憶が実際に選択を変える
- `test_case4_the_old_memory_is_not_deleted` — 訂正で消さない
- `test_memory_cannot_override_a_safety_warning` — 記憶が警告を押しのけない
- `test_the_trace_keeps_no_conversation_text` — トレースに本文が漏れない

## 10. レイテンシ（200エピソード + 5失敗のDBで実測）

| 項目 | 実測 |
|---|---|
| `memory_trigger_check_ms`（挨拶・DBを開かない） | 0.03 |
| `memory_retrieval_ms` | 2.4 |
| `memory_ranking_ms` | 2.8 |
| 想起 合計 | 8.8 |
| `memory_prompt_build_ms` | 0.02 |
| `memory_write_ms`（応答完了後） | 11 |
| `reflection_schedule_ms`（会話停止中のみ） | 6.0 |

## 11. 実機で確認すべきこと（この順で）

1. `memory.episodic_enabled: true` だけ上げる。**行動は何も変わらないはず**。
   `docs` ではなくDBを直接見て、雑談で行が増えていないことを確かめる
   （`sqlite3 data/mind_poppo.db "SELECT event_type, count(*) FROM memories GROUP BY 1"`）
2. `retrieval_enabled: true`。`cognition.trace.enabled: true` にして
   `logs/cognitive_trace.jsonl` の `memory.trigger` を読む。
   **挨拶で trigger が立っていたら閾値が緩すぎる**
3. `reflection_enabled: true`。会話を止めて3分ほど待ち、
   `memory_reflection` イベントが1件だけ出ることを確認
4. 「短い方がいい」→ 後日「詳しい方がいい」と言って、
   古い方が `superseded` になり新しい方が使われることを確認

## 12. 残っている穴

- **実機未検証。** `cognition.enabled` 自体も実機で有効化したことがない
- 想起は文字ベース。既存の `LocalEmbedder` による意味検索へ差し替えられる
  （`rank()` の `semantic_relevance` を差し替えるだけ）が、今回はやっていない
- `AXES` は4軸のみ。ここに無い話題の訂正は検出されない
- **Discord 側の配線は未着手**（Local のみ）。第19条の parity は次回
- 内省LLM（`Mind._apply_reflection`）が出す記憶候補は、まだ旧経路で
  `_store_memory` へ直行しており**ゲートを通っていない**。
  重複と訂正の保証が片側だけ効いている状態
- 自分の失敗の検出は2パターンのみ（`_self_failure_pattern`）。
  `repeated_the_same_explanation` と `joked_at_a_bad_moment` は
  補正表にあるが、**それを検出する側がまだ無い**

## 13. rollback

`config/config.yaml` の `memory.*` を3つとも false にすれば、既定の状態に戻る
（そもそも既定がその状態）。DBの列は残るが、既存の読み手は触らないので無害。
コードごと戻す場合は新規4ファイルと `store.py` の追加分を消し、
`prune` を元の DELETE のみへ戻す——ただし**戻すと保護対象が物理削除される**
ことに注意。
