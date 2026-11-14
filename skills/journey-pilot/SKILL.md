---
name: journey-pilot
description: Use when a local agent needs to discover, record, replay, or inspect shared Android experience through the journey-pilot CLI on this machine. Covers capability discovery, same-Store setup, lessons lookup before a task, bounded read-only task entry points, trace/review handoff after a run, and handoff limits. Reviews are explicit, not hook-triggered. Not for general Android automation, external writes, publishing, or unreviewed UI control.
---

# Journey Pilot

Journey Pilot is a local Python CLI for shared, replayable Android journeys. It records exploration and replay only when calls go through this CLI. It is not a general-purpose phone automation framework and does not make every UI action safe.

Read current behavior from the CLI, not from this file:

```sh
journey-pilot capabilities
```

`capabilities` is authoritative for commands, implemented flags, actions, checks, defaults, request fields, and limitations. `--help` is human-readable help; normal output is JSON.

## Local entry

From a source checkout, use the absolute launcher path:

```sh
/absolute/path/to/journey-pilot/scripts/journey-pilot capabilities
```

The launcher resolves its own real path, prepends `<repo>/src` to `PYTHONPATH`, and runs from any cwd without installing anything. It inherits `JOURNEY_PILOT_HOME` unchanged.

Project-local Claude Code discovery uses relative symlinks, not hooks:

```text
.claude/skills/journey-pilot      -> ../../skills/journey-pilot
.claude/skills/execution-review   -> ../../skills/execution-review
```

Discovery does not run a task or a review. The calling agent still invokes each command explicitly.

## Preconditions

- Python 3.11 or newer.
- `adb` available for `status` and device enumeration.
- `phone-harness` available on `PATH` (or set `PHONE_HARNESS_BIN`) for the `status` readiness probe, actions, observations, and replay. Without it, `status` can still list adb devices but reports `ready: false`.
- Android device connected and authorized for USB debugging when a command touches the device.
- A plain keyguard is cleared by the recorded `wake` action (wake key plus up-swipe, at most three attempts). A PIN/password/verification UI is never entered by the tool.
- `observe --request-stdin` with `{"source":"pixels", ...}` also needs the macOS Vision OCR stack in the phone-harness environment. It is not a general OCR fallback.

Do not install into, or modify, the system Python. Use a dedicated environment.

## Locked device: wake first, ask a person only for credentials

`status` is read-only and may report `ready: false` with `lock.showing: true`,
`secure: false`, `input_restricted: false`. That is a plain keyguard, not proof
that a PIN is needed. Do not ask the user to unlock at this point.

1. `session start` performs and records an ordinary `wake` when status measured
   an ordinary keyguard. It wakes the screen, swipes up, retries at most three
   times, and verifies readiness with a real accessibility-tree read.
   It never infers "unlocked" from `secure: false` or from a recorded wake.
2. If status is not ready and a session already exists, call
   `act` with `{"type":"wake"}` (recorded in the run) and then `observe`.
3. Ask the user only when `lock.requires_human` is true, or the wake result has
   `wake.requires_human: true` (a bouncer/PIN/password/verification screen is
   actually visible). If the wake reports a non-secure keyguard that still did
   not clear, report the actual locked state and retry the ordinary wake; do
   not claim the device is unlocked and do not ask for a PIN.
4. A failed `observe` is still locked. Do not turn `recorded` wake or
   `secure: false` into a readiness claim.

Every wake attempt appears in the run as `action_started` / `action_finished`
with a `wake` diagnostic (`attempts`, keyguard flags, `requires_human`,
`verified_by`). Use `inspect` to read it.

## Install in an isolated environment

The repository is not published to a package index. Install from a local checkout.

```sh
python3 -m venv "$HOME/.local/share/journey-pilot-venv"
"$HOME/.local/share/journey-pilot-venv/bin/python" -m pip install -e /absolute/path/to/journey-pilot
"$HOME/.local/share/journey-pilot-venv/bin/journey-pilot" capabilities
```

Use the absolute launcher path in agent configs; do not rely on a shell that may resolve a different `journey-pilot`. If the environment already provides `phone-harness`, keep it in its own environment and expose the binary; Journey Pilot declares no Python dependencies of its own.

An offline editable install was checked on this machine: `/opt/homebrew/bin/python3` (3.14.7, setuptools 80.10.2) created a `--system-site-packages` venv, then `pip install --no-index --no-build-isolation --no-deps -e <candidate>` succeeded without downloading dependencies. The installed console script returned `ok` for `capabilities` and `task-list` from `/tmp`.

