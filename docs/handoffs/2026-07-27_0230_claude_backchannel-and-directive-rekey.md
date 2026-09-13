# AI作業Handoff

- 担当AI: Claude (Cowork / claude-opus-5)
- Role: investigation / implementation / test
- Task: 相槌で読み上げが停止する問題と、話者確定でDirectiveが孤児になる問題の修正
- Started At: 2026-07-27 (JST)
- Finished At: 2026-07-27 (JST)
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: 実働ルート直接 (R-001のとおりGit未管理)
- Work Lock: 同セッション内で連続作業。他AIの同時編集なし

## 目的

ユーザー報告2件を実機ログ（`logs/neuro_voice.log` 2026-07-26 23:07〜23:16）から根本原因まで追い、修正する。

1. 「うん」「うんうん」の相槌だけで読み上げが完全に停止し、復帰しない。
2. TRPG開始後に設定確認などを挟むと応答が壊れ、物語へ復帰できない。

## 作業開始時の状態

- Git status: 取得不可（R-001）
- 既存変更: 同セッションの `2026-07-27_0030` および `_0130` の変更が適用済み
- テストbaseline: 736 passed / 3 skipped
- 対象ログはこれらの修正**前**のセッション（GUIは23:15時点のまま）

## 調査結果

### 1. 相槌で読み上げが止まる

VAD検知（STTより前、まだ相槌か割り込みか不明）の時点で破壊的処理を行っていた。

`pipeline.py::_on_speech_start` → `_pause_directive_for_human()` → `directive_runtime.pause_for_human()` の中で:

- `self._host.cancel_generation(...)`
- `self._host.discard_pending_audio()` → `playback.discard_paused_audio()`

つまり**声を出した瞬間に合成済みの音声キューを全部捨てていた**。その後STTが完了し「うんうん」をACKNOWLEDGEMENTと判定して `resume_from_pause()` を呼ぶが、**戻すべき音声がもう存在しない**。

さらにACKNOWLEDGEMENT分岐は `return` するため、`handle_user_turn` / `note_suppressed_user_turn` のどちらにも到達せず、**Directiveは PAUSED のまま誰にも解除されない**。相槌1回でラジオが終わる。

実ログ:

```
23:09:44,863 barge-in: 相槌として継続 (うんうん)
（以降、区間の再生なし）
```

憲法第7条の「ユーザー発話開始時は速やかに無音化し、**相槌なら位置を維持して再開する**」「soft interruptionとhard interruptionを分ける」への違反。確定前にhard扱いしていた。

### 2. TRPGが復帰できない — Directiveの孤児化

```
23:07:24,193  Directive created id=70e74988d1c0 interaction=NARRATION   ← TRPG本体
23:07:24,561  話者照合: チビ に類似度 0.97                              ← 368ms遅れて確定
23:08:02,786  Directive created id=9ab77062aa28 interaction=DIALOGUE    ← 別Directive
23:16:38,960  Directive finished id=9ab77062aa28 ... segments=19 elapsed=440s
```

`70e74988d1c0` の `finished` ログはセッション中に一度も出ていない。

原因: `conversation_id` は `Mind._dialogue_user_key()` が返す。

```python
if sid >= 0: return f"speaker:{sid}"
if name:     return f"hint:{...}"
return f"{source}:user"
```

**Directiveは話者識別の完了前に作られる**（声紋照合は非同期で数百ms遅れる）ため `local:user` に登録される。次のターン以降は `speaker:1` で検索するため `active()` が `None` を返し、`_create_from_plan` が新しいDirectiveを作る。NARRATIONは終端状態にも履歴にも入らず、参照する者がいないまま生き続ける。

物語を保持していたDirectiveが到達不能なので、「物語の復帰が難しい」のは当然だった。以前これを「話題の乗っ取り」と解釈して話題ガードで塞いだが、真因はこちらである。

