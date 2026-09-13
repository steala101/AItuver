# Phase 6: いまどうなっているか・共有する目標

- 日時: 2026-08-02 13:10 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 13:10 の項
- 状態: 自動回帰済み。**実機未検証**。機能フラグ既定 false

---

## 1. Preflight（7条件、すべて問題なし）

機会が Action Selector へ入る / 沈黙と比較される / Speech Gate を通る /
TTS直前再検証が動く / 期限切れが機能する / 予算と重複抑制が動く /
Closure 後に同一話題を再開しない。修正不要。

## 2. 既存機能の分類

| 分類 | 対象 |
|---|---|
| **REUSE** | `WorkingMemory`（open_questions）、`GameProfileSessionManager`、`KtaneTables`、`RelationshipStore`、Phase 3 の `EpisodicMemory`、Phase 5 の `AttentionEvent` / `InitiativeOpportunity` |
| **EXTEND** | `CognitiveKernel`（`goal_bias`）、`InitiativeRuntime`、`CognitiveTrace`、診断 |
| **NEW** | `cognition/world.py`、`cognition/goals.py` |
| **DUPLICATE / DEAD** | 無し |

## 3. 記憶と状況を分けた理由

| | エピソード記憶（Phase 3） | 世界状態（Phase 6） |
|---|---|---|
| 何を持つ | 過去に何が起きたか | いまどうなっているか |
| 時間 | **増えていく** | **古くなって消える** |
| 確信 | 支持が増えて上がる | 放っておくと落ちる |
| 保存 | SQLite | RAM |

同じレコードへ押し込むと、どちらかが必ずおかしくなる。
診断プローブ `goals.separation` が「鮮度は現在状況だけ、重要度は記憶だけ」を
固定している。

## 4. 事実の鮮度

**述語ごとに寿命を持つ**（`FACT_TTL`）。

```
hp 8秒 / position 10秒 / visible 15秒 / speaking 6秒
holding 60秒 / nearby 45秒
searching_for 10分 / activity 30分 / playing 1時間
```

一律にすると、HPが1時間残るか「いまMinecraftをやっている」が8秒で消えるか
のどちらかになる。

`sweep()` は**消さずに `STALE` の印を付ける**。`usable_facts()` が
根拠として使えるものだけを返す。

## 5. 「見えない」と「無い」を分ける

```python
class Presence(StrEnum):
    VISIBLE           # 見えている
    INFERRED          # 見えていないが在ると考えるのが自然
    RECENTLY_SEEN     # 直前まで在った
    CONFIRMED_ABSENT  # 無いことを確かめた
    UNKNOWN
```

`lost_sight_of()` は既定で `RECENTLY_SEEN`。`confirmed_absent=True` を
明示した時だけ `CONFIRMED_ABSENT`。**画面から外れただけで不存在を断定すると、
すぐに嘘をつく。**

時間が経つと `VISIBLE → RECENTLY_SEEN → INFERRED` と落ちる。

## 6. 対象を勝手に統合しない

`MERGE_CONFIDENCE = .75` 未満では既知の対象へ寄せない。
種類が食い違う場合（人 vs 場所）も統合しない。

**「たぶんあの人だ」で統合すると別人の情報が混ざり、後から分離できない。**
分けておく方が、混ざってから直すよりずっと安い。

矛盾も同じ考えで、`CONTRADICTION_MARGIN = .15` を超える確信差が無ければ
覆さない。代わりに古い方を `UNCERTAIN` にする。

## 7. 勝手な目標を作れない

登録できる出どころは**5つだけ**:

```
user_stated / user_approved / explicit_collaboration
assistant_promise / game_profile
```

拒否する出どころ:

```
small_talk / inferred_life_goal / psychological_inference
single_visual_change / self_generated_desire
```

**持ち主のいない目標も拒否する。** `owner_ids` が空だと、事実上ポッポ自身の
目標になる（`GAME_OBJECTIVE` だけ例外）。

出どころが分からないものは `PROPOSED` のまま置き、**承認されるまで動かさない**。

