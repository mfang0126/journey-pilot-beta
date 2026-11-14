---
name: execution-review
description: Use after a Journey Pilot run, task, or failed attempt when an agent must write a six-question review backed by verified trace references and retrieve reusable lessons. Reviews and lessons are reference material only; this skill does not execute devices, auto-trigger, install hooks, or grant Journey verified status.
---

# Execution Review

Turn one recorded run into a review that another agent can audit, then retrieve
lessons for an exact task key. The review is narrative plus **verified trace
references**, not a copy of the event log.

## Invocation boundary

Use this skill only when a person or calling agent explicitly starts a review
after a run, task, failure, or improvement attempt.

- This file does not install a hook, watcher, daemon, or automatic post-run
  trigger. Do not claim that a review happens automatically.
- Do not use this skill to operate a phone, publish, comment, install packages,
  change global agent configuration, or commit/push.
- A saved review or lesson is never executable and never grants a Journey
  `verified` state. Replay still goes through `propose` / `validate` / `run`.

## Before writing

1. Read the run with `journey-pilot inspect` or the equivalent store API.
2. Identify the event ranges that support each statement.
3. Build a reference for every cited range:

```python
from journey_pilot.trace import build_reference, verify_reference

ref = build_reference(store, run_id, seq_start, seq_end)
assert verify_reference(store, ref)
```

A reference is an inclusive `seq_start`..`seq_end` range plus the digest of
those events. Appending later events keeps it valid; deleting, replacing, or
losing a cited event invalidates it.

Do not paste raw events, observations, screenshots, task outputs, titles,
comments, or model summaries into the review. The caller owns redaction of the
narrative before saving. The review module stores what it is given; it does not
copy raw event or output payloads.

## The six questions

Answer all six. Keep each answer tied to evidence or label it unknown.

1. **Completion** — What evidence proves the intended result happened? Which
   postcondition, observation, or output supports that claim? A recorded action
   is not completion.
2. **Cost** — How many actions, observations, waits, retries, and unknown
   outcomes were spent? Use trace metrics. If a metric was not recorded, say
   missing; do not report it as zero.
3. **Repetition** — What work was repeated, rediscovered, or retried? Cite the
   relevant ranges. Distinguish necessary retries from duplicated effort.
4. **Cause or hypothesis** — What explains the result? Mark a cause as `fact`
   only when the cited events prove it. Otherwise mark it `hypothesis` and state
   what evidence would disprove it. Never turn a plausible story into a fact.
5. **Scope** — When does the conclusion apply, and when does it become invalid?
   Include app/page/version/device conditions and any assumption the run did not
   test.
6. **Next experiment** — What is the smallest change or check that would reduce
   uncertainty or cost? Prefer one bounded, reversible experiment with a clear
   pass/fail signal over a broad rewrite.

## Claims and references

Each claim has:

```json
{"kind": "fact", "text": "short statement", "references": [trace_reference]}
```

- `kind` is exactly `fact` or `hypothesis`.
- A `fact` needs at least one reference, and every reference must pass
  `verify_reference`.
- A `hypothesis` may have no reference, but any reference it does include must
  still verify.
- A hypothesis stays a hypothesis in storage. Do not promote it because it
  sounds likely, because a model said it, or because a lesson repeats it.
- If you cannot cite it, write it as a hypothesis or write `unknown`.

## Save a review

```python
from journey_pilot import review

result = review.save_review(store, {
    "run_id": run_id,
    "task_key": "read-title",
    "questions": {
        "completion": "...",
        "cost": "...",
        "repetition": "...",
        "cause": "...",
        "scope": "...",
        "next_experiment": "...",
    },
    "claims": [
        {"kind": "fact", "text": "...", "references": [ref]},
        {"kind": "hypothesis", "text": "...", "references": []},
    ],
    "lesson": {                     # optional
        "rule": "reusable instruction",
        "applies_when": "conditions where it holds",
        "invalid_when": "observable conditions that falsify it",
    },
})
```

`save_review` returns the normal result envelope. Success is `recorded`, not
`completed`; it means the review was saved durably.

- Reviews are immutable, numbered files under
  `runs/<run_id>/reviews/<version>.json`. A second save creates version 2; it
  never edits version 1.
- A lesson is an immutable file under `<store>/lessons/<lesson_id>.json` and
  points back to its review (`run_id`, review `version`, `review_id`). It also
  carries `review_digest`, the SHA-256 of the review's canonical JSON content
  (key order and whitespace normalized, not a hash of the file bytes). A
  revised rule is a new review and a new lesson, not an overwrite. Keep the old
  `invalid_when` evidence.
- Both documents carry `executable: false` and `runnable: false`.

Invalid types, oversized text, unknown fields, a missing run, a fact without a
reference, and a reference that does not verify are rejected without writing a
review. The request's narrative is caller-supplied; redact it first.

## Retrieve lessons

```python
lessons = review.discover_lessons(store, {"task_key": "read-title", "limit": 20})
```

- Matching is exact on `task_key`; there is no fuzzy or alias lookup here.
- Results are bounded (default 20, maximum 50) and always non-executable.
- Each lesson is re-checked before it is returned: the review it points to must
  still exist and every cited range must still verify. A lesson whose review or
  cited events were deleted, replaced, or corrupted is omitted, not returned as
  current experience. Do not restore such a lesson from memory or from an old
  summary; write a new review against a run whose events are intact.
- New lessons carry `review_digest` and are returned only when the canonical
  review content still matches, with `source_integrity: "verified"`. That covers
  both the cited ranges and the review content.
- Legacy lessons saved before the digest existed have no `review_digest`. They
  are still returned when their references verify, with
  `source_integrity: "legacy_no_digest"`. That guarantees only the cited
  references; it does not prove the review content is unchanged. Treat such a
  lesson as lower-assurance reference material, and write a new review if the
  rule matters.
- A lesson whose digest is present but malformed or mismatched is omitted. Do
  not fall back to treating it as legacy.
- Treat a lesson as a hint for the next experiment, not as permission to run a
  Journey. Check the current environment and use the normal eligibility path.
- If no lesson matches, say so. Do not invent a lesson to fill the gap.

## Close the loop

1. Save the review and any lesson.
2. Retrieve lessons for the same task key in a fresh session.
3. Choose one minimal change or check from `next_experiment`.
4. Run the bounded experiment through the normal CLI path.
5. Save a new review with the result, including failure or no-change findings.

Do not overwrite the earlier review to make the story cleaner. Counter-evidence
belongs in a new review and lesson revision.
