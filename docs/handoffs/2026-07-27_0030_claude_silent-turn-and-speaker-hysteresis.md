# AI作業Handoff

- 担当AI: Claude (Cowork / claude-opus-5)
- Role: review / implementation / test
- Task: Turn ClosureとBehaviorDirectiveの継ぎ目、および話者ヒステリシスが確定一致を上書きする問題の修正
- Started At: 2026-07-27 (JST)
- Finished At: 2026-07-27 (JST)
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: 実働ルート直接 (R-001のとおりGit未管理)
- Start Commit: なし (Git履歴なし)
- End Commit: なし
- Work Lock: Codexの2026-07-26 22:09の作業完了後に単独で実施

## 目的

ユーザー依頼「Codexの変更履歴を確認してほしい」に対する監査で見つかった2件を修正する。

1. Turn Closureが沈黙を選んだターンがDirective層へ届かず、ターン制セッションが無言で停止する。
2. `test_speaker_merge.py::test_the_longer_history_is_offered_as_the_target` がCodex環境でのみ失敗し、5エントリ連続で「既知・未変更」として持ち越されている。

## 作業開始時の状態

- Git status: 取得不可（R-001）
- 既存変更: Codexによる2026-07-26 16:28〜22:09の14エントリぶんが適用済み
- 読んだ憲法/ADR: `PROJECT_CONSTITUTION.md` 1.1.0、`AGENTS.md`、`SHARED_CHANGELOG.md`（全エントリ）、`KNOWN_RISKS_AND_DEBT.md`、`DECISION_LOG.md` D-013〜D-018
- テストbaseline: 726 passed / 3 skipped（音声・GUI依存4ファイル除外、sandbox Python 3.10 + StrEnum shim）

## 調査結果

### 1. 沈黙したターンがDirective層へ届かない

コード上の連鎖は3リンクで、推測を含まない。

| # | 位置 | 事実 |
|---|---|---|
| 1 | `pipeline.py:1058` / `bot.py:3448` | `response_plan.should_respond` が偽なら早期`return` |
| 2 | `pipeline.py:1417` / `bot.py` | `_handle_directive_turn` → `begin_user_turn` はその先の`_start_response`内にある |
| 3 | `continuation.py:409` | `note_user_turn_started` が `WAITING_FOR_USER` を解除する唯一の経路 |
| 4 | `continuation.py:305` | `due_directive` は `WAITING_FOR_USER` の間 `None` を返し続ける |

結果: NARRATION（GM）が手番を返した直後にプレイヤーが「うん」と答えると、Turn Closureが `exchange_naturally_closed` で沈黙を選び、Directiveは永久に `WAITING_FOR_USER` のまま止まる。応答も区間実行も発生せず、ログにも失敗として出ない。

`state.activity_active`（ActivityStateManager、しりとり等）はガードされていたが、BehaviorDirectiveはガード対象に入っていなかった。Local/Discord両方に同形で存在。

### 2. 話者ヒステリシスが確定一致を上書きする

`speakers.py::identify` のヒステリシス条件は `best_score < self._sticky_margin + sticky_score` のみで、**bestが確定閾値以上でも**直前話者へ引き戻していた。

`_sticky_profile` は `max(recent, key=last_seen)` で「直前に話した人」を選ぶが、`last_seen` は `time.time()`（wall clock）である。Windowsの`time.time()`は約15.6msごとにしか進まないため、1tick内の2発話は同一timestampになり、同着時の`max`はリスト先頭＝先に登録されたプロファイルを返す。

これが結合して、**新規登録直後の本人の発話が、直前話者（別人）へ吸い寄せられる**。ジーレンのプロファイルを作った直後にジーレン本人が話しても、チビ側へ加算され続ける。テスト失敗はこの副作用（`turns`が誤ったプロファイルへ積まれる）が表に出たもので、Linuxではns解像度のため再現しない。

環境依存の失敗であって「他人のテストの問題」ではなく、実機Windows上の実害がある不具合だった。

### async/queue/cancel

- `note_suppressed_user_turn` は同期メソッドで、task生成・LLM呼び出しを一切行わない。相槌1回ごとにモデルを呼ばないための意図的な制約（第3条）。
- 停止語だけは例外で、既存の `_stop_for_user`（計画task・区間task・生成・未再生音声の一括取消）へ委譲する。

### privacy/data