憲法第5条（状態所有権）の違反: Directiveのキーが、Directiveの寿命中に変化する値から導出されていた。

### async/queue/cancel

- `pause_for_human(discard_audio=False)` は生成の停止（task cancel、`_queued_segments = 0`）までは行い、`cancel_generation` / `discard_pending_audio` を呼ばない。
- 割り込み確定後に `pause_for_human(discard_audio=True)` を呼び、そこで初めて破棄する。`state_version` による stale 破棄の仕組み自体は不変。
- `rekey` は `DirectiveStore._lock` の内側で辞書を差し替える。

### privacy/data

- 追加した状態はキー文字列のみ。発話内容・音声を新たに保存しない。
- ログへ出すのは directive_id とキー名（`speaker:1` 等）で、話者名や発話は含めない。

## 変更

| File | Change | Why |
|---|---|---|
| `neuro_voice/dialogue/directive_runtime.py` | `pause_for_human(discard_audio=True)` へ引数追加。`False` なら生成停止のみ | 相槌か割り込みか不明な時点で音声を捨てない |
| `neuro_voice/dialogue/directive_runtime.py` | `resume_after_backchannel()` を追加 | 相槌経路はreply pathへ到達しないため、ここでしか解除できない |
| `neuro_voice/dialogue/directive.py` | `DirectiveStore.rekey(old, new)` を追加。`put()` の置換をログへ | 話者確定・話者統合でキーが変わってもセッションを追従させる／無言の消滅をなくす |
| `neuro_voice/dialogue/continuation.py` | `rekey()` と `directive_rekeys` メトリクス | 観測可能性（第20条） |
| `neuro_voice/mind/mind.py` | `_directive_key_snapshot()` / `_follow_speaker_key()`。`identify_speaker` と `set_speaker_hint` から呼ぶ。`merge_speakers` でも rekey | 判断はMindが所有（D-015） |
| `neuro_voice/pipeline.py` | VAD開始は `discard_audio=False`。割り込み確定時に `discard_audio=True`。相槌／誤検知／barge-in無効時に `_resume_directive_after_backchannel()` | Local側の配線 |
| `neuro_voice/discord_bridge/bot.py` | 同上（`_on_discord_speech_start` と3分岐） | 片側だけ変更しない（第19条） |
| `tests/test_backchannel_and_rekey.py` | 新規15件 | 下記テスト節 |

## 変更しなかったもの

- `SpeechIntent` の分類器そのもの。相槌の判定は正しく動いていた（ログで確認）。壊れていたのは判定前の破壊処理と、判定後の復帰漏れ。
- `_dialogue_user_key` の導出規則。キーが変わること自体は正しく、変わった時に追従していなかったことが問題。
- `clear_speaker_context()` からは rekey しない。Discordで話者が不明になった時に、進行中セッションを汎用キーへ移すのは意味が逆になる。

## 設計判断

1. **確定していない情報で不可逆な処理をしない。** VAD検知は「人が音を出した」以上の情報を持たない。そこでできるのは「作るのを止める」までで、「捨てる」はSTTが割り込みと確定してから。第7条のsoft/hard分離をコードの構造として表現した。
2. **相槌は会話の一部であって、セッションの中断ではない。** 相槌後にDirectiveを能動的に再開する経路を明示的に置いた。「いつか次のtickで戻る」に頼らない。
3. **キーが変わる前提で設計する。** 話者は会話の途中で判明し、統合で変わり、Discordではヒントで変わる。Directive側に固定キーを持たせるのではなく、キーの変化に追従する`rekey`を1箇所用意した。移動先に別の進行中セッションがある場合は**新しい方を優先**し、古いものを復活させない。
4. **消えるなら記録を残す。** `put()` の置換は今までログを出しておらず、TRPGが1行の痕跡もなく消えた。第20条の趣旨に反していた。

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| `pytest tests/ -q`（sandbox / Python 3.10 + StrEnum shim、音声・GUI依存4件除外） | 751 passed, 3 skipped, 11 subtests | 全実行ログ確認 |
| `tests/test_backchannel_and_rekey.py`（新規15件） | pass | 修正を戻すと**4件が失敗**することを確認済み |
| `test_local_audio_liveness.py` + `test_discord_screen_share.py`（sounddeviceスタブ下） | 21 passed | Codexの視覚系・音声系へ回帰なし |
| `python3 -m compileall neuro_voice` | exit 0 | — |

