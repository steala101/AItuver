# Phase 6D: Presence・話者Identity・入退室・視覚状態

- 日時: 2026-08-02 16:45 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 16:45 の項
- 状態: 自動回帰済み。**実機未検証**。新フラグ既定 false（`greet_on_join` は元から true）
- 前段: `docs/handoffs/2026-08-02_1430_claude_observation-parity-phase6c.md`

---

## 0. この回で直したこと

1つの `speaker_id` が3つの違うことを同時に意味していた:

- どの接続から来たか（Discord の user ID）
- どの声に似ているか（声紋の推定）
- 誰として覚えているか（関係値・記憶の持ち主）

**同じ番号で扱っていると、声紋が一度外れただけで関係値が別人へ移る。**
そして移った先が正しいかどうかを後から確かめる手がかりが残らない。
取り違えは、起きてから分離できない形で起きる。

あわせて、入室の挨拶が固定文の即時TTSだったのを認知経路へ移した。

---

## 1. authoritative Presence の処理

### 正本の印を付けてよいもの

| 出どころ | 正本か | 理由 |
|---|---|---|
| Discord の入室・退室 | **○** | 接続が保証する。疑いようがない |
| KTANE のセッション開始・終了 | **○** | セッション管理そのものが持ち主 |
| 明示的な切断通知 | **○** | 同上 |
| 声がしばらく聞こえない | × | 黙っているだけかもしれない |
| 画面から人物が消えた | × | 視界の外へ出ただけかもしれない |
| 声紋の類似度が下がった | × | マイク条件や体調で揺れる |
| LLM の推測 | × | 推測 |

推測側は**従来どおり矛盾マージンに従う**
（`test_a_guess_still_obeys_the_contradiction_margin`）。
今回の変更は印を付けた所だけに効く。

### 退出を確定できるのは接続だけ

```
接続が切れた   → LEFT（確定）
声が聞こえない → STALE（居ないとは言っていない）
```

`PresenceRegistry.sweep` は `PresenceSource.TRANSPORT` のエントリを
そもそも触らない——**接続が「居る」と言っている間は、黙っていても居る。**

### 出入りのイベントIDは毎回変える

入り直しは**別の出来事**であって重複ではない。同じIDを使い回していたら
2回目の入室が「同じイベントの再送」として落ち、退出したまま戻らなかった
（テストで発見）。`join:<key>:<時刻>` にしてある。

---

## 2. `greet_on_join` の新しい動作

**削除も `false` 化もしていない。** 意味だけを変えた。

| | 旧 | 新 |
|---|---|---|
| `true` | 固定文を即時TTS | **挨拶の候補を作ってよい** |
| 実際に話すか | 必ず話す | Action Selector と Speech Gate が決める |

`cognitive_greeting_enabled` が下りている間は**従来どおり固定文**。
上げると固定文の経路はその場で `return` して通らない——**二重挨拶の禁止**。

---

## 3. 固定挨拶から認知経路への変更

```
Discord join
→ authoritative Presence 更新
→ Greeting Opportunity（候補）
→ Action Selector（沈黙と同じ列で比較）
→ GREET / ACKNOWLEDGE_PRESENCE / REMAIN_SILENT
→ Planner
→ Speech Gate
→ TTS
```

### 候補を作らない条件（builder 側）

| 条件 | なぜ |
|---|---|
| 誰か分からない | **知らない人に名前で挨拶する方が失礼** |
| 90秒以内の再接続 | 回線が切れただけ。挨拶し直すと鬱陶しい |
| 3人以上の同時入室 | 一人ずつ名指しすると場が止まる |
| 30分以内に挨拶済み | 同じ相手へ何度も言わない |

### 候補は作るが黙る条件（`suppression_reason` 側）

ユーザーが話している / 他の参加者が話している / 自分が話している /
割り込み処理中 / **危険警告中** / 集中モード / 既に言った / 予算切れ。

**判定を2箇所に分けていない。** builder は「その人について」を見て、
`suppression_reason` は「場について」を見る。場の判定を builder へ
持ち込むと、通常の自発発話と挨拶で条件が食い違う。

### 挨拶の性質

- `SpeechPolicy.OPTIONAL`——**話してもよい、であって話すべきではない**。
  `REQUIRED` にすると選ばれた時点で発話が確定し、直前の再検証
  （相手が話し始めた等）で取り消せなくなる。
- `TargetScope.DIRECT`——その人へ。場全体への独り言ではない。
- 人が多いほど `social_cost` が上がる（名指しの一言は高くつく）。
- 25秒で期限切れ。**入ってから何十秒も経って挨拶するのは変。**
- さっきまで話していた人には `ACKNOWLEDGE_PRESENCE`（挨拶より軽い）。

### 退出