- 追加した状態は `directive_waiting_for_user` の真偽値のみ。発話内容・話者情報を新たに複製していない。
- `_seen_order` はプロセス内の整数カウンタで、永続化しない。

## 変更

| File | Change | Why |
|---|---|---|
| `neuro_voice/dialogue/directive_runtime.py` | `note_suppressed_user_turn()` を追加 | 沈黙したターンでも手番を返す。Directive作成・計画改訂は行わない意図的に狭い経路 |
| `neuro_voice/dialogue/state.py` | `ConversationState.directive_waiting_for_user` を追加し、metadataから更新・snapshotへ公開 | 会話状態の所有者はConversationState（第5条） |
| `neuro_voice/dialogue/turn_closure.py` | `directive_waiting_for_user` なら `CONTINUE`（reason=`directive_awaiting_user_move`） | 手番待ちの沈黙は「自然に閉じた会話」ではなく停止 |
| `neuro_voice/mind/mind.py` | `directive_waiting_for_user(source)` を追加 | 判断はMindが所有する（D-015） |
| `neuro_voice/pipeline.py` | metadataへフラグを供給。応答見送り経路で `note_suppressed_user_turn` を呼ぶ | Local側の配線 |
| `neuro_voice/discord_bridge/bot.py` | 同上（`_note_silent_user_turn` 経由） | 片側だけ変更しない（第19条・不変条件） |
| `neuro_voice/mind/speakers.py` | ヒステリシス条件へ `best_score < self._threshold` を追加 | 確定一致を直前話者で上書きしない |
| `neuro_voice/mind/speakers.py` | `_seen_order` 単調カウンタと `_mark_seen()` を追加し、`_sticky_profile` の同着を解消 | wall clockの解像度に「直前に話した人」を依存させない（第13条） |
| `neuro_voice/mind/speakers.py` | `merge_candidates` の同着を `turns → n → id` で決定的に | リスト順で提案が変わっていた |
| `neuro_voice/mind/speakers.py` | `merge()` / `forget()` で `_seen_order` を整理 | id再利用時に古い順序が残らないように |
| `tests/test_silent_turn_directive.py` | 新規15件 | 下記テスト節 |
| `tests/test_speaker_stability.py` | 3件追加 | 確定一致の優先、wobble時の従来動作維持、粗い時計の模擬 |
| `tests/test_speaker_merge.py` | 対象テストを書き直し、順序非依存テストを追加 | 下記「失敗した試行」参照 |

## 変更しなかったもの

- Turn Closureの語彙（`_FEEDBACK_PHRASES` 等）は一切触っていない。R-030の誤沈黙は実会話replayで測るべきで、規則の足し込みで潰す対象ではない（D-016）。
- `conversation.turn_closure.enabled` のrollback経路はそのまま。
- `sticky_margin` の既定値0.08は変更していない。効いていなかったのは値ではなく条件。

## 設計判断

1. **沈黙は「話さない」決定であって「ターンが無かった」ではない。** Turn Closureは発話の有無だけを決め、セッションの手番はDirective層が持つ。両者を混ぜないため、沈黙経路にはDirective作成も計画改訂も置かない。
2. **手番待ちだけを特別扱いする。** ACTIVEなラジオ中の相槌は従来どおり沈黙でよい。`WAITING_FOR_USER` は「こちらが訊いて待っている」という明示状態なので、そこだけCONTINUEへ落とす。
3. **ヒステリシスは揺れを吸収するもので、確証を覆すものではない。** 上限を確定閾値に置くことで、margin値の調整に頼らず意味で線を引いた。
4. **「直前に話した人」は順序の問題であって時刻の問題ではない。** wall clockの解像度に依存させず、単調カウンタで順序を持つ。

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| `pytest tests/ -q`（sandbox / Python 3.10 + StrEnum shim、音声・GUI依存4件除外） | 726 passed, 3 skipped, 11 subtests | 全実行ログ確認 |
| `tests/test_silent_turn_directive.py`（新規15件） | pass | 修正を戻すと**7件が失敗**することを確認済み |
| `tests/test_speaker_stability.py`（9件、うち新規3件） | pass | 修正を戻すと**2件が失敗**することを確認済み |
| `tests/test_speaker_merge.py`（18件） | pass | 対象テストが順序・時計に依存しないことを確認 |
| 既存テストの回帰 | なし | 開始時726 → 終了時726（数が同じなのは書き換え1件と追加18件の差） |
| `python3 -m compileall neuro_voice` | exit 0 | — |

