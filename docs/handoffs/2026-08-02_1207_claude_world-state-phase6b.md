# Phase 6B: 世界状態を実イベントへ繋ぐ・永続化・Obligation統合・権限制御

- 日時: 2026-08-02 12:07 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 12:07 の項
- 状態: 自動回帰済み。**実機未検証**。機能フラグ既定 false
- 前段: `docs/handoffs/2026-08-02_1310_claude_world-state-phase6.md`

---

## 0. この回で直したこと（一行で）

Phase 6 は**部品を全部作って、呼ぶ側を一つも作っていなかった**。

`GroundedWorldState` も `WorldStateGate` も `GoalAdmissionGate` も動く。
テストも通る。でも `observe_entity` を呼ぶ本番コードが**存在しなかった**ので、
実際に起動すると世界状態は永久に空だった。今回はそこだけを埋めた。

**新しい認知機能は足していない。** 足すと、また「部品はあるが繋がっていない」
層が一段増えるだけになる。

---

## 1. producerの監査（何が既に出ているか）

繋ぐ前に、**既に実際に出ているイベント**を数えた。架空のイベントに
繋いで「経路がある」と言うのを避けるため。

| 出どころ | 実際に出ているもの | 頻度 | 取り込んだか |
|---|---|---|---|
| 話者識別 | `Mind.identify_speaker` → `{id, name, auto_name, is_new}` | 発話ごと | **○（実経路A）** |
| 映像解析 | `VisionObservation.scene_type` / `confidence` | 約1秒ごと | **○（実経路B、1種類だけ）** |
| 映像解析 | 同 `people` / `objects` | 約1秒ごと | ×（確信が安定しない） |
| ゲーム | `GameCompanionEvent.kind` / `priority` / `signature` | 数百msごと | **○（実経路C）** |
| KTANE | モジュール状態 | 不定 | ×（次回以降） |
| Discord | 参加者の出入り | 不定 | ×（次回以降、parityの借り） |

**`kind` は既存の `GameCompanionDirector` が付けている。**
新しい分類器は作っていない。架空のイベント型も足していない。

---

## 2. 実経路（どこから呼んでいるか）

### A. 話者 → 参加者・いま話している人

`neuro_voice/pipeline.py` の `_respond` にある話者識別のコールバックから、
新設した `note_speaker_observation(profile, event_id=...)` を呼ぶ。

コールバックの中に直接書かなかったのは、**実際に通るかを試せるようにする**ため。
クロージャの中では外から呼べず、「文字列が存在する」以上の確認ができない。

```python
confirmed = not bool(profile.get("auto_name", True))
```

`auto_name` は「声紋では確定できず、仮の名前を付けた」印。
**確認できていない声を既知の人物として扱うと、別人の関係値が動く。**
取り違えは後から分離できないので、確認できた時だけ `PARTICIPANT` にする。
確認できない声は `UNKNOWN` エンティティを1つ作るだけで、**Factは作らない**。

### B. 映像 → いまの場面

`_on_game_observation` の冒頭。`scene_type` の**1種類だけ**を取り込む。

`people` / `objects` を全部入れると、世界状態がすぐ画面のゴミで埋まる。
確信が安定して当たるものから始める。実機で当たり方を見てから増やす。

### C. ゲーム → いまのプレイ状況

同じ `_on_game_observation` の、実況イベントが確定した後。
`kind` をそのまま `game:situation` にする。`signature` を元イベントIDにする。

---

## 3. 高頻度への備え

映像は毎秒、ゲームは数百msごとに来る。**そのままDBへ行くと音声応答が止まる。**

`ObservationIntake` が4つの理由で落とす:

| 理由 | 条件 |
|---|---|
| `below_confidence` | 確信 < 0.55 |
| `duplicate_event_id` | 同じイベントIDを2回適用しようとした |
| `unchanged` | 同じ内容を3秒以内に再度受けた |
| `rate_limited` | 1秒あたり8件を超えた |

落とした理由は数えて Trace と診断へ出す。**「0件」が「入力が無かった」なのか
「呼んだが全部落ちた」なのかを区別できないと、また同じ間違いをする。**

同期でDBへは書かない。書き込みは `flush_world_state()` がターンの区切りで
まとめてやる。

---

## 4. 永続化と復元（ここがいちばん嘘をつきやすい）

既存の `mind.db` に4表を足した。**新しいDBは作っていない。**

