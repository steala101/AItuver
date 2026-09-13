"""Phase 6D: 誰が居るのか・誰なのか・挨拶。

これまで1つの `speaker_id` が3つの違うことを同時に意味していた:
どの接続から来たか、どの声に似ているか、誰として覚えているか。
**同じ番号で扱うと、声紋が一度外れただけで関係値が別人へ移る。**
そして移った先が正しいか確かめる手がかりが残らない。

いちばん守りたいのは4つ:

* **接続を声紋で上書きしない**（Discord ID の方が確実）
* **一度の声紋一致で人物を確定しない**
* **誤統合を必ず戻せる**（物理削除しない）
* **推定人数を確定人数として喋らない**
"""
from __future__ import annotations

from pathlib import Path

import pytest

from neuro_voice.cognition.identity import (
    CONFIRM_SUPPORT, IdentityResolver, IdentityType, LinkStatus,
    ResolutionStatus, TransportIdentity, VoiceIdentity,
)
from neuro_voice.cognition.initiative import (
    MASS_JOIN_THRESHOLD, RECONNECT_WINDOW, OpportunityType, SpeakingConditions,
    farewell_opportunity, greeting_opportunity, suppression_reason,
)
from neuro_voice.cognition.presence import (
    PresenceRegistry, PresenceSource, PresenceStatus,
)
from neuro_voice.cognition.runtime import InitiativeRuntime
from neuro_voice.cognition.types import ActionType

from tests.test_world_wiring import WIRED, Cfg, store_at

SOCIAL = {
    **WIRED,
    "identity.resolution_enabled": True,
    "identity.voice_transport_linking_enabled": True,
    "identity.reversible_links_enabled": True,
    "presence.registry_enabled": True,
    "presence.local_estimation_enabled": True,
    "discord.greet_on_join": True,
    "discord.cognitive_greeting_enabled": True,
    "discord.farewell_opportunity_enabled": True,
    "world_state.extended_vision_fact_enabled": True,
    "initiative.enabled": True,
}


def runtime(**overrides) -> InitiativeRuntime:
    return InitiativeRuntime(Cfg(**{**SOCIAL, **overrides}))


def discord(user_id="7", name="ジーレン", authoritative=True) -> TransportIdentity:
    return TransportIdentity(provider="discord", transport_user_id=str(user_id),
                             display_name=name, authoritative=authoritative)


def voiceprint(voiceprint_id="3", confidence=.9) -> VoiceIdentity:
    return VoiceIdentity(voiceprint_id=str(voiceprint_id), confidence=confidence,
                         model_version="probe-v1")


# ===========================================================================
# 1. authoritative の回帰確認
# ===========================================================================


def test_an_authoritative_join_reaches_the_world_state():
    live = runtime()
    live.note_participant_joined(transport_key="discord:7", display_name="ジーレン",
                                 channel="general", now=1000.0)
    assert live.world.fact("discord:7", "present_in_conversation").value is True


def test_an_authoritative_leave_is_not_rejected_by_the_margin():
    """**これが Phase 6C で見つけた実バグ。** 二度と戻さない。"""
    live = runtime()
    live.note_participant_joined(transport_key="discord:7", display_name="ジーレン",
                                 now=1000.0)
    live.note_participant_left(transport_key="discord:7", display_name="ジーレン",
                               now=1010.0)
    fact = live.world.fact("discord:7", "present_in_conversation")
    assert fact.value is False
    assert live.world.recent_changes[-1].rejected_reason == ""


def test_a_guess_still_obeys_the_contradiction_margin():
    """**推測は今までどおり。** 印を付けた所だけが変わる。"""
    from neuro_voice.cognition.world import GroundedWorldState, WorldStateGate

    state, gate = GroundedWorldState(), WorldStateGate()
    gate.observe_fact(state, "player", "hp", "low", confidence=.9, now=1000.0)
    delta = gate.observe_fact(state, "player", "hp", "high", confidence=.9, now=1010.0)
    assert state.fact("player", "hp").value == "low"
    assert delta.rejected_reason == "below_contradiction_margin"


def test_only_transport_events_are_authoritative():
    """**正本の印を付けてよい出どころを限る。**

    声が聞こえない・画面から消えた・声紋が下がった——どれも推測。
    """
    from neuro_voice.cognition.observation import (
        from_discord_presence, from_ktane_event, from_speaker, from_vision,
    )

    _e, discord_facts = from_discord_presence(user_id=7, display_name="x",
                                              present=False, now=1000.0)
    assert all(item.authoritative for item in discord_facts)
    _e, ktane_facts = from_ktane_event(event="bomb_ended", session_id="s", now=1000.0)
    assert all(item.authoritative for item in ktane_facts)
    # 声紋は推測。**正本にしない。**
    _e, speaker_facts = from_speaker(speaker_id=3, name="チビ", confirmed=True,
                                     now=1000.0)
    assert not any(item.authoritative for item in speaker_facts)
    # 画面の体力も推測。
    frame = type("V", (), {"scene_type": "minecraft_gameplay", "confidence": .9,
                           "observation_id": "o",
                           "player_state_details": {"health_estimate": "low"}})()
    hp = next(item for item in from_vision(frame, now=1000.0)[1]
              if item.predicate == "hp")
    assert not hp.authoritative


def test_silence_is_not_a_departure():
    """**声が聞こえないだけでは帰ったことにしない。**"""
    registry = PresenceRegistry()
    registry.note_voice(voice_key="voice:1", person_id="person:A", known=True,
                        confidence=.9, now=1000.0)
    registry.sweep(now=1000.0 + 99999.0)
    entry = registry.entries["person:A"]
    assert str(entry.presence_status) == str(PresenceStatus.STALE)
    assert str(entry.presence_status) != str(PresenceStatus.LEFT)