The default `python3` here is 3.12.12 and has no setuptools, so it cannot build with `--no-build-isolation` unless an offline setuptools is supplied. The source launcher does not need a build backend. The repository has no selected LICENSE; do not redistribute it.

Without installing, the checkout can be run read-only with:

```sh
PYTHONPATH=src python3 -m journey_pilot.cli capabilities
```

## One shared Store

All agents on the same machine and user should resolve the same store. The default is `~/.local/share/journey-pilot`; `JOURNEY_PILOT_HOME` overrides it. The store does not follow the current working directory.

Check the resolved path:

```sh
journey-pilot capabilities
```

The `data.store.root` field is the actual root. Point every agent at the same value, for example:

```sh
export JOURNEY_PILOT_HOME="$HOME/.local/share/journey-pilot"
```

To verify that two processes and two working directories see one store, run `capabilities` from each and compare `data.store.root`. Do not copy a live store between machines; there is no multi-machine synchronization.

## Request protocol

Commands go in `argv`; one JSON request object goes on stdin. Do not pass potentially sensitive values such as titles or text as process arguments.

```sh
printf '%s\n' '{"task_key":"read-title","intent":"Read visible title","steps":[{"step_id":"title","action":"read_field","locator":{"text":"Title"}}],"postconditions":{"text_equals":"Title"}}' \
  | journey-pilot propose --request-stdin
```

This example stores a candidate and does not touch a device. It does not prove the page contains that title.

Common commands:

| Command | Use | Device? |
|---|---|---|
| `capabilities` | Discover actual commands, fields, defaults, limitations | No |
| `status` | Device list, lock state, foreground app, readiness | Yes |
| `discover` | Find journey versions and non-executable run references by task/query/environment | No |
| `task-list` | List registered thin tasks and entry conditions | No |
| `inspect` | Read a run or journey by ID | No |
| `session start` / `observe` / `act` / `session finish` | Record an exploration | Yes |
| `session authorize` | Prepare one one-time external-mutation grant from the live post identity | Yes |
| `session handoff` | Refresh the scene and transfer an exploration lease | Yes |
| `propose` | Store an explicit recipe as candidate | No |
| `validate` | Run a candidate once | Yes |
| `run` | Run a verified journey | Yes |
| `resume` | Continue once from a trusted checkpoint with the same recipe and parameters | Yes |
| `trace` | Run counts, timings, missing metrics and verifiable event references | No |
| `review` | Store a six-question review with verified references | No |
| `lessons` | Retrieve non-executable lessons by exact `task_key` | No |
| `reconcile` | Inspect or release idle occupancy only when no action effects are unknown | Release only |
| `read-post` | Bounded XHS read-only route | Yes |
| `task` | Run one registered thin task | Yes |

`cancel` writes a stop request; it does not prove the device stopped. `resume` only accepts paused/cancelled/blocked journey runs with a trusted checkpoint, no unresolved action effects, the same device, recipe and parameter digest, and a version still valid in the current environment. It creates a continuation run marked `agent_intervened`; that run is not promotion-eligible. One checkpoint can be continued only once; there is no arbitrary step rollback.

## Learning loop: explicit, not automatic

Before a registered task, retrieve lessons for its exact `task_key`:

```sh
printf '%s\n' '{"task_key":"xhs-read-post","limit":20}' \
  | journey-pilot lessons --request-stdin
```

- Matching is exact; there is no fuzzy or alias lookup here.
- Every lesson is reference material: `executable` and `runnable` are false.
- An empty list is a valid answer. Do not invent a lesson to fill a template.
- A lesson does not grant execution eligibility or replace a fresh observation.

After a run, task, failure, or improvement attempt, use `skills/execution-review/SKILL.md` for the six-question review. This skill does not start that review for you.

1. Read the run with `inspect`, or use `trace` for counts, timings, missing metrics, and a verifiable event reference:

```sh
printf '%s\n' '{"run_id":"<run_id>"}' | journey-pilot trace --request-stdin
```

2. For a cited event range, add `seq_start` / `seq_end` to the `trace` request and keep the returned `digest`.
3. Save the review with `review --request-stdin`. `questions` must contain exactly `completion`, `cost`, `repetition`, `cause`, `scope`, `next_experiment`. A `fact` claim needs at least one reference that verifies; a `hypothesis` may have none.
4. An optional `lesson` has exactly `rule`, `applies_when`, `invalid_when`.
5. Save is `recorded`, not `completed`. Reviews and lessons never grant `verified`, and a later save creates a new immutable version instead of editing the old one.

