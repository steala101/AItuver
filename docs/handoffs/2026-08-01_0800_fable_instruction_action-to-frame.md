# Fable 5 向け作業指示: ActionType を TurnFrame へ繋ぐ

## 目的

認知カーネルが選んだ `ActionDecision` を、`TurnFrame` の
`response_goal` / `response_shape` / `social_mode` へ反映する。
現状は決定がログに出るだけで、発話に届いていない。

設計は確定済み。**判断はせず、下記のとおり実装すること。**

## 変更ファイル

1. `neuro_voice/cognition/bridge.py`（新規）
2. `neuro_voice/mind/mind.py`（メソッド1つ追加）
3. `neuro_voice/pipeline.py`（1行追加）
4. `tests/test_cognitive_bridge.py`（新規）

## ファイルごとの変更内容

### 1. `neuro_voice/cognition/bridge.py`（新規）

```python
ACTION_TO_FRAME: dict[ActionType, dict[str, str]] = {
    ActionType.ANSWER: {
        "response_goal": "Answer the question directly before adding one useful thought.",
        "response_shape": "answer_first", "social_mode": "neutral"},
    ActionType.ACKNOWLEDGE_EMOTION: {
        "response_goal": "Acknowledge how the person feels before anything else. Do not lead with a solution.",
        "response_shape": "acknowledge_then_answer", "social_mode": "supportive"},
    ActionType.ASK_CLARIFICATION: {
        "response_goal": "Ask one concrete clarification question; do not search or guess.",
        "response_shape": "clarify_reference", "social_mode": "neutral"},
    ActionType.CHALLENGE_ASSUMPTION: {
        "response_goal": "Name the assumption you disagree with and give one reason.",
        "response_shape": "direct_take", "social_mode": "candid"},
    ActionType.CONTINUE_PREVIOUS_TOPIC: {
        "response_goal": "Pick up the unresolved thread instead of starting a new one.",
        "response_shape": "resolve_then_answer", "social_mode": "neutral"},
    ActionType.WARN: {
        "response_goal": "Warn about the immediate danger in one short sentence. Details only if asked.",
        "response_shape": "urgent_warning", "social_mode": "urgent"},
    ActionType.MAKE_LIGHT_JOKE: {
        "response_goal": "React playfully to one concrete detail, then return to the topic.",
        "response_shape": "playful_twist", "social_mode": "playful"},
    ActionType.RESUME_INTERRUPTED_RESPONSE: {
        "response_goal": "Finish the sentence you were cut off from, briefly.",
        "response_shape": "resume", "social_mode": "neutral"},
}
```

`frame_changes(decision) -> dict[str, str] | None` を実装する。

- `decision.selected_action` が表に無ければ `None`
- `decision.speaks` が False なら `None`
- それ以外は表の dict の**コピー**を返す

### 2. `neuro_voice/mind/mind.py`

`cognitive_state()` の直後へ追加する。

```python
def apply_cognitive_decision(self, decision, *, source: str = "local",
                             response_id: str = "") -> bool:
```

- `from neuro_voice.cognition.bridge import frame_changes`
- `changes = frame_changes(decision)`。`None` なら `False` を返す
- `self._kernel.guarded_transition(changes, source=source,
  response_id=response_id or None,
  transition_reason=f"cognitive_kernel:{decision.decision_reason}"[:120])`
  の結果を返す
- 例外は `logger.exception` して `False`

### 3. `neuro_voice/pipeline.py`

`self._cognitive_decision = self._cognitive_decide(user_text, transcript)` の
**直後**に追加する。

```python
if self._cognitive_decision is not None and self._mind is not None:
    self._mind.apply_cognitive_decision(
        self._cognitive_decision, source="local", response_id=response_id)
```

## 守るインターフェース

- `guarded_transition` 以外で `TurnFrame` を書き換えない
- `transition_reason` を**空にしない**（保護フィールドなので拒否される）
- `ACTION_TO_FRAME` に無い `ActionType` を勝手に足さない
- `cognition.enabled` が false のとき、`_cognitive_decide` は `None` を返す。
  上記3の `if` がそのまま偽になるので、**追加の分岐を書かない**

## テスト（`tests/test_cognitive_bridge.py`）

1. 表にある8つの `ActionType` すべてで、3キーが揃った dict が返る
2. `REMAIN_SILENT` / `STORE_MEMORY` / `ABANDON_INTERRUPTED_RESPONSE` は `None`
3. 返り値を書き換えても `ACTION_TO_FRAME` が変わらない（コピーであること）
4. `apply_cognitive_decision` が `guarded_transition` を
   **空でない `transition_reason` 付きで**呼ぶ（モックで確認）
5. `frame_changes` が `None` のとき `guarded_transition` を呼ばない

## 禁止事項

- `REMAIN_SILENT` で発話を止める処理を書かない（**範囲外**。別作業）
- `kernel.py` の `_PROTECTED_FIELDS` を変更しない
- `response_director.py` / `conversation_planner.py` を変更しない
- Discord 側（`discord_bridge/bot.py`）に手を入れない
- 新しい設定キーを追加しない
- 既存テストの期待値を書き換えない

## 完了条件

- 上記4ファイルの変更が入っている
- 新規テスト5件が通る
- 全体回帰が通る（実行前 **1343 passed / 17 subtests**）
- `python -m pytest tests/test_no_undefined_names.py` が通る
- `docs/SHARED_CHANGELOG.md` へ1項目追記（必須記録形式に従う）