def test_only_the_transport_can_confirm_a_departure():
    registry = PresenceRegistry()
    registry.note_transport(person_id="person:A", transport_identity="discord:1",
                            present=True, now=1000.0)
    registry.note_transport(person_id="person:A", transport_identity="discord:1",
                            present=False, now=1010.0)
    assert str(registry.entries["person:A"].presence_status) == str(PresenceStatus.LEFT)


# ===========================================================================
# 2〜5. 挨拶
# ===========================================================================


def test_greet_on_join_makes_a_candidate_not_a_sentence():
    """**`true` は「挨拶を検討してよい」。** 固定文の即時再生ではない。"""
    live = runtime()
    assert live.note_participant_joined(
        transport_key="discord:7", display_name="ジーレン", now=1000.0) is True
    pending = live.social_opportunities(now=1000.0)
    assert pending and pending[0].proposed_action is ActionType.GREET


def test_the_fixed_sentence_path_is_gone_when_cognitive_is_on():
    """**二重に挨拶しない。** どちらか一方だけ通る。"""
    text = (Path(__file__).resolve().parents[1] / "neuro_voice" / "discord_bridge"
            / "bot.py").read_text(encoding="utf-8")
    start = text.index("async def _greet_voice_member")
    body = text[start:text.index("def _note_greeting_opportunity")]
    assert "self._cognitive_greeting_enabled" in body
    # 認知経路を通る時は、その場で return して固定文へ落ちない。
    marker = body.index("self._cognitive_greeting_enabled")
    assert "return" in body[marker:marker + 200]
    assert "await self._speak" in body, "従来の経路まで消してはいけない"


def test_the_greeting_goes_through_the_speech_gate():
    from neuro_voice.cognition.rollout import (
        PROACTIVE_ALLOWLIST, SpeechRequest, SpeechSource, check_speech,
    )

    assert ActionType.GREET in PROACTIVE_ALLOWLIST
    verdict = check_speech(
        SpeechRequest(source_type=SpeechSource.PROACTIVE_OPPORTUNITY,
                      source_action=ActionType.GREET, confidence=.95),
        cognition_enabled=True, has_valid_decision=True, decision_allows_speech=True)
    assert verdict.allowed


def test_a_greeting_without_a_decision_does_not_speak():
    """**決定が無いまま挨拶しない。**"""
    from neuro_voice.cognition.rollout import SpeechRequest, SpeechSource, check_speech

    verdict = check_speech(
        SpeechRequest(source_type=SpeechSource.PROACTIVE_OPPORTUNITY,
                      source_action=ActionType.GREET, confidence=.95),
        cognition_enabled=True, has_valid_decision=False)
    assert not verdict.allowed


def test_greeting_and_silence_are_compared_side_by_side():
    """**GREET と REMAIN_SILENT を同じ列に並べる。**"""
    from neuro_voice.cognition import CognitiveEvent, CognitiveKernel, EventType, build_state

    live = runtime()
    live.note_participant_joined(transport_key="discord:7", display_name="ジーレン",
                                 now=1000.0)
    result = live.evaluate(SpeakingConditions(), now=1001.0)
    actions = {str(item.proposed_action) for item in result.opportunities}
    assert "greet" in actions or "acknowledge_presence" in actions
    # Action Selector まで通ること。
    decision = CognitiveKernel().decide(
        CognitiveEvent(event_type=EventType.SILENCE_TIMEOUT), build_state(),
        opportunities=result.opportunities, now=1001.0)
    assert decision.selected_action is not None


@pytest.mark.parametrize("conditions,expected", [
    (SpeakingConditions(user_speaking=True), "user_is_speaking"),
    (SpeakingConditions(other_participant_speaking=True),
     "another_participant_is_speaking"),
    (SpeakingConditions(assistant_speaking=True), "already_speaking"),
    (SpeakingConditions(handling_interruption=True), "handling_interruption"),
    (SpeakingConditions(warning_in_progress=True), "warning_in_progress"),
    (SpeakingConditions(focus_mode=True), "user_is_focused"),
])
def test_the_greeting_stays_quiet_when_the_room_is_busy(conditions, expected):
    """**他の人が話している間は黙る**——`REMAIN_SILENT` を選べること。"""
    opportunity = greeting_opportunity(person_id="person:A", known=True,
                                       identity_confidence=.95, now=1000.0)
    assert suppression_reason(opportunity, conditions, now=1000.0) == expected


def test_a_quick_reconnect_does_not_greet_again():
    """回線が切れて戻っただけ。**挨拶を連打しない。**"""
    assert greeting_opportunity(
        person_id="person:A", known=True, identity_confidence=.95,
        seconds_since_last_seen=RECONNECT_WINDOW - 1, now=1000.0) is None
    assert greeting_opportunity(
        person_id="person:A", known=True, identity_confidence=.95,
        seconds_since_last_seen=RECONNECT_WINDOW + 1, now=1000.0) is not None


def test_a_reconnect_still_restores_presence():
    """**挨拶しないことと、居ることを忘れることは別。**"""
    live = runtime()
    live.note_participant_joined(transport_key="discord:7", display_name="ジーレン",
                                 now=1000.0)
    live.note_participant_left(transport_key="discord:7", now=1005.0)
    live.note_participant_joined(transport_key="discord:7", display_name="ジーレン",
                                 now=1010.0)
    assert live.world.fact("discord:7", "present_in_conversation").value is True
    summary = live.presence_summary(now=1010.0)
    assert summary.authoritative_transport_count == 1
    # 2回目の入室では挨拶を作らない。
    assert live.counters["greetings_skipped"] >= 1


