# Phase 7D: Obligation-derived retrieval gate

## Scope and safety

- Local workspace is authoritative: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`.
- No Memory DB, persona, config, migration, deletion, restoration, or historical Trace was changed.
- Playback delivery was accepted before this work: the fresh cognition trace `c966f56ef6c7` has one logical playback session, four unique streaming segments, zero replays, no overlap, and `duplicate_stage=none`.

## Read-only audit of the reported IDs

The historical `2足す2は？` Trace actually selected IDs `203`, `205`, and `204`; it did **not** select `23` or `132`.

| Memory ID | Corresponding obligation / goal | Safe metadata | Trigger / relevance for `2足す2は？` |
|---|---|---|---|
| 203 | No durable obligation or goal record. | `kind=promise`, `status=active`, `persona_id=neuro`, `scope=persona_private`, two stored topic IDs, `source_event_ids=[]`, `created_at=2026-08-03T16:36:40.179969+00:00`; no persistent priority or `due_at`. | Historical trigger was `unresolved_obligation`; recorded score `0.58` was the old promise-plus-unresolved boost, not utterance relevance. Direct lexical/semantic similarity was `0.0`. |
| 23 | No durable obligation or goal record. | `kind=fact`, `status=active`, `persona_id=neuro`, `scope=persona_private`, no topic IDs, `source_event_ids=[]`, `created_at=2026-07-11T15:26:32.344032+00:00`; no priority or `due_at`. | Not selected by that turn; no Trace relevance score. Direct similarity was `0.0`. |
| 132 | No durable obligation or goal record. | `kind=fact`, `status=active`, `persona_id=neuro`, `scope=persona_private`, no topic IDs, `source_event_ids=[]`, `created_at=2026-07-12T16:35:08.587740+00:00`; no priority or `due_at`. | Not selected by that turn; no Trace relevance score. Direct similarity was `0.0`. |

No Memory body or summary was copied into this handoff. The immediate fault was not a durable obligation: `Mind.cognitive_state()` appended the current `ConversationKernel` frame's response/answer duty to `unresolved_obligations`. This made every ordinary cognitive turn look like an obligation-resumption candidate.

## Implemented route

1. `Mind.cognitive_state()` now builds obligation contexts only from active WorkingMemory open questions, with persona ownership and safe identifiers/metadata. It no longer incorporates current-turn ConversationKernel duties.
2. `obligation_retrieval_gate()` in `neuro_voice/cognition/recall.py` permits `unresolved_obligation` only when an active same-persona context exists and one condition holds:
   - explicit past/resumption wording;
   - deterministic topic/entity overlap;
   - the selected action is `CONTINUE_PREVIOUS_TOPIC` or `RESUME_OBLIGATION`.
   Resolved/cancelled/expired and other-persona contexts are rejected.
3. The action kernel may only create a continuation candidate through that same gate (except its pre-existing ending-signal diagnostic path). Generic math/cat questions no longer search simply because an open item exists.
4. Due open items produce a privacy-safe `OBLIGATION_DUE` attention event, consumed by Initiative as `RESUME_OBLIGATION`; a due item is not silently sent as recall context for an unrelated ANSWER.
5. Trace has the following privacy-safe fields: `obligation_retrieval_considered`, `obligation_retrieval_triggered`, `obligation_retrieval_reason`, `obligation_candidate_count`, `obligation_relevance_score`, and `obligation_effect_on_action`.
6. A turn with no retrieval trigger now emits `memory_retrieval_trigger=not_applicable` and `memory_trigger_result=NOT_APPLICABLE` rather than an empty string.

## Tests

Added `tests/test_obligation_retrieval_gate.py` covering:

- open obligation plus math/cat: no search and normal ANSWER;
- related topic and explicit resumption: retrieval gate opens;
- resolved/cancelled/other-persona: rejected;
- selected continuation action: allowed;
- due item: Initiative opportunity only, not blank-query recall;
- Trace gate diagnostics.

Final test run:

```text
2925 passed, 1 skipped in 98.47s
```

The only skip is `tests/test_tool_dialogue.py:580`, where Windows lacks symlink privilege.

## Required next hardware check

Do not run the 30-turn measurement yet. Fully restart the local app, enable the UI cognition test session, then speak **only**:

```text
5足す3は？
```

After speech completes, inspect the fresh Trace. Required values:

```text
cognition_enabled=true
selected_action=ANSWER
memory_trigger_result=NOT_APPLICABLE
retrieved_memory_count=0
obligation_retrieval_triggered=false
speech_request_count=1
logical_playback_session_count=1
replayed_segment_count=0
duplicate_stage=none
```

Do not delete, resolve, or otherwise modify unresolved obligations. If this one turn passes, cognition warm-up is formally complete and the final 30-turn measurement may begin.