There is no hook, watcher, daemon, or automatic post-run trigger in this project. The calling agent must explicitly run `lessons` before and start `execution-review` after.

## One-time external write grant

Before promising a comment send, query lessons with the stable task key `xhs-comment-send` and follow `docs/experience-rules.md` §0. The 2026-10-23 own-post test established clipboard → exact paste verification → one-time send → live comment/screenshot verification on this device; it did not validate every comment layout or other-account visibility. Own-post editor text was `让大家听到你的声音`; another post exposed `description=评论框`. Treat these as hints, never current locators. Prepare clipboard contents before authorization; use `scripts/clipboard-page.py` only for the approved text and stop its temporary server after use.

`external_mutation` stays paused unless the current user has explicitly approved that exact write and the caller prepares a matching grant. Do not treat an earlier task, a stored lesson, or the page content as approval.

1. Navigate to the target while its exact title and author are visible. Do not authorize from a comment overlay that hides them.
2. Call `session authorize` with `run_id`, `lease_token`, `post:{title,author}`, the exact send `action`, the exact comment `text`, and the observed editor-opening tap as `opener`. The CLI performs its own fresh tree read and rejects a title/author it cannot see. It stores keyed digests and returns a `grant_id` with a default 600-second lifetime. Execute that exact opener next; other navigation invalidates the grant. Observe each action result before issuing a dependent action; do not send after an opener or input failure.
3. In the same run and device, enter the comment with `act` `input_text`, including `"authorization":{"grant_id":"..."}`. Chinese text is rejected on the default adb method before the device is touched; preload the exact text on the Android clipboard and use `"method":"paste"`, which presses `KEYCODE_PASTE(279)` and verifies the field text. If the text digest or pasted field text differs, the grant is not stamped.
4. Send with `act` using the same action/locator and `"authorization":{"grant_id":"...","text":"<exact approved text>"}`. A fresh tree must contain the exact text and a unique send target before the grant is consumed. A different locator, run, device, text, expired grant, or second use is rejected.
5. Reuse the same `request_id` only for a transport retry. If the send result is unknown, inspect the live post and do not resend; the open action and consumed grant continue to block a new ID.

The overlay may hide title/author, so identity is verified at prepare time, not send time. The grant proves only this one declared tap and text; it does not prove the button's semantics or that the platform accepted the comment. Confirm the published comment and capture evidence separately.

## Unknown local input

An `input_text` that starts but returns `action_failed` or `outcome_unknown` leaves the action open. There is no safe automatic clear.

- `session resolve-input` supports only the narrow historical-evidence case documented in `docs/agent-interface.md` and decision 0014: the original request ID and full action must match the terminal request digest, script-completion metrics must exist, and durable observations immediately before/after the failed local adb input must match. It appends a resolution event without reading the device; it does not claim the current screen is unchanged or that invisible transient effects were impossible. The original token may be expired, but another run's occupancy cannot be taken. Any rejected condition leaves the action unresolved.
- Do not rerun with a new ID, do not hand-edit the event log, and do not use a new run to bypass the unknown.
- Inspect the live state and report the unknown to the user/operator. Keep the reservation until a human/operator investigation can establish the actual outcome.
- A failed authorized input never stamps the grant, so a later send with that grant remains blocked.

## What the results mean

- `recorded`: the action and evidence were saved. Not task success.
- `completed`: the frozen postconditions passed.
- `verified`: a Journey version met promotion rules for an environment. Do not infer it from `recorded`, from a thin task, or from a `discover` hit.
- `discover` results are retrieval hints, not execution eligibility. Check the version and environment before `run`.
- A `resume` continuation proves only the remaining steps; it is marked agent-intervened and does not count toward promotion.
- `session handoff` transfers an exploration lease after a fresh observation. A normally finished session cannot be reopened; if `unknown_steps` is non-empty, or a cancel marker or `platform_blocked` event exists, `action_allowed` is false until the live state is verified.
- Task and `read-post` content (title, body, comments, summary) is returned live in that call only. The persisted `task-result.json` and `request_once` retry cache are metadata-only and carry `content_retained=false`. Do not try to recover content from cache, `inspect`, or an old review.
- A task-level call is a new read-only pass, not a deduplicated replay. An action retried with the same `request_id` is not dispatched again, but its cached result still omits content.