def test_an_unknown_person_is_not_greeted_by_name():
    """**誰か分からないまま挨拶しない。**"""
    assert greeting_opportunity(person_id="person:A", known=False,
                                identity_confidence=.3, now=1000.0) is None


def test_a_crowd_is_not_greeted_one_by_one():
    assert greeting_opportunity(
        person_id="person:A", known=True, identity_confidence=.95,
        joined_together=MASS_JOIN_THRESHOLD, now=1000.0) is None


def test_the_same_person_is_not_greeted_twice():
    assert greeting_opportunity(
        person_id="person:A", known=True, identity_confidence=.95,
        greeted_recently_seconds=60.0, now=1000.0) is None


def test_a_familiar_person_gets_a_lighter_reaction():
    """さっきまで話していた人には、**挨拶より軽く。**"""
    opportunity = greeting_opportunity(
        person_id="person:A", known=True, identity_confidence=.95,
        recent_interaction_seconds=30.0, now=1000.0)
    assert opportunity.opportunity_type is OpportunityType.ACKNOWLEDGE_PRESENCE


def test_a_greeting_expires():
    """**入ってから何十秒も経って挨拶するのは変。**"""
    opportunity = greeting_opportunity(person_id="person:A", known=True,
                                       identity_confidence=.95, now=1000.0)
    assert not opportunity.expired(now=1010.0)
    assert opportunity.expired(now=1100.0)


def test_a_crowded_room_makes_greeting_costlier():
    quiet = greeting_opportunity(person_id="person:A", known=True,
                                 identity_confidence=.95, group_size=1, now=1000.0)
    crowded = greeting_opportunity(person_id="person:A", known=True,
                                   identity_confidence=.95, group_size=8, now=1000.0)
    assert crowded.social_cost > quiet.social_cost
    assert crowded.initiative_score < quiet.initiative_score


def test_the_greeting_is_addressed_to_that_person():
    opportunity = greeting_opportunity(person_id="person:A", known=True,
                                       identity_confidence=.95, now=1000.0)
    assert opportunity.target_participant_ids == ("person:A",)
    assert str(opportunity.target_scope) == "direct"


def test_the_greeting_carries_no_text():
    """**本文も表示名も入れない**（第12条）。"""
    import json

    opportunity = greeting_opportunity(person_id="person:A", known=True,
                                       identity_confidence=.95,
                                       channel_context="general", now=1000.0)
    assert opportunity.summary == ""
    assert "ジーレン" not in json.dumps(opportunity.metadata, ensure_ascii=False)


# -- 退出 -------------------------------------------------------------------


def test_leaving_always_updates_presence_even_without_a_farewell():
    live = runtime(**{"discord.farewell_opportunity_enabled": False})
    live.note_participant_joined(transport_key="discord:7", display_name="ジーレン",
                                 now=1000.0)
    assert live.note_participant_left(transport_key="discord:7", now=1010.0) is False
    assert live.world.fact("discord:7", "present_in_conversation").value is False
    entry = next(iter(live.presence().entries.values()))
    assert str(entry.presence_status) == str(PresenceStatus.LEFT)


def test_a_farewell_needs_an_actual_conversation():
    assert farewell_opportunity(person_id="person:A", known=True,
                                was_talking_with=False, now=1000.0) is None
    assert farewell_opportunity(person_id="person:A", known=True,
                                was_talking_with=True, identity_confidence=.9,
                                now=1000.0) is not None


def test_an_abrupt_disconnect_gets_no_farewell():
    """**もう聞いていない相手へ喋っても届かない。**"""
    assert farewell_opportunity(person_id="person:A", known=True,
                                was_talking_with=True, abrupt=True,
                                now=1000.0) is None


def test_a_farewell_does_not_linger():
    """切断のずっと後に、宛先不在の音声を出さない。"""
    opportunity = farewell_opportunity(person_id="person:A", known=True,
                                       was_talking_with=True,
                                       identity_confidence=.9, now=1000.0)
    assert opportunity.expired(now=1100.0)


def test_the_same_departure_is_handled_once():
    assert farewell_opportunity(
        person_id="person:A", known=True, was_talking_with=True,
        identity_confidence=.9, source_event_id="e1",
        handled_event_ids=("e1",), now=1000.0) is None


# ===========================================================================
# 6〜10. Identity
# ===========================================================================


def test_the_three_identities_are_separate_types():
    transport, voice = discord(), voiceprint()
    assert transport.key == "discord:7"
    assert voice.key == "voice:3"
    assert transport.key != voice.key


def test_an_authoritative_transport_resolves_immediately():
    resolver = IdentityResolver()
    verdict = resolver.resolve(transport=discord(), event_id="e1")
    assert str(verdict.resolution_status) == str(ResolutionStatus.AUTHORITATIVE)
    assert verdict.known and verdict.person_id


def test_the_same_transport_returns_the_same_person():
    resolver = IdentityResolver()
    first = resolver.resolve(transport=discord(), event_id="e1")
    second = resolver.resolve(transport=discord(name="Zilen"), event_id="e2")
    assert first.person_id == second.person_id, "表示名だけで人物が分かれている"


def test_a_single_voice_match_does_not_confirm_a_person():
    """**一度の声紋一致で恒久統合しない。**"""
    resolver = IdentityResolver()
    resolver.link(person_id="person:A", identity_type=IdentityType.VOICE,
                  value="voice:3", confidence=.9, event_id="e1")
    verdict = resolver.resolve(voice=voiceprint())
    assert str(verdict.resolution_status) == str(ResolutionStatus.PROBABLE)
    assert not verdict.known, "候補どまりを既知として扱っている"