## 8. 目標は命令ではない

`goal_bias` は **±0.15 で頭打ち**。加えて:

- **打ち切りの合図（end_signal > .5）が出ていれば何も返さない。**
  目標が ACTIVE でも、会話を続ける理由にはならない
- ただし**目標は破棄しない**（状態を保持する）
- `COMPLETED` の目標は `RESUME_OBLIGATION` を**減点**する
  （完了後に同じ助言を続けない）
- 同じ助言は `ADVICE_COOLDOWN`（60秒）以内に繰り返さない

`next_actions()` は**最大3手**。長い計画を毎ターン作らない。

## 9. 完了と中断

- **完了は証拠と確信 .8 以上が要る**（`mark_completed`）。
  `completion_conditions` があればそれも照合する。
  早まると、まだ終わっていない作業の助言が止まる
- **中断中に勝手に再開しない。** `PAUSED` の目標は `next_actions` に出ない。
  `resume_candidates()` が話題か対象の一致で拾った時だけ候補になる

## 10. 権限

```
要許可（10件）: send_message / send_email / post / purchase / delete_file /
               game_input / persist_setting / contact_other_person /
               external_request / modify_file
自動（6件）:   update_internal_state / search_memory / organise_situation /
               propose_utterance / game_advice / recall_obligation
```

**知らない行動は許可が要る側へ倒す。** `requires_approval("")` も true。
取り返しがつくかどうかで分けてある。

## 11. テストとレイテンシ

```
tests/test_world_state.py   76件
全体                         1964 passed / 17 subtests
pyflakes                     clean
```

| 項目 | 実測 |
|---|---|
| `world_state_snapshot_ms` | 0.057 |
| `world_state_delta_ms` | 0.006 |
| `entity_resolution_ms` | 0.010 |
| `fact_validation_ms` | 0.030 |
| `goal_matching_ms` | 0.009 |
| `next_action_generation_ms` | 0.016 |
| **100ターン相当** | **2.8ms** |

**通常イベントごとの追加LLM呼び出しは無し。**

診断: `{ok: 28, off: 14, blocked: 2}`、**要注意 0 件**を維持。
「状況」層に6プローブ（鮮度 / 同定 / 目標の登録 / Closure / 権限 / 分離）。

## 12. 実機で確認すべきこと

まだ**作る側の配線が無い**ので、いまフラグを上げても状態は空のまま。
先に §13 の穴を埋める必要がある。

埋めた後の順序:

1. `world_state.enabled` + `entity_tracking_enabled`。
   🩺 の counters で `entities_seen` が増えるのを見る
2. `fact_tracking_enabled`。トレースの `world.summary` で
   `facts_usable` / `facts_stale` の比を見る。
   **stale ばかりなら TTL が短すぎる**
3. `goals.enabled`。ユーザーが目標を言った時に `goals_admitted` が増え、
   LLM が勝手に提案した時に `goals_rejected` が増えることを確認
4. `goals.next_action_enabled`

## 13. 残っている穴

- **実機未検証**
- **世界状態を作る側の配線が無い。** `observe_entity` / `observe_fact` を
  呼ぶ実経路（映像・ゲーム・話者検出）が未接続。いまは部品だけ
- **世界状態も目標も RAM のみ。** 再起動で消える。
  §16 の「再起動後も長期GoalとObligationを維持」は**未達**
- **既存 Obligation と `GoalRecord` を繋いでいない。**
  `obligation_integration_enabled` は枠だけで、`WorkingMemory.open_questions`
  との対応付けが無い
- 垂直スライス（§15）は部品としては通るが、**実経路では動かない**
- `next_actions` の `permission` は常に `AUTOMATIC`（外部行動を提案する
  経路がまだ無いため）。権限の仕組みは作ったが、使われていない

## 14. rollback

`config/config.yaml` の `world_state.*` と `goals.*` を false（既定）。
コードごと戻す場合は新規2ファイルと `runtime.py` の Phase 6 部分、
`kernel.py` の `goal_bias` を消す。**実経路へ繋いでいないので、
消しても他は動く。**
