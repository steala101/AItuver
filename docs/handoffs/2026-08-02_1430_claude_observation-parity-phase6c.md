# Phase 6C: 入力元ごとの接続差を埋める（Discord入退室・画面種別・KTANE）

- 日時: 2026-08-02 14:30 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 14:30 の項
- 状態: 自動回帰済み。**実機未検証**。機能フラグ既定 false
- 前段: `docs/handoffs/2026-08-02_1207_claude_world-state-phase6b.md`

---

## 0. この回で直したこと

Phase 6B で「実イベントへ繋いだ」と言ったが、**繋いだのは3経路だけ**だった。

残っていたのは入力元ごとの差で、この手の差は放っておくと
**片方で動く機能がもう片方では黙って死ぬ**。実際、VCで複数人と話していても
「誰が居るか」は空のままだったし、爆弾解除中でも世界状態は何も知らなかった。

新しい認知機能も、新しい画像認識も足していない。

---

## 1. 繋ぐ前の監査（何が実際に出ているか）

| 出どころ | 実際に出ているもの | 使ったか |
|---|---|---|
| Discord `on_voice_state_update` → `_refresh_voice_members` | Bot除外済みのメンバー一覧 | **○** |
| 映像 `VisualObservation.scene_type` | `minecraft_gameplay\|menu\|loading\|error\|unknown`（プロンプトが要求する語） | **○** |
| 映像 `people` / `objects` | 自由記述 | ×（確信が安定しない） |
| KTANE `GameProfileSessionManager` の開始・終了 | セッションID・時刻 | **○** |
| KTANE `KtaneExpert.current_module` | `parse_module` が発話から読んだ名前 | **○** |
| KTANE `Edgework.strikes` | `_STRIKES` 正規表現が発話から読んだ回数 | **○** |
| KTANE 画面認識 | **存在しない** | — |

**KTANE に画面認識は無い。** 既存実装は最初から最後まで会話駆動で、
爆弾の情報もモジュール名もミス回数も、ユーザーの発話から読み取っている。
今回もそこだけを使った。**画面から爆弾の状態を読む経路は作っていない**
——当たらない認識を根拠に「切って」と言うと、爆発する。

---

## 2. 今回見つけて直した実バグ（いちばん重要）

**退出が世界状態へ反映されなかった。**

`WorldStateGate` は矛盾する事実を「僅差では覆さない」。映像やLLMが
「たぶんメニュー」と言っただけで今の画面を塗り替えられては困るからで、
これは正しい。だが同じ規則が Discord の退出にも適用されていた:

```
present_in_conversation=True (確信 .9)
→ present_in_conversation=False (確信 .9)
→ below_contradiction_margin で棄却
→ VCから出た人がずっと居ることになる
```

矛盾の余裕は**推測**のためにある。Discord の接続状態は推測ではない
——**持ち主が「もう居ない」と言えば居ない**（第5条 状態所有権）。

`observe_fact(authoritative=True)` を足し、正本と分かっている観測だけ
余裕を通り抜けるようにした。推測側の挙動は一切変えていない
（`test_a_guess_still_cannot_flip_a_confident_fact` で保証）。

印を付けたのは3つだけ:

* Discord の参加状態・チャンネル
* KTANE のセッション状態
* 映像の画面種別 **ただし確信が下限を超えた時だけ**

最後の条件が要る。画面は移り変わるものなので、はっきり見えているなら
それが今の画面。だが**自信が無い認識まで正本にすると、曖昧な1フレームで
今の画面が塗り替わる**。低確信は `unknown` へ落としたうえで正本にしない。

---

## 3. Discord の入退室

### 接続点は1つだけ

`_refresh_voice_members` に相乗りした。ここは入室・退室・キック・
チャンネル削除・再接続のすべてが通る場所で、**Bot の除外も既に入っている**。

そのうえで、入退室イベントを1件ずつ追うのではなく
**現在のメンバー一覧で毎回合わせ直す**（`reconcile_discord`）。理由:

* 切断中に起きた出入りはイベントが来ない
* 再接続直後に前回の参加状態を信じてはいけない
* 起動直後の復元も同じ仕組みで済む（別処理が要らない）