| 表 | 何を持つ |
|---|---|
| `world_entities` | 人・物・場所 |
| `world_facts` | いまこうなっている（`session_scoped` 付き） |
| `goals` | 共有する目標 |
| `goal_obligations` | 目標と約束の対応 |

### 復元で必ず落とすもの

**再起動しただけで、ゲーム途中の状態を「いまこう」と言うのは嘘になる。**

- `session_scoped` な事実（`speaking` / `scene` / `situation` / `hp` /
  `position` / `visible` / `present_in_conversation`）は、
  起動時に `mark_session_facts_stale()` で**必ず古い印**を付ける。
  消しはしない（「あった」ことは残す）が、`usable()` は false を返す。
- Entity の `presence` は `VISIBLE` ではなく `INFERRED` で戻す。
  **「在ると推測」まで。見えているとは限らない。**
- ACTIVE だった目標は `PAUSED` で戻す。
  **確かめるまで動き出さない。** 完了・破棄はそのまま。

長く効く事実（`project:name` のような TTL 無し）はそのまま現在の事実として残る。

### 保存に失敗した時

会話は止めない（第17条）。ただし**未保存を成功扱いしない**:

- `flush_world()` は `{"persisted": False, "reason": "..."}` を返す
- `world_persist_failed` カウンタが増える
- pipeline が `world_state_persist_failed` を出す
- Trace の `world_state_persisted` は `False`（未実施の `None` とは別）

---

## 5. Obligation と Goal

同じ約束を2箇所で別々に持つと、**片方だけ果たされた状態**になる。
既存の `WorkingMemory` の Obligation を捨てず、双方向に写す。

| Obligation | Goal |
|---|---|
| `pending` | `ACTIVE` |
| `blocked` | `BLOCKED` |
| `fulfilled` / `resolved` | `COMPLETED` |
| `cancelled` | `ABANDONED` |

逆方向も定義してある（`GOAL_TO_OBLIGATION`）。
**同じイベントIDでの二度目の同期は無視する**——無限更新ループの種になる。

Goal も Obligation も**直接発話しない**。Phase 6 のままで、
どちらも行動候補への小さな補正までしか届かない。

---

## 6. 権限（固定の `AUTOMATIC` を廃止）

Phase 6 では `NextAction.permission` が常に `AUTOMATIC` だった。
判定しているように見えて、**何も判定していなかった**。

7種類に分けて、種類ごとに決める:

| 種類 | 判定 | 例 |
|---|---|---|
| `INTERNAL` | 自動 | 内部状態の更新、記憶検索 |
| `SPEECH` | 自動 | 発話の提案、聞き返し |
| `ADVICE_ONLY` | 自動 | ゲームの助言（**操作はしない**） |
| `EXTERNAL_READ` | 自動 | 画面を読む、検索 |
| `EXTERNAL_WRITE` | **確認** | 設定の保存、ファイル変更 |
| `DESTRUCTIVE` | **確認＋対象確認** | 削除、ゲーム操作 |
| `PERSON_DIRECTED` | **確認＋対象確認** | 送信、投稿 |

- **知らない行動は `REQUIRE_CONFIRMATION`。** 既定を自動側にすると、
  新しい行動を足すたびに黙って自動実行されるようになる。
- **対象が分からない削除は通さない**（`reason="target_unknown"`）。
  「何を消すのか」を確かめずに消さない。
- 判定は**コードが決める**。Planner の文章には依存しない
  （`decide_permission` にプロンプトが入っていないことをテストで確認）。

`permissions.enforcement_enabled` は**実際に止める**フラグなので、
段階的公開の最後に上げる。

---

## 7. 観測可能性

### Trace（`CognitiveTrace`、本文は入れない・第12条）

`observation_source` / `source_event_id` / `observation_adapter` /
`observe_entity_called` / `observe_fact_called` / `observation_dropped` /
`world_state_delta_id` / `world_state_persisted`（**`None`=未実施、`False`=失敗**）/
`world_state_persistence_error` / `goal_id` / `obligation_id` /
`goal_obligation_sync` / `next_action_id` / `permission_category` /
`permission_result` / `permission_reason` / `vertical_slice_stage`

話者名は記録しない（テストで確認済み）。

### レイテンシ

