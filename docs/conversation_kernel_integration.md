# Conversation Kernel integration

## Authority boundary

`neuro_voice.dialogue.kernel.ConversationKernel` is the single response-decision
authority shared by local microphone and Discord turns. It is a read model: the
memory store, privacy manager, Activity ledger, search service, LLM, TTS, and
playback tracker remain authoritative for their own data.

Do not introduce another response classifier after the kernel. A legitimate
later transition must be recorded through the kernel with a reason. An
unreasoned change to a protected decision field is rejected and logged as
`KERNEL_DECISION_OVERRIDDEN`.

## Turn lifecycle

1. STT finalizes one human utterance and creates an `utterance_id`.
2. The response path creates a `response_id`.
3. `Mind.begin_conversation_turn()` builds exactly one `TurnFrame` before any
   Web search can execute. The frame creates its own `turn_id` and
   `decision_id`.
4. Bounded memory recall enriches that same frame. It does not create a second
   frame for the same response.
5. A real Web retrieval calls `mark_kernel_search_execution()`. A denied or
   unused search remains `executed=false`.
6. LLM generation records `generation_id`; direct validated responses use the
   response ID as their generation ID.
7. The same `response_id` is already used by the segmenter, TTS queue,
   `PlayedTextTracker`, and playback cancellation checks.
8. The final visible reply is summarized in the Decision Trace. Raw user input
   and memory text are removed from the public status snapshot.

## Decision priority

The priority order is:

1. explicit correction/repair;
2. conversation reference resolution;
3. direct question;
4. canonical Activity continuation;
5. emotion acknowledgement;
6. ordinary response.

An ambiguous or missing conversation referent requires one concrete
clarification question and blocks Web search. An explicit search request can
search, except while a higher-priority repair, unresolved reference, privacy
boundary, or canonical Activity rule blocks it.

## Memory influence

Recall selection and spoken mention are separate. Decision Trace records
selected memory IDs plus rejected IDs and rejection reasons. A current user
correction/negation can reject a conflicting older memory with
`CURRENT_INPUT_CONTRADICTION`; group privacy filtering records
`PRIVACY_BOUNDARY`. Memory changes the present reasoning but does not authorize
the assistant to quote private context.

## Activity integrity

The Activity event ledger remains canonical. Snapshot JSON is only a cache and
is rebuilt from accepted events. A conflicting ledger enters `NEEDS_REPAIR`;
normal moves cannot commit until an explicit repair or control action resolves
it. Completed activities expose no current actor or required next kana.

## Diagnostics

The heart/status view exposes safe metadata only: turn/decision/response IDs,
source, update time, obligation, decision reason, reference status, search
decision and execution state, selected/rejected memory counts, Activity version,
actor, and stale health. It does not expose the raw utterance or memory text.

## Tests

Runtime dependencies are in `requirements.txt`; developer-only test dependencies
are in `requirements-dev.txt`.

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
python -m pytest
```

`pytest.ini` limits collection to this application's `tests/` directory.
Hardware/model tests bundled inside third-party source trees (for example the
vendored Style-Bert-VITS2 project) belong to those projects and are not part of
the AItuber regression suite.

The standard-library suite remains runnable with:

```powershell
python -m unittest discover -s tests
```

`dialogue/shiritori.py` is a compatibility helper used by historical unit tests.
Runtime Activity truth is owned only by `ActivityStateManager`.
