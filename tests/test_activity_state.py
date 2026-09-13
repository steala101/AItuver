from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from neuro_voice.activity import ActivityStateManager, ActionProposal, ValidationResult


class _Cfg:
    def get(self, key, default=None):
        return {
            "fact_grounding.enabled": True,
            "fact_grounding.max_state_history": 200,
        }.get(key, default)


class ActivityStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "activities.json"
        self.engine = ActivityStateManager(self.path, _Cfg())

    def tearDown(self):
        self.tmp.cleanup()

    def test_final_start_and_user_word_become_canonical(self):
        started = self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.assertTrue(started.handled)
        self.assertEqual(self.engine.snapshot()["current_actor_id"], "user")
        self.assertEqual(self.engine.snapshot()["status"], "WAITING_FOR_FIRST_MOVE")

        accepted = self.engine.handle_final_input("りんご", "user", source="local")
        self.assertFalse(accepted.handled)
        state = self.engine.snapshot()
        self.assertEqual(state["public_state"]["required_kana"], "ご")
        self.assertEqual(state["current_actor_id"], "assistant")
        self.assertEqual(state["state_version"], 1)

    def test_katakana_first_move_uses_first_move_path(self):
        self.engine.handle_final_input("しりとりしよう", "discord:42", source="discord")
        before = self.engine.snapshot()
        accepted = self.engine.handle_final_input("パンダ", "discord:42", source="discord", utterance_id="u-1")
        self.assertFalse(accepted.handled)
        self.assertEqual(accepted.input_intent, "GAME_MOVE")
        self.assertTrue(accepted.requires_assistant_move)
        after = self.engine.snapshot()
        self.assertEqual(before["status"], "WAITING_FOR_FIRST_MOVE")
        self.assertEqual(after["status"], "WAITING_FOR_AI")
        self.assertEqual(after["public_state"]["previous_word"], "ぱんだ")
        self.assertEqual(after["public_state"]["required_kana"], "だ")

    def test_kanji_word_uses_shared_reading_service(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        accepted = self.engine.handle_final_input("お米", "user", source="local")
        self.assertFalse(accepted.handled)
        self.assertEqual(accepted.result, ValidationResult.VALID)
        state = self.engine.snapshot()
        self.assertEqual(state["public_state"]["previous_word"], "こめ")
        self.assertEqual(state["public_state"]["required_kana"], "め")
        self.assertEqual(state["public_state"]["used_word_records"][0]["spoken_form"], "お米")

    def test_unknown_move_confirmation_commits_saved_candidate(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        # Tests do not assume an optional morphology package is installed.
        uncertain = self.engine.handle_final_input("🌀", "user", source="local")
        self.assertTrue(uncertain.handled)
        self.assertEqual(uncertain.reason, "uncertain_input")
        confirmed = self.engine.handle_final_input("合ってる", "user", source="local")
        self.assertTrue(confirmed.handled)
        self.assertEqual(confirmed.reason, "reading_unknown")
        # Confirmation retries the exact pending candidate; it never becomes a
        # second unrelated game move or silently changes the turn.
        self.assertEqual(self.engine.snapshot()["current_actor_id"], "user")

    def test_pending_move_can_be_corrected_naturally(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.engine.handle_final_input("🌀", "user", source="local")
        corrected = self.engine.handle_final_input(
            "いや、全然違う。お米だよ", "user", source="local",
        )
        self.assertFalse(corrected.handled)
        self.assertEqual(corrected.result, ValidationResult.VALID)
        self.assertEqual(corrected.reason, "pending_move_corrected")
        state = self.engine.snapshot()
        self.assertEqual(state["public_state"]["used_words"], ["こめ"])
        self.assertEqual(state["current_actor_id"], "assistant")

    def test_rejected_pending_move_accepts_next_one_word_answer(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.engine.handle_final_input("🌀", "user", source="local")
        rejected = self.engine.handle_final_input("違う", "user", source="local")
        self.assertTrue(rejected.handled)
        self.assertEqual(rejected.reason, "pending_move_rejected")
        accepted = self.engine.handle_final_input("米", "user", source="local")
        self.assertFalse(accepted.handled)
        self.assertEqual(self.engine.snapshot()["public_state"]["used_words"], ["こめ"])

    def test_meta_question_does_not_hit_wrong_turn_guard_and_retries_ai_turn(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.engine.handle_final_input("パンダ", "user", source="local")
        answer = self.engine.handle_final_input("考え直した？", "user", source="local")
        self.assertFalse(answer.handled)
        self.assertEqual(answer.input_intent, "META_QUESTION")
        self.assertTrue(answer.requires_assistant_move)
        self.assertEqual(self.engine.snapshot()["current_actor_id"], "assistant")

    def test_correction_of_already_committed_word_retries_ai_without_guard(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.engine.handle_final_input("パンダ", "user", source="local")
        answer = self.engine.handle_final_input("パンダって言ったよ", "user", source="local")
        self.assertFalse(answer.handled)
        self.assertEqual(answer.reason, "state_already_correct")
        self.assertTrue(answer.requires_assistant_move)

    def test_quoted_word_with_punctuation_is_valid(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.engine.handle_final_input("パンダ", "user", source="local")
        result, reason = self.engine.validate_and_commit_assistant_text("じゃあ「だるま！」")
        self.assertEqual((result, reason), (ValidationResult.VALID, None))

    def test_partial_never_creates_event(self):
        outcome = self.engine.handle_final_input("しりとりしよう", "user", source="local", is_final=False)
        self.assertFalse(outcome.handled)
        self.assertEqual(self.engine.snapshot(), {})

    def test_invalid_user_word_does_not_change_state(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.engine.handle_final_input("りんご", "user", source="local")
        before = self.engine.snapshot()
        invalid = self.engine.handle_final_input("ねこ", "assistant", source="local")
        self.assertEqual(invalid.result, ValidationResult.INVALID)
        self.assertEqual(self.engine.snapshot()["state_version"], before["state_version"])

    def test_stale_ai_proposal_is_rejected(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.engine.handle_final_input("りんご", "user", source="local")
        state = self.engine.snapshot()
        proposal = ActionProposal("p", state["activity_session_id"], 0, "assistant", "SUBMIT_WORD", {"word": "ごま"})
        result, reason = self.engine.validate_proposal(proposal)
        self.assertEqual(result, ValidationResult.STALE)
        self.assertEqual(reason, "state_version_changed")

    def test_ai_text_is_checked_and_committed_before_playback(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.engine.handle_final_input("りんご", "user", source="local")
        result, reason = self.engine.validate_and_commit_assistant_text("じゃあ「ごま」！ 次は「ま」だよ。", generation_id="turn-1")
        self.assertEqual((result, reason), (ValidationResult.VALID, None))
        state = self.engine.snapshot()
        self.assertEqual(state["public_state"]["required_kana"], "ま")
        self.assertEqual(state["current_actor_id"], "user")

    def test_wrong_announced_next_is_blocked_without_commit(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.engine.handle_final_input("りんご", "user", source="local")
        before = self.engine.snapshot()
        result, reason = self.engine.validate_and_commit_assistant_text("「ごま」！ 次は「ね」だよ。")
        self.assertEqual((result, reason), (ValidationResult.INVALID, "wrong_announced_next"))
        self.assertEqual(self.engine.snapshot()["state_version"], before["state_version"])

    def test_explicit_last_move_correction_rebuilds_from_events(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.engine.handle_final_input("りんご", "user", source="local")
        # The final move is a human move, so its replacement is unambiguous.
        corrected = self.engine.handle_final_input("りんごじゃなくてりす", "user", source="local")
        self.assertEqual(corrected.result, ValidationResult.VALID)
        state = self.engine.snapshot()
        self.assertEqual(state["public_state"]["used_words"], ["りす"])
        self.assertEqual(state["public_state"]["required_kana"], "す")

    def test_restart_rebuilds_snapshot_from_ledger(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.engine.handle_final_input("りんご", "user", source="local")
        self.engine.validate_and_commit_assistant_text("「ごま」")
        recovered = ActivityStateManager(self.path, _Cfg())
        self.assertEqual(recovered.snapshot()["public_state"]["used_words"], ["りんご", "ごま"])

    def _state_where_suzume_is_used_and_assistant_needs_su(self):
        self.engine.handle_final_input("しりとりしよう", "user", source="local")
        self.engine.handle_final_input("すずめ", "user", source="local")
        self.engine.validate_and_commit_assistant_text("「めだか」")
        self.engine.handle_final_input("かりす", "user", source="local")
        return self.engine.snapshot()

    def test_ai_duplicate_is_normalized_and_never_committed(self):
        before = self._state_where_suzume_is_used_and_assistant_needs_su()
        result, reason = self.engine.validate_and_commit_assistant_text("「スズメ！」")
        self.assertEqual((result, reason), (ValidationResult.INVALID_RULE, "word_already_used"))
        after = self.engine.snapshot()
        self.assertEqual(after["state_version"], before["state_version"])
        self.assertEqual(after["public_state"]["used_word_keys"], before["public_state"]["used_word_keys"])
        self.assertIn("すずめ", self.engine.retry_context())
        self.assertEqual(after["current_actor_id"], "assistant")

    def test_second_rejected_candidate_pauses_instead_of_looping(self):
        self._state_where_suzume_is_used_and_assistant_needs_su()
        self.engine.validate_and_commit_assistant_text("「すずめ」")
        result, reason = self.engine.validate_and_commit_assistant_text("「スズメ」")
        self.assertEqual((result, reason), (ValidationResult.STATE_CONFLICT, "max_reproposals_exceeded"))
        self.assertEqual(self.engine.snapshot()["status"], "PAUSED")

    def test_duplicate_correction_invalidates_erroneous_ai_commit_and_replays(self):
        before = self._state_where_suzume_is_used_and_assistant_needs_su()
        # Simulate a legacy erroneous commit that bypassed the current validator.
        bad = self.engine._append("AI_MOVE_COMMITTED", "assistant", {
            "proposal_id": "legacy-bad", "surface": "スズメ", "word": "すずめ", "word_key": "すずめ",
        }, provenance="LEGACY")
        self.engine.rebuild()
        self.assertEqual(self.engine.snapshot()["integrity_status"], "CONFLICTED")
        self.assertEqual(self.engine.snapshot()["status"], "NEEDS_REPAIR")
        self.assertIsNone(self.engine.snapshot()["current_actor_id"])
        repaired = self.engine.handle_final_input("スズメ2回目だよ", "user", source="local", utterance_id="fix-1")
        self.assertFalse(repaired.handled)
        self.assertEqual(repaired.reason, "erroneous_commit_rolled_back")
        after = self.engine.snapshot()
        self.assertEqual(after["integrity_status"], "VALID")
        self.assertEqual(after["public_state"]["previous_word"], before["public_state"]["previous_word"])
        self.assertEqual(after["public_state"]["required_kana"], "す")
        self.assertEqual(after["current_actor_id"], "assistant")
        self.assertIn("すずめ", self.engine.retry_context())
        self.assertTrue(any(e.get("event_type") == "AI_MOVE_INVALIDATED" for e in self.engine._data["events"]))

    def test_completed_activity_exposes_no_next_actor_or_required_kana(self):
        self.engine.handle_final_input(
            "しりとりしよう", "user", source="local",
        )
        self.engine.handle_final_input("りんご", "user", source="local")
        ended = self.engine.handle_final_input(
            "しりとりをやめる", "user", source="local",
        )
        self.assertTrue(ended.handled)
        state = self.engine.snapshot()
        self.assertEqual(state["status"], "COMPLETED")
        self.assertIsNone(state["current_actor_id"])
        self.assertIsNone(state["public_state"]["required_kana"])


if __name__ == "__main__":
    unittest.main()