Presence は**必ず**更新する。別れの一言は条件付き:

- 直前までその人と話していた（`note_talked_with` が根拠）
- 既知の人物である
- 突然の切断ではない——**もう聞いていない相手へ喋っても届かない**
- 同じ退出を処理済みでない
- 15秒で期限切れ（切断のずっと後に宛先不在の音声を出さない）

---

## 4. Transport / Voice / Person の構造

```
TransportIdentity   provider + transport_user_id + authoritative
                    「誰が繋いでいるか」は確実。**誰の声かは言っていない。**

VoiceIdentity       voiceprint_id + confidence + model_version
                    「誰の声か」は言う。**確実ではない。**

PersonIdentity      person_id + canonical_name
                    関係値・記憶・目標が参照する。**ここだけが正本。**
```

`IdentityLink` が繋ぐ。状態は
`CANDIDATE` → `CONFIRMED` / `CONFLICTED` / `REVOKED` / `SUPERSEDED`。

### 解決の優先順位

1. 正本の Transport Identity → `AUTHORITATIVE`
2. 確認済みの Identity Link → `CONFIRMED`
3. 根拠が複数ある声紋 → `PROBABLE`（**既知扱いにはしない**）
4. 単発の声紋推定 → `PROBABLE`
5. → `UNKNOWN`

`IdentityResolution.known` は 1 と 2 だけ true。
**`PROBABLE` で関係値を動かさない。**

### 一度では確定しない

`CONFIRM_SUPPORT = 3`、`CONFIRM_CONFIDENCE = .72`。
声紋は数%揺れるので、たまたま高く出た1回で確定すると、
以後の関係値が全部別人へ流れる。

### 食い違った時

```
Discord は A だと言い、声紋は B だと言う
→ A を採る（接続を優先）
→ 声紋リンクを CONFLICTED にする
→ **リンクの持ち主は B のまま**（付け替えない）
→ A と B の関係値を混ぜない
→ 警告ログと Trace に残す
```

`CONFLICTED` なリンクは以後の根拠に使わないが、
**`UNKNOWN` とは区別して返す**——前者は根拠が2つあって割れている、
後者は根拠が無い。同じに見せると原因が追えない。

### Discord direct audio

`discord_direct` では Discord が「この user が喋った」を保証する。
その音声から出た声紋は、**その人のものだと言い切れる**。
`note_voice_sample` が根拠を積む（`model_version` と event ID 付き）。
確信 .72 未満は積まない——弱い一致を数えると `CONFIRMED` が意味を失う。

---

## 5. 誤統合を戻す仕組み

```
revoke(link_id, reason)
→ status = REVOKED
→ **物理削除しない**
→ revoked_reason が残る
→ 以後の根拠に使われない

relink(link_id, person_id, reason)
→ 新しい CANDIDATE リンクを作る
→ 古い方は SUPERSEDED + superseded_by
→ 出典（source_event_ids）は引き継ぐ
→ **確信は引き継がない**（また積み直す）
```

`history_for(value)` で、その識別子に紐づいた全てのリンクを
作成順に辿れる。

**過去の会話や記憶は自動で動かさない。**
`relink` のソースに `memories` / `transcript` / `episodes` /
`relationship` が出てこないことをテストで固定してある。
動かすと、繋ぎ直しがまた誤りだった時に二重に壊れる。

---

## 6. Local 参加人数の推定

Local のマイクには参加者一覧が無い。だから2つの数を分けて持つ:

```
authoritative_transport_count   接続が保証。**確定。**
estimated_unique_person_count   声から推した。**確定ではない。**
unknown_voice_count             誰か分からない声の本数
```

`PresenceSummary.certain` は
「接続の一覧がある **かつ** 身元不明の声が0」の時だけ true。

> **自分のプローブが自分の設計ミスを見つけた。**
> 最初は `certain = authoritative_count > 0` にしていた。
> 一覧は**繋いでいる人**を保証するが、**部屋に他の誰かが居ないこと**
> までは保証しない。身元の分からない声が1つでも聞こえていたら、
> 「いま3人だよね」とは言えない。`presence.counts` プローブが赤くなって
> 気づいた。

**同じマイクから別人の声が入っても、本人の関係値を動かさない**
（`known=False` なら `PresenceEntry` を作らず、不明な声として数えるだけ）。

---

## 7. 追加した映像Fact

`player_state.health_estimate` → `player:hp`（`low` / `medium` / `high`）。

**既存プロンプトが既に要求しているフィールド**で、新しい検出器も
新しい画像認識モデルも作っていない。世界状態側も `FACT_TTL["hp"] = 8.0` を
最初から持っていた——設計だけあって埋まっていなかった枠。

条件:

- **ゲーム中の画面でのみ**（メニュー中の「HPが少ない」は今の状況ではない）
- 確信 .70 以上（場面の .60 より高い。外すと「まだ大丈夫」と言って死ぬ）
- TTL 8秒
- **正本にしない**——画面は読み違えるので、確信で比べさせる
- 知らない言い方（「紫色」など）は空を返す。**近そうな段階へ寄せない**

---

## 8. 追加テストと結果

- `tests/test_identity_presence.py` — 106件
- 全体 **2265 passed / 17 subtests**、pyflakes clean、診断**要注意 0 件**

### 故障注入（赤くなることの確認）

| 切ったもの | 赤くなったもの |
|---|---|
| `CONFIRM_SUPPORT = 1` | `identity.layers` |
| 取り消しを物理削除にする | `identity.reversible` |
| 静かなだけで `LEFT` にする | `presence.counts` |
| 危険警告中でも挨拶を通す | `greeting.suppression` |
| 知らない人にも挨拶する | `greeting.cognitive` |
| メニュー中の体力も事実にする | `world.vision_health` |

1件目は最初のプローブが見逃した（往復だけ見ていて、
「1回目は候補」のままだったため）。**方針そのもの（定数）を見る**
チェックを足して赤くなるようにした。

---

## 9. 実機確認手順（**この順で、1つずつ**）

### ① `presence.registry_enabled`

1. Discord VC に何人かで入る。
2. 🩺の `presence` で `authoritative` が実人数（Bot 除く）と一致。
3. 誰かが抜ける → 減る。**その人が「居る」のままにならないこと。**
4. 全員が黙って数分待つ → **誰も `LEFT` にならないこと。**
5. ポッポを再起動 → VC へ入れ直す → 実状態から作り直されること。

### ② `discord.cognitive_greeting_enabled`

1. 誰かに入ってもらう → 挨拶が出る（または出ない）。
   **どちらでも正しい**——出ないなら🩺で理由を確認。
2. **誰かが話している最中**に別の人が入る → 挨拶で割り込まないこと。
3. 入って→すぐ抜けて→また入る → **挨拶を連打しないこと。**
4. 3人以上が同時に入る → 一人ずつ名指しで挨拶しないこと。
5. 危険警告（Minecraft の危険等）の最中に誰かが入る → 割り込まないこと。

### ③ `identity.resolution_enabled`

1. `discord_direct` で会話 → 🩺の `identity` に `people` と `links` が増える。
2. **Discord ID が違う2人が同じ表示名**でも、別々の人物になること。

### ④ `identity.voice_transport_linking_enabled`

1. 同じ人と何度か話す → `links` の声紋リンクが
   `candidate` → `confirmed` へ上がること（3回以上必要）。
2. **1〜2回では `confirmed` にならないこと。**
3. ログに「声紋が接続と食い違う」が出た場合、
   **関係値がその人へ移っていないこと**を🩺で確認。

### ⑤ 可逆リンク（`reversible_links_enabled` は元から true）

1. 誤って結びついたリンクがあれば `revoke_identity_link` で取り消す。
2. 🩺で `by_status` に `revoked` が1件増え、**件数は減らないこと。**

### ⑥ `presence.local_estimation_enabled`

1. ローカルマイクで、登録済みの人と未登録の人が交互に話す。
2. 🩺で `estimated` が増え、`authoritative` は変わらないこと。
3. **未登録の声で、既知の人の関係値が動いていないこと。**

### ⑦ `world_state.extended_vision_fact_enabled`

1. Minecraft で体力を減らす → 🩺に `player:hp = low`。
2. メニューを開く → **hp が更新されないこと。**
3. 10秒ほど待つ → hp が古くなること。

### ⑧ 通常利用

`discord.farewell_opportunity_enabled` はここで上げる。
直前まで話していた人が抜けた時だけ見送りが出ること。

どこかで想定と違ったら、そのフラグだけ false に戻す。

---

## 10. 残っている重要な穴

- **実機未検証**（上の手順は未実行）。
- **既存 `SpeakerRegistry` と `PersonIdentity` が統合されていない。**
  声紋プロファイル（`speaker:N`）と `person_id` は今のところ別々に動く。
  関係値は今も `speaker_id` を主キーにしている。移行するなら、
  既存データの移し替えと、移し替えを戻す手段が先に要る。
  **今回は「新しい層を足して、既存を壊さない」で止めてある。**
- **Local 側に接続の一覧が無い**ので、人数は最後まで推測のまま。
  これは環境の制約で、実装で埋まるものではない。
- **`ACKNOWLEDGE_PRESENCE` の言い回しは Planner 任せ。**
  実機で聞いてみないと、挨拶との差が出るか分からない。
- **挨拶の言葉そのものは未確認。** `GREET` が選ばれた後に
  Conversation Planner が何を言うかは、実機で聞くまで分からない。