### 人物の同一性

| 何 | 何を言っているか | 確信 |
|---|---|---|
| Discord user ID | **接続上の人物**。誰が繋いでいるか | .8 |
| 声紋 (`speaker:N`) | **声の主**。誰が喋っているか | .9 |

**別々の Entity として持つ。** 一致していても自動では統合しない。
Discord ID が確定していても「誰の声か」は言っていないし、逆も同じ。
取り違えは後から分離できないので、確信が高い方に寄せることもしない。

表示名は変わるので、Entity は `external_id`（`discord:7`）で引く。
**表示名だけで人物を分けない。**

### 退出しても消さない

Entity は残し、`present_in_conversation=False` にする。
消すと再入室のたびに別人になり、関係が積み上がらない。

### 出入りから直接喋らない

```
presence change → Attention Event → （ここで止まる）
```

`PARTICIPANT_CHANGE` を注意層へ入れるだけ。発話機会は作らない
（`test_a_presence_change_alone_produces_no_speech_opportunity`）。
話すかどうかは Action Selector と Speech Gate が決める。

> **要確認（仕様との衝突）**
>
> 既存の `discord.greet_on_join: true` は、入室のたびに
> 「◯◯さん、こんにちは。」と固定文で喋る（`_greet_voice_member`）。
> Phase 6C 仕様の「毎回join/leaveへ挨拶する実装は禁止」と衝突する。
>
> **ユーザー設定なので勝手に既定を変えていない。** 止めるなら
> `discord.greet_on_join: false`。挨拶自体を残したまま条件付きにするなら、
> `_greet_voice_member` を Speech Gate 経由へ移す改修が別途必要。
> どちらにするか指示があれば対応する。

---

## 4. 映像の画面種別

### 追加した種類

`vision/service.py` のプロンプトが要求する語だけを使う:

| 正規化後 | 実出力の例 |
|---|---|
| `gameplay` | `minecraft_gameplay`、`in_game`、`playing` |
| `menu` | `menu`、`title`、`pause_menu`、`inventory` |
| `loading` | `loading`、`loading_screen` |
| `error` | `error`、`result` |
| `unknown` | 空、`none`、**それ以外すべて** |

**架空の分類を足していない。** 足しても映像側が一度も返さないので、
テストだけが緑になって実機では死んだままになる。
`world.scene_types` プローブが**プロンプトとの食い違いを赤くする**。

`normalise_scene("shop_screen")` は `unknown`。**近そうな分類へ寄せない**
——メニューを開いただけで「まだ戦ってる」と言うのが、いちばん困る壊れ方。

### Fact は1つに収束する

`subject=active_screen` / `predicate=scene`。同じ画面が続く間は
`dedup_key` が同じなので `CONFIRM`（鮮度が戻るだけ）。
50フレーム流しても Fact は1件のまま。

### 遷移

変化した瞬間だけ `menu->gameplay` の形で `ObservationResult.detail` に出る。
**種類の数ではなく、変化を捉えられることに意味がある。**
Trace の `vision_scene_transition` に入る。

低確信の観測は `unknown` へ落としたうえで正本にしないので、
**はっきり見えている画面を曖昧な1フレームが塗り替えることはない。**

---

## 5. KTANE

### 取り込んだ実イベント（4種）

| イベント | 出どころ | 作る Fact |
|---|---|---|
| `bomb_started` | 「爆弾解除を始めよう」（`_START`） | `current_game=ktane`、`bomb_active=true` |
| `module_detected` | `KtaneExpert.current_module` の変化 | `current_module` |
| `strike_recorded` | `Edgework.strikes` の変化 | `strike_count` |
| `bomb_ended` | セッション終了、または結果の明言 | `bomb_active=false`、`session_result` |

すべて `KTANE_BOMB` Entity（`TASK_TARGET`）にぶら下がる。
**すべて `SESSION_SCOPED_PREDICATES`**——再起動後に現在の事実として戻らない。

ミス回数を短命にしていないのは、**解法そのものが変わる**から
（サイモンは `min(strikes, 2)` で表が切り替わる）。

### 結果は言われるまで `unknown`