`observation_adapter_ms` / `observe_entity_ms` / `observe_fact_ms` /
`entity_resolution_ms` / `world_state_delta_ms` / `goal_matching_ms` /
`next_action_generation_ms` / `permission_decision_ms` /
`goal_obligation_sync_ms` / `world_state_persist_ms` /
`world_state_hydrate_ms` / `vertical_slice_total_ms`

合計を出しているのは、**安全側の判定が遅いと迂回したくなる**から。
テストで通し 50ms 未満を保証している（実測は 1ms 未満）。

### 診断プローブ（管理者メニュー🩺）

| キー | 何を見るか |
|---|---|
| `world.producers` | **呼ぶ側があるか**（3経路＋書き出し） |
| `world.observation` | 観測が `observe_entity` / `observe_fact` まで届くか |
| `world.intake` | 高頻度を落とせるか |
| `world.persistence` | 再起動で一時状態を現在の事実にしないか |
| `goals.obligation_bridge` | 約束と目標が双方向に写るか |
| `goals.permission_decision` | 種類ごとに判定しているか |

**要注意 0 件を維持。** 既定オフのものは `off` であって `broken` ではない。

---

## 8. 故障注入で赤くなることの確認

「テストが通る」は「テストが意味を持つ」ではない。切って赤くなることを確認した。

| 切ったもの | 赤くなったもの |
|---|---|
| `observe_game_event` の呼び出しを消す | `test_the_three_producers_call_the_adapter` / `world.producers` |
| 復元時に `PAUSED` へ落とさない | `test_an_active_goal_returns_as_paused` / 垂直スライス |
| `EXTERNAL_WRITE` を自動許可へ戻す | `test_each_category_gets_its_policy` / `goals.permission_decision` |
| `SESSION_SCOPED_PREDICATES` を空にする | `test_a_transient_fact_is_not_current_after_a_restart` / `world.persistence` |
| `mark_session_facts_stale` が 0 を返す | `world.persistence` |

---

## 9. テスト

- `tests/test_world_wiring.py` — 65件。実経路A/B/Cを**本物の
  `VoicePipeline` メソッドで実行**（`_on_game_observation` を含む）。
  話者・不明話者・映像・ゲーム・重複・高頻度・永続化・一時状態の復元・
  Obligation同期・権限分類・打ち切り・DB失敗時のフォールバック。
- `tests/test_world_wiring_trace.py` — 27件。Trace項目、レイテンシ、
  診断プローブ、故障注入。
- 全体: **2056 passed / 17 subtests**。pyflakes は新規分すべて clean。

---

## 10. 実機確認手順（**この順で、1つずつ**）

一度に全部上げない。上げた直後に管理者メニュー🩺を開く。

1. `world_state.enabled` / `entity_tracking_enabled` / `fact_tracking_enabled` /
   `observation_wiring_enabled` / `speaker_observation_enabled` を true。
   → 名前が確定している人と話す。🩺の `world.observation` が緑、
   `world` の件数が増える。**仮名の話者では参加者が増えないこと**を確認。
2. `vision_observation_enabled` を true。Minecraftを映す。
   → `session:scene` が入る。`world.intake` の `dropped` が増えている
   （毎秒来ているのに件数が増え続けないこと）。
3. `game_observation_enabled` を true。
   → `game:situation` が入る。応答の待ち時間が伸びていないこと。
4. `world_state.persistence_enabled` を true。少し会話して終了 → 再起動。
   → ログに `世界状態を復元` が出る。**その直後にゲームの状況を聞いて、
   「いまは分からない」と答えること**（前回の状況を現在として語らない）。
5. `goals.enabled` / `goals.persistence_enabled` / `obligation_bridge_enabled`。
   → 「あとで◯◯して」と頼む → 再起動 → 目標が `PAUSED` で戻り、
   **勝手に再開せず**、確認してから動くこと。
6. `permissions.enforcement_enabled` は**最後**。上げる前に、
   🩺の `goals.permission_decision` で自動になる行動一覧を確認する。

どこかで想定と違ったら、そのフラグだけ false に戻す。全部戻せば元の挙動。

---

## 11. 残っている借り

- **Discord parity**: 参加者の出入りを世界状態へ入れていない。
  Local だけ「誰が居るか」を持っている状態で、第19条の借り。
- **映像は1種類だけ**: `people` / `objects` は確信が安定しないので未着手。
- **KTANE**: モジュール状態を世界状態へ入れていない。
- **実機未検証**: 上の手順は実行していない。