def test_repeated_matches_confirm_the_link():
    resolver = IdentityResolver()
    for index in range(CONFIRM_SUPPORT):
        link = resolver.link(person_id="person:A", identity_type=IdentityType.VOICE,
                             value="voice:3", confidence=.9, event_id=f"e{index}")
    assert str(link.status) == str(LinkStatus.CONFIRMED)
    assert resolver.resolve(voice=voiceprint()).known


def test_a_weak_match_does_not_count_as_support():
    """弱い一致を根拠に積むと、確認済みが意味を失う。"""
    resolver = IdentityResolver()
    for index in range(10):
        resolver.note_voice_sample(transport=discord(), voice=voiceprint(confidence=.4),
                                   event_id=f"e{index}")
    assert not any(str(item.identity_type) == "voice" for item in resolver.links.values())


def test_a_low_confidence_voice_does_not_touch_a_known_person():
    """**不明話者が既知の人物の関係値を動かさない。**"""
    resolver = IdentityResolver()
    resolver.resolve(transport=discord(), event_id="e1")
    verdict = resolver.resolve(voice=voiceprint(voiceprint_id="99", confidence=.2))
    assert str(verdict.resolution_status) == str(ResolutionStatus.UNKNOWN)
    assert not verdict.person_id


def test_the_transport_wins_when_the_voice_disagrees():
    """**Discord ID を優先する。** 声紋の方が強く出ていても。"""
    resolver = IdentityResolver()
    for index in range(CONFIRM_SUPPORT):
        resolver.link(person_id="person:B", identity_type=IdentityType.VOICE,
                      value="voice:3", confidence=.95, event_id=f"b{index}")
    verdict = resolver.resolve(transport=discord(), voice=voiceprint(), event_id="c1")
    assert verdict.person_id != "person:B"
    assert verdict.conflict_reason == "voice_disagrees_with_transport"


def test_the_conflicting_voice_link_is_marked_not_moved():
    """**A と B の関係を混ぜない。** リンクの持ち主も変えない。"""
    resolver = IdentityResolver()
    for index in range(CONFIRM_SUPPORT):
        resolver.link(person_id="person:B", identity_type=IdentityType.VOICE,
                      value="voice:3", confidence=.95, event_id=f"b{index}")
    resolver.resolve(transport=discord(), voice=voiceprint(), event_id="c1")
    link = next(item for item in resolver.links.values()
                if item.identity_value == "voice:3")
    assert str(link.status) == str(LinkStatus.CONFLICTED)
    assert link.person_id == "person:B", "食い違いだけで人物を付け替えている"


def test_a_conflicted_link_is_not_used_as_evidence():
    resolver = IdentityResolver()
    for index in range(CONFIRM_SUPPORT):
        resolver.link(person_id="person:B", identity_type=IdentityType.VOICE,
                      value="voice:3", confidence=.95, event_id=f"b{index}")
    resolver.resolve(transport=discord(), voice=voiceprint(), event_id="c1")
    verdict = resolver.resolve(voice=voiceprint())
    assert str(verdict.resolution_status) == str(ResolutionStatus.CONFLICTED)
    assert not verdict.known


def test_a_direct_audio_sample_builds_the_voice_link():
    """**接続が正本と分かっている音声**だけを根拠にする。"""
    resolver = IdentityResolver()
    for index in range(CONFIRM_SUPPORT):
        link = resolver.note_voice_sample(transport=discord(), voice=voiceprint(),
                                          event_id=f"d{index}")
    assert link is not None
    assert str(link.status) == str(LinkStatus.CONFIRMED)
    assert link.model_version == "probe-v1", "モデルの版が残っていない"
    assert link.source_event_ids, "根拠のイベントIDが残っていない"


def test_a_guessed_transport_is_not_a_valid_source():
    resolver = IdentityResolver()
    assert resolver.note_voice_sample(
        transport=discord(authoritative=False), voice=voiceprint()) is None


def test_display_names_never_merge_people():
    resolver = IdentityResolver()
    first = resolver.resolve(transport=discord(user_id="7", name="ジーレン"))
    second = resolver.resolve(transport=discord(user_id="8", name="ジーレン"))
    assert first.person_id != second.person_id


# -- 取り消しと繋ぎ直し -----------------------------------------------------


def test_a_link_can_be_revoked_without_deleting_it():
    resolver = IdentityResolver()
    link = resolver.link(person_id="person:A", identity_type=IdentityType.VOICE,
                         value="voice:3", confidence=.9, event_id="e1")
    resolver.revoke(link.link_id, reason="別人だった")
    assert str(link.status) == str(LinkStatus.REVOKED)
    assert link.link_id in resolver.links, "物理削除している"
    assert link.revoked_reason == "別人だった"


def test_a_revoked_link_is_no_longer_used():
    resolver = IdentityResolver()
    for index in range(CONFIRM_SUPPORT):
        link = resolver.link(person_id="person:A", identity_type=IdentityType.VOICE,
                             value="voice:3", confidence=.9, event_id=f"e{index}")
    resolver.revoke(link.link_id, reason="別人")
    assert not resolver.resolve(voice=voiceprint()).known


def test_relinking_keeps_the_old_record():
    resolver = IdentityResolver()
    old = resolver.link(person_id="person:A", identity_type=IdentityType.VOICE,
                        value="voice:3", confidence=.9, event_id="e1")
    fresh = resolver.relink(old.link_id, person_id="person:B", reason="取り違え")
    assert fresh.person_id == "person:B"
    assert str(old.status) == str(LinkStatus.SUPERSEDED)
    assert old.superseded_by == fresh.link_id
    assert fresh.source_event_ids == old.source_event_ids, "出典が失われている"