- 除外理由: `test_style_bert_vits2` / `test_ui_mind_status` / `test_discord_screen_share` / `test_local_audio_liveness` はsandboxに`sounddevice`・GPU・GUIがないため収集不能。実機では対象に含めること。
- 未実施: **実機でのTRPG継続、Discord側の同挙動、Windows上での話者名の安定**
- Performance p50/p95: 未計測

### 修正前に失敗することを確認したテスト

「テストを書いた」だけでは根拠にならないため、修正を一時的に戻して失敗を確認した。

- 沈黙の継ぎ目: `test_a_session_waiting_for_the_player_is_never_silenced` / `test_a_silenced_answer_hands_the_floor_back` / `test_the_stalled_session_can_act_again` / `test_a_silenced_turn_does_not_revise_the_plan` / `test_a_paused_directive_resumes_on_a_silenced_turn` / `test_both_surfaces_clear_the_wait_identically[local, discord]`
- 話者: `test_a_certain_match_beats_the_previous_speaker` / `test_who_spoke_last_survives_a_coarse_clock`

## 設定・Migration

- Config: 変更なし。新しい設定キーを追加していない。
- Environment: 変更なし。
- DB schema: 変更なし。`data/speakers_*.json` の形式も不変（`_seen_order`はメモリ上のみ）。

## Rollback

- 沈黙の継ぎ目: `conversation.turn_closure.enabled: false` でTurn Closure自体を止めれば、修正前の経路は発生しなくなる。個別に戻す場合は `turn_closure.py` のガードと、両surfaceの `note_suppressed_user_turn` 呼び出しを削除する。
- 話者ヒステリシス: `speakers.py` の `best_score < self._threshold` を外すと元の挙動へ戻る。`sticky_margin: 0.0` でヒステリシス自体を無効化することもできる。

## 失敗した試行

**`test_the_longer_history_is_offered_as_the_target` は、もともと私のテスト設計が悪かった。**

このテストは `identify()` を9回呼んで履歴を積んでいた。検証したいのは `merge_candidates` の同着処理だけなのに、閾値判定・ヒステリシス・重心更新・wall clockまで巻き込んでいた。結果として、Linuxでは通りWindowsでは落ちるテストになり、**Codex側で5エントリ連続「既知の失敗」として持ち越された**。

私が受け取るべき教訓は2つある。

1. 対象の振る舞いだけを直接組み立てる。副作用で状態を作ると、テストは対象以外の変更で壊れる。
2. 他AIが「未変更のテストが落ちる」と報告した時点で、それは持ち越し事項ではなく調査対象である。今回は実際に実機Windowsで起きる不具合の兆候だった。

なお、私が2026-07-26に入れた `AudioDeviceMonitor` の既定 `refresh=True` も同じ形の誤りで（Codexが17:40に修正済み）、テストが「引数を渡せばスキップできる」ことしか確かめておらず、既定値の危険性を検証していなかった。

## 憲法・ADRへの影響

- 第5条（状態所有権）: 会話状態は`ConversationState`、継続依頼は`ContinuationController`という分担を維持したまま、両者の受け渡しだけを追加した。
- 第13条（時間）: 「直前に話した人」の順序判定をwall clock解像度から切り離した。
- 第19条（変更の互換性）: Local/Discord両方を同一意味論で変更した。
- D-015（判断はMind、実行はサーフェス）に従い、`directive_waiting_for_user` の判定はMindへ置いた。
- R-030（Turn Closureの誤沈黙）へ本件を追記した。
- R-021（話者identity誤統合）へヒステリシスの件を追記した。

## 次の担当への注意

- **Turn Closureへ「〜の場合は必ず沈黙」といった規則を足す前に、D-016を読むこと。** 語彙で潰すと、ユーザーが言葉で覆せない振る舞いが増える。
- `note_suppressed_user_turn` にDirective作成・計画改訂を足さないこと。相槌1回ごとにLLM呼び出しが増え、D-017（ローカルLLMは直列）に反する。
- 話者テストを書くとき、`identify()` を状態構築の手段として使わないこと。
- 実機受入項目: (a) TRPGでGMの問いに「うん」とだけ答えて物語が進むか、(b) ラジオ中の相槌で番組が止まらないか、(c) Windowsで連続発話中に話者名が入れ替わらないか、(d) Discordで同じ3点。
