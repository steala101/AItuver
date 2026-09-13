# Phase 8 closure action eligibility repair

## Scope

- Local PROD SESSION only. Tool and Discord acceptance remain blocked.
- No Memory DB, persona, migration, persistent configuration, or historical Trace was changed.

## Read-only diagnosis

- Failed turn: `3f4fdb0a67ce` at 2026-08-07 01:02:41 JST.
- Startup diagnostics identify the local project root, runtime source root, and config source as `C:\Users\coala\Desktop\Grok_API\Codex\AItuber` and its `config\config.yaml`; the Trace fingerprint was `360b5d5ef05f211b`.
- Existing stop handling and the cognitive end signal both fired. `REMAIN_SILENT` and `BRIEF_ACKNOWLEDGE` were generated and had higher raw scores than `ANSWER`.
- The direct cause was the old `response_required` filter, which reduced the selector pool to `ANSWER` before scoring. Planner then received `ANSWER`; no later surface transformation occurred.

## Implemented route

1. Existing intent detectors and TurnFrame signals produce a text-free `ActionEligibility` snapshot.
2. `ActionConstraintValidator` runs immediately after raw selector choice and before planner/realizer.
3. Closure-only allows `REMAIN_SILENT`/`BRIEF_ACKNOWLEDGE`; explicit reply request allows only `BRIEF_ACKNOWLEDGE`; direct question allows `ANSWER`.
4. Invalid selection is deterministically corrected from the eligible candidates. No LLM retry or Legacy fallback is used.
5. `BRIEF_ACKNOWLEDGE` uses one fixed sentence through the direct TTS route, so it cannot expand into a normal prompt-driven answer, question, or new topic.

## Trace additions

`action_constraints` now records no user or response text: closure signal/confidence, direct-question and explicit-request flags, new-task/safety flags, response obligation, eligible actions, candidate scores, initial/final selection, correction result, response policy, and reason.

## Automated verification

- Closure-only excludes ANSWER and continuation.
- Fault-injected high-score ANSWER is rejected and corrected before planner.
- Explicit reply request on a closure signal allows only BRIEF_ACKNOWLEDGE.
- Direct question retains ANSWER; affirmative closure preserves unrelated open work; terminal punctuation variants share the stop intent; low-confidence closure installs no unsafe hard answer constraint.
- BRIEF_ACKNOWLEDGE has a speech plan and a single-sentence direct route.
- Targeted: `17 passed`.
- Final full test run: `2954 passed, 1 skipped` (Windows symlink privilege unavailable).

## Required next step

Do not issue an extra test utterance automatically. After the user reviews this report, fully restart, select Local PROD SESSION while idle, and use the single terminal-cue probe. Require either valid silent closure with provenance or one BRIEF_ACKNOWLEDGE delivery; no ANSWER, Tool execution, fallback, or duplicate delivery.