def test_the_history_of_a_mistake_is_traceable():
    resolver = IdentityResolver()
    old = resolver.link(person_id="person:A", identity_type=IdentityType.VOICE,
                        value="voice:3", confidence=.9, event_id="e1")
    resolver.relink(old.link_id, person_id="person:B", reason="取り違え")
    history = resolver.history_for("voice:3")
    assert len(history) == 2
    assert [str(item.status) for item in history] == [
        str(LinkStatus.SUPERSEDED), str(LinkStatus.CANDIDATE)]


def test_a_relinked_person_starts_uncertain_again():
    """**繋ぎ直した先も、また積み直す。** 確信を引き継がない。"""
    resolver = IdentityResolver()
    for index in range(CONFIRM_SUPPORT):
        old = resolver.link(person_id="person:A", identity_type=IdentityType.VOICE,
                            value="voice:3", confidence=.9, event_id=f"e{index}")
    fresh = resolver.relink(old.link_id, person_id="person:B")
    assert str(fresh.status) == str(LinkStatus.CANDIDATE)
    assert not resolver.resolve(voice=voiceprint()).known


def test_the_runtime_exposes_the_undo():
    live = runtime()
    resolver = live.resolver()
    link = resolver.link(person_id="person:A", identity_type=IdentityType.VOICE,
                         value="voice:3", confidence=.9, event_id="e1")
    assert live.revoke_identity_link(link.link_id, reason="誤り") is not None
    assert str(link.status) == str(LinkStatus.REVOKED)


def test_no_bulk_move_of_the_past():
    """**過去の会話や記憶を自動で大量移動しない。**"""
    import inspect

    from neuro_voice.cognition import identity as module

    source = inspect.getsource(module.IdentityResolver.relink)
    for banned in ("memories", "transcript", "episodes", "relationship"):
        assert banned not in source, banned


# ===========================================================================
# 11〜12. Presence
# ===========================================================================


def test_the_two_counts_are_kept_apart():
    registry = PresenceRegistry()
    registry.note_transport(person_id="person:A", transport_identity="discord:1",
                            present=True, now=1000.0)
    registry.note_voice(voice_key="voice:x", known=False, now=1001.0)
    summary = registry.summary(now=1001.0)
    assert summary.authoritative_transport_count == 1
    assert summary.estimated_unique_person_count > 1
    assert summary.unknown_voice_count == 1


def test_an_estimate_is_never_stated_as_fact():
    """**推定人数を確定人数として扱わない。**"""
    registry = PresenceRegistry()
    registry.note_voice(voice_key="voice:x", known=False, now=1000.0)
    summary = registry.summary(now=1000.0)
    assert not summary.certain
    assert summary.authoritative_transport_count == 0


def test_an_unknown_voice_makes_even_a_known_room_uncertain():
    """一覧があっても、**知らない声が混じれば言い切れない。**"""
    registry = PresenceRegistry()
    registry.note_transport(person_id="person:A", transport_identity="discord:1",
                            present=True, now=1000.0)
    assert registry.summary(now=1000.0).certain
    registry.note_voice(voice_key="voice:x", known=False, now=1001.0)
    assert not registry.summary(now=1001.0).certain


def test_a_new_voice_raises_the_estimate():
    registry = PresenceRegistry()
    before = registry.summary(now=1000.0).estimated_unique_person_count
    registry.note_voice(voice_key="voice:1", known=False, now=1000.0)
    registry.note_voice(voice_key="voice:2", known=False, now=1001.0)
    assert registry.summary(now=1001.0).estimated_unique_person_count > before


def test_an_unknown_voice_does_not_become_a_known_person():
    """**同じマイクから別人の声が入っても、本人の関係値を動かさない。**"""
    live = runtime()
    entry = live.note_local_voice(voice_key="voice:9", known=False,
                                  confidence=.3, now=1000.0)
    assert entry is None
    assert live.presence().summary(now=1000.0).unknown_voice_count == 1
    assert not live.presence().present_ids()


def test_a_known_voice_counts_as_that_person():
    live = runtime()
    entry = live.note_local_voice(voice_key="voice:1", person_id="person:A",
                                  known=True, confidence=.9, now=1000.0)
    assert entry is not None
    assert live.presence().present_ids() == ("person:A",)


def test_a_voice_does_not_downgrade_a_transport_entry():
    """接続が「居る」と言っているのを、声で格下げしない。"""
    registry = PresenceRegistry()
    registry.note_transport(person_id="person:A", transport_identity="discord:1",
                            present=True, now=1000.0)
    registry.note_voice(voice_key="voice:1", person_id="person:A", known=True,
                        confidence=.6, now=1001.0)
    entry = registry.entries["person:A"]
    assert str(entry.presence_source) == str(PresenceSource.TRANSPORT)
    assert entry.certain


def test_unknown_voices_fade():
    registry = PresenceRegistry()
    registry.note_voice(voice_key="voice:x", known=False, now=1000.0)
    registry.sweep(now=1000.0 + 99999.0)
    assert registry.summary(now=1000.0 + 99999.0).unknown_voice_count == 0


# ===========================================================================
# 13. World State との接続
# ===========================================================================


def test_the_person_id_is_what_the_world_state_holds():
    live = runtime()
    live.note_participant_joined(transport_key="discord:7", display_name="ジーレン",
                                 now=1000.0)
    person_ids = live.presence().present_ids()
    assert person_ids and person_ids[0].startswith("person:")