画面は見ていないので、爆弾がどうなったかはユーザーが言うまで分からない。
**「終わった」は「解除できた」ではない**——時間切れかもしれないし、
飽きただけかもしれない。`_DEFUSED` / `_EXPLODED` が明示的に当たった時だけ
結果を付け、それ以外は `unknown` のままにする。

### 目標

```
bomb_started (確信 .95) → GAME_OBJECTIVE「爆弾を解除する」 ACTIVE
bomb_ended               → COMPLETED + outcome
```

- **低確信の開始では立てない**（`confidence < .8` は無視）。
  仕様の「低confidenceの画面認識だけでGoalをACTIVEにしない」に対応。
  そもそも `bomb_started` はユーザーが明示した時しか出ない。
- 爆弾1つに目標1つ（`goal_id` が `ktane_defuse:<session>`）。
- 失敗用の状態は**足していない**。`GoalRecord.outcome` と `succeeded` で表す。
  `FAILED` を足すと、状態を見ている全箇所が
  「`COMPLETED` だけ見ればよい」から「`COMPLETED` か `FAILED` か」へ変わる。
  **終わったか（状態）とうまくいったか（結果）は別の軸。**

### WARN と通常経路

**触っていない。** `FAST_PATH_ALLOWLIST[GAME_WARNING_FAST_PATH]` は
`{WARN}` のまま。`ANSWER` / `COMMENT` は
`fast_path_action_not_allowlisted` で止まる（テストとプローブで確認）。

権限も広げていない:

| 行動 | 分類 | 判定 |
|---|---|---|
| 世界状態の更新 | `INTERNAL` | 自動 |
| 攻略助言 | `ADVICE_ONLY` | 自動 |
| 音声での警告・回答 | `SPEECH` | 自動 |
| ゲーム操作 | `DESTRUCTIVE` | **確認＋対象確認**（今回は使わない） |

### 人格切り替えで配線が切れないように

`GameProfileSessionManager` は人格を切り替えると作り直される。
`Mind.attach_game_observer` は登録相手を覚えておいて付け直す。
**忘れると、人格を変えた瞬間に配線だけ静かに切れる。**

---

## 6. 観測可能性

### Trace（本文は入れない・第12条）

`discord_participant_id` / `participant_presence_delta` /
`vision_scene_type` / `vision_scene_transition` /
`ktane_event_type` / `ktane_entity_ids` / `ktane_fact_ids` / `ktane_goal_id` /
`speech_source_type` / `permission_result` / `speech_gate_result` /
`suppression_reason`

表示名は残さない（`test_the_trace_keeps_no_names`）。

### 診断プローブ（管理者メニュー🩺）

| キー | 何を見るか |
|---|---|
| `world.discord_presence` | 入退室が往復し、Botを数えず、再入室で同じ人に戻るか |
| `world.presence_speech` | 出入りのたびに喋り出さないか |
| `world.scene_types` | **映像プロンプトと分類が一致しているか** |
| `world.scene_transition` | 変化を捉え、同じ画面で増やさず、曖昧で覆さないか |
| `world.ktane` | 爆弾の状態が入り、結果を決めつけないか |
| `world.ktane_goal` | 目標が開いて閉じ、失敗を区別できるか |
| `world.ktane_fast_path` | 警告以外が高速経路から出ないか |

**要注意 0 件**を維持。

---

## 7. 故障注入（赤くなることの確認）

| 切ったもの | 赤くなったもの |
|---|---|
| 正本の印を無視する | `world.discord_presence`（退出が反映されない） |
| 架空の `scene_type` を足す | `world.scene_types` |
| Bot を人間として登録 | `world.discord_presence` |
| 高速経路に `ANSWER` を足す | `world.ktane_fast_path` / `cognition.speech_gate` |
| KTANE の出来事を配らない | `world.ktane`（`disconnected`） |
| 参加状態から直接発話 | `world.presence_speech` |

---

## 7.5. ついでに直した不安定なテスト

全体回帰が1回だけ赤くなった:
`test_the_status_never_contains_conversation_text`（第12条の privacy テスト）。