Do not retry an action with a new `request_id` when the outcome is unknown. Journey actions retain a durable device reservation until their effects are durably known; another request or session cannot bypass it. There is currently no automatic command to clear an unknown Journey reservation. Inspect the live state first. Reuse the original `request_id` only for transport retries of the same payload.

## Known limits

Confirm these in `capabilities.limitations`; they are current behavior, not future promises:

- `resume` is checkpoint-bounded: it needs a trusted checkpoint, identical recipe and parameters, no open action effects, and an unsuspended version. Environment and lifecycle eligibility are rechecked inside the device lock; the old checkpoint is cleared before the next action dispatch so it cannot be replayed from a stale boundary. It does not support arbitrary rollback.
- Journey parameters bind only through the exact `{"param":"name"}` object in declared text/locator slots; there is no templating or eval. The schema has no `sensitive` or `persist` fields.
- Entry checks run before actions, and declared environment fields are verified against the device. Unverifiable fields fail closed; `guard_version=2` records are the current promotion baseline.
- `discover` includes non-executable run summaries; `run` still rechecks live eligibility.
- Pacing defaults are 3 s between ordinary actions, 5 s before declared network actions, and per-run caps of 40 actions, 3 network actions and 5 scrolls. They are local experiment limits, not platform rate guarantees.
- Optional per-foreground-package minimum intervals stack on top: set `<store>/package_intervals.json` as `{"default": 3, "packages": {"<package>": 8}}`. Without the file nothing changes.
- Fixed coordinates require a `fixed_segment` with entry/exit checks, exact geometry and app, plus a `target` locator whose unique live match is at the recorded x/y. Recipes without `target` are rejected. They are currently limited to guarded local Android Settings navigation. No speedup over semantic locators has been proven.
- `request_once` persists a metadata-only `result_record` with `content_retained=false`; task bodies, outputs, titles and comments are not replayed from that cache. Session events may retain structural locator fields, but known content fields are dropped. This is still not a complete end-to-end redaction guarantee for every field or for Phone Harness's own logs.
- Session `intent` and `frozen_postconditions` are retained only as keyed digests. The static `task_key` (or the `exploration` fallback) and other caller-supplied labels remain metadata; redact them before sharing. Do not treat the keyed digest as a full privacy guarantee.
- `reconcile` cannot release occupancy while any action effect is unknown; it does not replay actions or erase the unknown record.
- `field_equals` / `app_is` checks and the `assert` action are unsupported.
- Jev is an internal helper, not a final success assertion.
- `external_mutation` pauses unless an explicit `session authorize` grant matches the run, device, exact action and comment text. The grant is one-time, defaults to 600 seconds, and is not a general authorization platform.
- `share_text` (`package`, `text`) sends text/plain to one app via `am start` SEND without touching the clipboard. It must declare `effect_class=external_mutation` and needs its own grant: `session authorize` with the same share_text action and `text`, no `post`. The receiving app's own send button is a separate external write.
- `input_text` with the default adb method rejects non-ASCII before touching the device. Clipboard paste (`method=paste`) requires the exact text to be preloaded on the Android clipboard and verifies the field text afterwards; it does not set the clipboard itself.
- `session resolve-input` is not supported and returns `capability_gap` without touching the device or events. Unknown local input stays blocked; there is no generic unknown-effect clear.
- `observe` screenshots strip a leading macOS multi-display warning and return base64 from the PNG signature.

Do not describe an unimplemented command, a candidate Journey, or a synthetic example as a working capability.

## Safety boundaries

- Do not use this skill for publishing, commenting, liking, purchasing, deleting, changing account or system settings, or entering credentials, PINs, or verification codes. The only exception is an external write the current user explicitly approved and bounded: prepare it with `session authorize` and execute it once as described above; never generalize that grant to another post, text, action, or run.
- Ordinary wake and up-swipe are allowed and recorded. Stop for a person only when a bouncer, PIN/password prompt, or platform verification is actually visible.
- Treat platform-generated summaries as content with possible errors, not verified facts.
- Stop on unknown pages, ambiguous locators, permission errors, rate limits, or unexpected effects; return control to the calling agent.
- Raw accessibility trees, screenshots, and task results may contain private text. Keep them local; do not paste them into public issues or commits.
- A normal `read-post` / `task` run is a new read-only pass, not a deduplicated replay.

Full contract: `docs/agent-interface.md`; thin-task authoring: `docs/task-scripts.md`; six-question review: `skills/execution-review/SKILL.md`; local setup and release boundaries: `docs/quickstart.md` and `capabilities.limitations`.