def test_relationships_are_not_keyed_by_voiceprint():
    """**声紋IDを関係の主キーにしない。**"""
    import inspect

    from neuro_voice.cognition import identity as module

    assert "person_id" in inspect.signature(
        module.IdentityResolver.link).parameters


# ===========================================================================
# 14. 映像Fact
# ===========================================================================


def frame(health="low", confidence=.9, scene="minecraft_gameplay"):
    return type("V", (), {
        "scene_type": scene, "confidence": confidence, "observation_id": "obs1",
        "player_state_details": {"health_estimate": health}})()


def test_the_health_estimate_becomes_a_fact():
    live = runtime()
    live.observe_vision(frame(), now=1000.0)
    assert live.world.fact("player", "hp").value == "low"


@pytest.mark.parametrize("said,expected", [
    ("low", "low"), ("critical", "low"), ("残りわずか、危険", "low"),
    ("half", "medium"), ("だいたい半分", "medium"),
    ("full", "high"), ("満タン", "high"),
    ("紫色", ""), ("", ""),
])
def test_the_health_wording_is_normalised(said, expected):
    from neuro_voice.cognition.observation import normalise_health

    assert normalise_health(said) == expected


def test_a_faint_health_reading_is_not_asserted():
    live = runtime()
    live.observe_vision(frame(confidence=.65), now=1000.0)
    assert live.world.fact("player", "hp") is None


def test_the_health_is_not_read_from_a_menu():
    """メニュー画面の体力は、**いまの状況ではない。**"""
    live = runtime()
    live.observe_vision(frame(scene="menu"), now=1000.0)
    assert live.world.fact("player", "hp") is None


def test_the_health_goes_stale_quickly():
    live = runtime()
    live.observe_vision(frame(), now=1000.0)
    assert live.world.fact("player", "hp").usable(now=1002.0)
    assert not live.world.fact("player", "hp").usable(now=1100.0)


def test_the_health_keeps_its_source_event():
    live = runtime()
    live.observe_vision(frame(), now=1000.0)
    assert live.world.fact("player", "hp").source_event_ids == ("obs1",)


def test_only_a_change_updates_the_health():
    live = runtime()
    for index in range(20):
        live.observe_vision(frame(), now=1000.0 + index * 10.0)
    assert len([key for key in live.world.facts if key.startswith("player:")]) == 1


def test_the_extra_fact_needs_its_flag():
    live = runtime(**{"world_state.extended_vision_fact_enabled": False})
    live.observe_vision(frame(), now=1000.0)
    assert live.world.fact("player", "hp") is None
    # 画面の種類は今までどおり入る。
    from neuro_voice.cognition.observation import ACTIVE_SCREEN

    assert live.world.fact(ACTIVE_SCREEN, "scene") is not None


# ===========================================================================
# 15. 再起動
# ===========================================================================


def test_identity_links_survive_a_restart(tmp_path):
    store = store_at(tmp_path)
    first = runtime()
    first.attach_store(store)
    resolver = first.resolver()
    for index in range(CONFIRM_SUPPORT):
        link = resolver.note_voice_sample(transport=discord(), voice=voiceprint(),
                                          event_id=f"d{index}")
    assert first.flush_identity()["persisted"]
    link_id, person_id = link.link_id, link.person_id
    store.close()

    reopened = store_at(tmp_path)
    second = runtime()
    second.attach_store(reopened)
    assert second.hydrate_identity()["hydrated"]
    restored = second.resolver().links[link_id]
    assert restored.person_id == person_id
    assert str(restored.status) == str(LinkStatus.CONFIRMED)
    assert second.resolver().resolve(voice=voiceprint()).known
    reopened.close()


def test_a_revoked_link_stays_revoked_after_a_restart(tmp_path):
    store = store_at(tmp_path)
    first = runtime()
    first.attach_store(store)
    link = first.resolver().link(person_id="person:A", identity_type=IdentityType.VOICE,
                                 value="voice:3", confidence=.9, event_id="e1")
    first.revoke_identity_link(link.link_id, reason="別人だった")
    first.flush_identity()
    store.close()

    reopened = store_at(tmp_path)
    second = runtime()
    second.attach_store(reopened)
    second.hydrate_identity()
    restored = second.resolver().links[link.link_id]
    assert str(restored.status) == str(LinkStatus.REVOKED)
    assert restored.revoked_reason == "別人だった"
    reopened.close()


def test_presence_is_not_current_after_a_restart(tmp_path):
    """**再起動しただけで「居る」と言わない。**"""
    registry = PresenceRegistry()
    registry.restore([{"person_id": "person:A", "transport_identity": "discord:1"}])
    entry = registry.entries["person:A"]
    assert str(entry.presence_status) == str(PresenceStatus.UNKNOWN)
    assert str(entry.presence_source) == str(PresenceSource.RESTORED)
    assert registry.summary(now=1000.0).authoritative_transport_count == 0


def test_presence_returns_after_reconnecting():
    registry = PresenceRegistry()
    registry.restore([{"person_id": "person:A", "transport_identity": "discord:1"}])
    registry.note_transport(person_id="person:A", transport_identity="discord:1",
                            present=True, now=1000.0)
    assert registry.summary(now=1000.0).authoritative_transport_count == 1


def test_a_failed_identity_write_does_not_stop_the_conversation():
    class Broken:
        def save_identity_links(self, people, links):
            raise RuntimeError("disk full")

    live = runtime()
    live.attach_store(Broken())
    live.resolver().link(person_id="person:A", identity_type=IdentityType.VOICE,
                         value="voice:3", confidence=.9)
    result = live.flush_identity()
    assert result["persisted"] is False
    assert "disk full" in result["reason"]