### 修正前に失敗することを確認したテスト

- `test_vad_onset_keeps_the_audio_already_made`
- `test_a_confirmed_interruption_does_discard`
- `test_the_session_survives_the_speaker_becoming_known`
- `test_the_directive_is_not_left_behind_at_the_old_key`

`test_without_the_move_the_session_would_be_unreachable` は、孤児化した状態そのものを固定するテスト（rekeyを呼ばなければ到達不能になることを明示）。

- 未実施: **実機での相槌・TRPG通し確認**
- Performance p50/p95: 未計測

## 設定・Migration

- Config: 変更なし。新規キーなし。
- DB schema: 変更なし。
- 再起動: **GUI再起動が必要**（実行中プロセスへは反映されない）。

## Rollback

- 相槌: `pause_for_human` の `discard_audio` 既定は `True` のままなので、両surfaceの呼び出しから引数を外せば従来動作へ戻る。`_resume_directive_after_backchannel()` の呼び出し3箇所を削除する。
- rekey: `Mind._follow_speaker_key` の呼び出しを外せば従来動作。`DirectiveStore.rekey` は残っていても副作用がない。

## 失敗した試行

**どちらも私が入れた設計の誤りで、しかも症状の解釈を一度間違えている。**

「物語が壊れる」「話題が乗っ取られる」という以前のユーザー報告に対し、私は話題抽出と話題ガードを直した（2026-07-26のD-014周辺）。テストも通り、症状は一時的に減った。しかし真因は**Directiveのキーが話者識別の完了前後で変わること**で、話題の問題ではなかった。表層の症状に合う修正を当てると、根本原因が残ったまま「直った」ように見える。

同様に、相槌の破棄は「割り込み時に古い音声が残る」問題への対策として `pause_for_human` に置いたものだが、**呼び出し箇所が確定前だった**ため、対策が別の不具合になった。処理の正しさだけでなく、**それを呼ぶ時点で何が確定しているか**を見る必要がある。

## 憲法・ADRへの影響

- 第5条（状態所有権）: Directiveのキーが可変値から導出される問題を、追従（rekey）という形で解決した。キー生成規則自体は変えていない。
- 第7条（音声会話と割り込み）: soft/hard interruptionの分離を、判定タイミングとしてコード構造に落とした。
- 第19条: Local/Discord両方を同一意味論で変更。
- 第20条: Directiveの置換とrekeyをログ・メトリクス・イベントで観測可能にした。
- R-026 / R-030 へ本件を追記。

## 次の担当への注意

- **`_on_speech_start` から不可逆な処理を呼ばないこと。** この時点で確定しているのは「人が音を出した」だけで、相槌か割り込みかは分からない。
- 相槌分岐で早期returnを足す場合、Directiveの再開が漏れていないか確認すること。この分岐はreply pathへ到達しない。
- `_dialogue_user_key` の規則を変える場合、`_follow_speaker_key` の呼び出し箇所（`identify_speaker` / `set_speaker_hint` / `merge_speakers`）を見直すこと。
- 実機受入項目: (a) ラジオ中に「うん」と言って読み上げが途切れず続くか、(b) 実質的な割り込みでは即座に止まるか、(c) TRPG開始→設定確認→物語再開で同一Directiveが続くか（ログの `Directive created` が1回だけであること、`Directive rekeyed` が出ること）、(d) Discordで同3点。