調べたら**漏れではなかった**。合言葉が `1234` で、`event_id` は
`uuid4().hex[:12]` の16進乱数。12桁の16進にはたまたま `1234` が入る
（実測 20000回中4回 = 0.02%）。

放置すると危ない。**本物の漏れと区別が付かない赤は、やがて誰も見なくなる。**
合言葉を `1234-5678` にした——`-` は16進に絶対現れない。20000回で0件。

## 8. テスト

- `tests/test_observation_parity.py` — 103件
- 全体 **2159 passed / 17 subtests**、pyflakes 新規分 clean

自動テストで**実イベント producer の直前まで**通している。
`GameProfileSessionManager.handle_final_input` は本物を呼んでおり、
「爆弾解除を始めよう」から `bomb_started` が出るところまでは実コード。
Discord と映像は実機のイベントが要るので、そこは下記の手順で。

---

## 9. 実機確認手順（**この順で、1つずつ**）

上げた直後に管理者メニュー🩺を開く。

### ① `world_state.discord_presence_enabled`

前提: `world_state.enabled` / `entity_tracking` / `fact_tracking` /
`observation_wiring_enabled` が true。

1. VCへポッポを入れ、自分も入る。
2. 🩺の `world.discord_presence` が緑。`world` の件数が増える。
3. **人数がポッポを除いた実人数と一致すること。**
4. 誰かにVCから抜けてもらう → 人数が減る。
   **抜けた人が「居る」のままになっていないこと**（今回直したバグ）。
5. もう一度入ってもらう → 人物が増えず、同じ人として戻ること。
6. **入退室で勝手に喋り出さないこと。**
   （喋る場合は `discord.greet_on_join` が原因。§3の要確認を参照）
7. ポッポを再起動 → VCへ入れ直す → 人数が実状態から作り直されること。

### ② `world_state.vision_scene_parity_enabled`

前提: `vision_observation_enabled` が true、映像取得が動いている。

1. Minecraftをプレイ → 🩺で `active_screen:scene = gameplay`。
2. Escでポーズメニューを開く → `menu` へ変わる。
   Trace（有効なら）に `gameplay->menu` が出る。
3. 戻る → `gameplay`。
4. しばらく同じ画面のまま放置 → **Fact の件数が増え続けないこと。**
5. 画面を暗くする等で認識を鈍らせる → `unknown` になるか、
   **前の画面のままになること**（別の画面名を名乗らないこと）。

### ③ `world_state.ktane_observation_enabled`

前提: `goals.enabled` が true。

1. 「爆弾解除を始めよう」 → 🩺の `goals` に「爆弾を解除する」が ACTIVE。
2. 「最初は配線のモジュール」 → `current_module` が入る。
3. 「ミスが1回ある」 → `strike_count=1`。
   **この状態でサイモンの指示が変わること**（既存の表引きが効いているか）。
4. 「爆発した」 → 目標が COMPLETED、`outcome=exploded`。
   **「解除できた」と言っていないのに成功扱いになっていないこと。**
5. 「爆弾解除を終わろう」 → セッション終了。
6. ポッポを再起動 → **前の爆弾の状態を現在として語らないこと。**
7. **KTANE中に、危険警告以外が高速経路から出ていないこと**
   （通常の助言はいつもどおり間があってから出る）。

どこかで想定と違ったら、そのフラグだけ false に戻す。

---

## 10. 残っている穴

- **実機未検証**（上の手順は未実行）。
- **`discord.greet_on_join` との衝突**（§3）。指示待ち。
- **映像は `scene_type` のみ**。`people` / `objects` は確信が安定しないので
  未着手。実機で当たり方を見てから。
- **KTANE の解除・爆発は発話依存**。ユーザーが言わなければ `unknown` のまま。
  画面認識を足す予定は無い（爆弾では誤認が事故になる）。
- **Discord の声紋一致による統合は未実装**。Discord ID と `speaker:N` は
  別々の Entity のまま。統合するなら、取り違えを戻せる仕組みが先に要る。
- **Local 側に参加者の概念が薄い**。Local は `speaker:N` だけで、
  Discord のような「いま何人居る」が無い。第19条の残り。