# ===========================================================================
# 16. Trace
# ===========================================================================


def test_the_trace_has_the_new_fields():
    from neuro_voice.cognition.trace import CognitiveTrace

    trace = CognitiveTrace()
    for name in ("presence_event_type", "presence_authoritative",
                 "presence_update_result", "greeting_opportunity_id",
                 "greeting_action_selected", "greeting_suppression_reason",
                 "transport_identity", "voice_identity", "resolved_person_id",
                 "identity_resolution_status", "identity_link_id",
                 "identity_conflict", "identity_link_status_change",
                 "authoritative_participant_count", "estimated_participant_count",
                 "vision_fact_id", "vision_fact_ttl", "speech_gate_result"):
        assert hasattr(trace, name), name


def test_the_trace_records_the_resolution():
    from neuro_voice.cognition.trace import CognitiveTrace

    resolver = IdentityResolver()
    verdict = resolver.resolve(transport=discord(), voice=voiceprint(), event_id="e1")
    trace = CognitiveTrace()
    trace.note_identity(verdict)
    assert trace.transport_identity == "discord:7"
    assert trace.voice_identity == "voice:3"
    assert trace.identity_resolution_status == "authoritative"
    assert trace.resolved_person_id


def test_the_trace_keeps_the_two_counts_apart():
    from neuro_voice.cognition.trace import CognitiveTrace

    registry = PresenceRegistry()
    registry.note_transport(person_id="person:A", transport_identity="discord:1",
                            present=True, now=1000.0)
    registry.note_voice(voice_key="voice:x", known=False, now=1000.0)
    trace = CognitiveTrace()
    trace.note_presence(registry.summary(now=1000.0))
    assert trace.authoritative_participant_count == 1
    assert trace.estimated_participant_count > 1


def test_the_trace_holds_no_audio_or_voiceprint():
    """**音声データも声紋ベクトルも通常ログへ出さない。**"""
    import json

    from neuro_voice.cognition.trace import CognitiveTrace

    resolver = IdentityResolver()
    trace = CognitiveTrace()
    trace.note_identity(resolver.resolve(transport=discord(), voice=voiceprint()))
    text = json.dumps(trace.snapshot(), ensure_ascii=False)
    assert "ジーレン" not in text
    for banned in ("vec", "embedding", "waveform", "pcm", "audio"):
        assert banned not in text.lower()


def test_the_identity_status_holds_no_names():
    import json

    resolver = IdentityResolver()
    resolver.resolve(transport=discord(), event_id="e1")
    assert "ジーレン" not in json.dumps(resolver.status(), ensure_ascii=False)


# ===========================================================================
# 17. 機能フラグ
# ===========================================================================


def test_the_new_flags_default_to_off():
    live = InitiativeRuntime(Cfg())
    for flag in ("identity_resolution_enabled", "voice_transport_linking_enabled",
                 "presence_registry_enabled", "local_estimation_enabled",
                 "cognitive_greeting_enabled", "farewell_enabled",
                 "extended_vision_fact_enabled"):
        assert not getattr(live, flag), flag
    # **取り消せることは既定 true。** 取り消せない統合を作る理由が無い。
    assert live.reversible_links_enabled


def test_the_config_keeps_greet_on_join_true():
    """**勝手に false へ変えない。** 意味を変えただけ。"""
    import yaml

    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "config"
                           / "config.yaml").read_text(encoding="utf-8"))
    assert data["discord"]["greet_on_join"] is True
    assert data["discord"]["cognitive_greeting_enabled"] is False
    assert data["discord"]["farewell_opportunity_enabled"] is False
    assert data["identity"]["resolution_enabled"] is False
    assert data["identity"]["reversible_links_enabled"] is True
    assert data["presence"]["registry_enabled"] is False
    assert data["world_state"]["extended_vision_fact_enabled"] is False


def test_identity_resolution_is_inert_while_disabled():
    live = runtime(**{"identity.resolution_enabled": False})
    verdict = live.resolve_identity(transport=discord())
    assert not verdict.person_id


def test_the_greeting_is_not_offered_while_disabled():
    live = runtime(**{"discord.cognitive_greeting_enabled": False})
    assert live.note_participant_joined(transport_key="discord:7",
                                        display_name="x", now=1000.0) is False
    assert not live.social_opportunities(now=1000.0)


# ===========================================================================
# 18. 実経路
# ===========================================================================


def bot_source() -> str:
    return (Path(__file__).resolve().parents[1] / "neuro_voice" / "discord_bridge"
            / "bot.py").read_text(encoding="utf-8")


def test_the_bot_wires_every_new_path():
    text = bot_source()
    for needle in ("self._note_greeting_opportunity(member)",
                   "self._note_farewell_opportunity(member)",
                   "self._note_direct_voice_identity(user, prof)",
                   "note_participant_joined(", "note_participant_left(",
                   "note_voice_sample("):
        assert needle in text, needle


def test_the_direct_audio_path_uses_the_discord_id():
    """**接続が正本。** 声紋だけで人物を決めない。"""
    text = bot_source()
    start = text.index("def _note_direct_voice_identity")
    body = text[start:start + 2000]
    assert "authoritative=True" in body
    assert "TransportIdentity" in body and "VoiceIdentity" in body


def test_the_join_marker_is_set_before_the_member_list_refresh():
    """同時入室を見分けるため、**人数を更新する前に**印を付ける。"""
    text = bot_source()
    start = text.index("async def on_voice_state_update")
    body = text[start:start + 1400]
    assert body.index("self._join_times[int(member.id)]") < body.index(
        "self._refresh_voice_members(current_channel)")


# ===========================================================================
# 19. 配線診断
# ===========================================================================


NEW_PROBES = ("identity.layers", "identity.conflict", "identity.reversible",
              "presence.counts", "presence.restart", "greeting.cognitive",
              "greeting.suppression", "greeting.farewell", "world.vision_health")


def yaml_config():
    import yaml

    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "config"
                           / "config.yaml").read_text(encoding="utf-8"))

    class YamlCfg:
        def get(self, key, default=None):
            current = data
            for part in str(key).split("."):
                if not isinstance(current, dict):
                    return default
                current = current.get(part)
                if current is None:
                    return default
            return current

    return YamlCfg()


def test_the_new_probes_are_registered():
    from neuro_voice.diagnostics.wiring import run_all

    keys = {item.probe.key for item in run_all(yaml_config()).results}
    for key in NEW_PROBES:
        assert key in keys, key


def test_nothing_needs_attention():
    """**要注意 0 件を保つ。**"""
    from neuro_voice.diagnostics.wiring import run_all

    report = run_all(yaml_config())
    assert report.snapshot()["attention"] == [], report.snapshot()["attention"]


def test_a_one_shot_confirmation_turns_the_probe_red(monkeypatch):
    """**方針そのものを見ている**こと。往復だけでは1回確定を見逃す。"""
    import neuro_voice.cognition.identity as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    monkeypatch.setattr(module, "CONFIRM_SUPPORT", 1)
    report = run_all(yaml_config(), keys=("identity.layers",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_a_hard_delete_turns_the_probe_red(monkeypatch):
    import neuro_voice.cognition.identity as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    original = module.IdentityResolver.revoke

    def deleting(self, link_id, *, reason=""):
        result = original(self, link_id, reason=reason)
        self.links.pop(link_id, None)
        return result

    monkeypatch.setattr(module.IdentityResolver, "revoke", deleting)
    report = run_all(yaml_config(), keys=("identity.reversible",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_a_silent_departure_turns_the_probe_red(monkeypatch):
    import neuro_voice.cognition.presence as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    original = module.PresenceRegistry.sweep

    def eager(self, *, now=None, quiet=module.QUIET_BEFORE_STALE):
        marked = original(self, now=now, quiet=quiet)
        for person_id in marked:
            self.entries[person_id].presence_status = module.PresenceStatus.LEFT
        return marked

    monkeypatch.setattr(module.PresenceRegistry, "sweep", eager)
    report = run_all(yaml_config(), keys=("presence.counts",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_a_missing_suppression_turns_the_probe_red(monkeypatch):
    import neuro_voice.cognition.initiative as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    original = module.suppression_reason

    def lax(opportunity, conditions, **kwargs):
        if conditions.warning_in_progress:
            return ""
        return original(opportunity, conditions, **kwargs)

    monkeypatch.setattr(module, "suppression_reason", lax)
    report = run_all(yaml_config(), keys=("greeting.suppression",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_a_greeting_for_strangers_turns_the_probe_red(monkeypatch):
    import neuro_voice.cognition.initiative as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    original = module.greeting_opportunity
    monkeypatch.setattr(
        module, "greeting_opportunity",
        lambda **kwargs: original(**{**kwargs, "known": True}))
    report = run_all(yaml_config(), keys=("greeting.cognitive",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_a_guessed_health_reading_turns_the_probe_red(monkeypatch):
    """知らない言い方を「たぶん少ない」で通すと赤くなること。"""
    import neuro_voice.cognition.observation as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    monkeypatch.setattr(module, "normalise_health", lambda value: "low")
    report = run_all(yaml_config(), keys=("world.vision_health",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_a_menu_health_reading_turns_the_probe_red(monkeypatch):
    """**メニュー画面の体力を今の状況にしない。** 外すと赤くなる。"""
    import neuro_voice.cognition.observation as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    original = module.from_vision

    def ignoring_scene(observation, *, now=None):
        entities, facts = original(observation, now=now)
        if not any(item.predicate == "hp" for item in facts):
            details = getattr(observation, "player_state_details", {}) or {}
            level = module.normalise_health(details.get("health_estimate"))
            if level:
                facts = [*facts, module.FactObservation(
                    subject_id="player", predicate="hp", value=level,
                    source_type="vision", confidence=.9, ttl=module.VISION_HP_TTL)]
        return entities, facts

    monkeypatch.setattr(module, "from_vision", ignoring_scene)
    report = run_all(yaml_config(), keys=("world.vision_health",))
    # 場面も確信も、どちらの門番が外れても赤くなる。
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_the_new_flags_are_watched():
    from neuro_voice.diagnostics.wiring import run_all

    flags = run_all(yaml_config()).flags
    for key in ("identity.resolution_enabled",
                "identity.voice_transport_linking_enabled",
                "identity.reversible_links_enabled",
                "presence.registry_enabled", "presence.local_estimation_enabled",
                "discord.greet_on_join", "discord.cognitive_greeting_enabled",
                "discord.farewell_opportunity_enabled",
                "world_state.extended_vision_fact_enabled"):
        assert key in flags, key


def test_the_probes_do_not_touch_real_state():
    from neuro_voice.diagnostics.wiring import run_all

    live = runtime()
    live.note_participant_joined(transport_key="discord:7", display_name="x",
                                 now=1000.0)
    before = (len(live.world.entities), len(live.resolver().links),
              len(live.presence().entries))
    run_all(yaml_config())
    assert (len(live.world.entities), len(live.resolver().links),
            len(live.presence().entries)) == before
