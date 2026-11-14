# Journey Pilot beta

Journey Pilot is an open-source Python CLI for AI agents to share experience and replay validated Android user journeys through adb and phone-harness.

Journey Pilot 是一个开源 Python 命令行工具，让 AI Agent 通过 adb 和 phone-harness 共享经验并重放经过验证的 Android 用户旅程。

## Why Journey Pilot?

An agent can record what it learned, and other local agents can discover that experience without relying on the original conversation. A Journey is data, not permission to act: it must pass validation for the target context before replay.

## Features

- Propose and validate Journeys, discover prior experience, and execute verified recipes with guards.
- Record observations and logs before interacting with the phone; inspect traces and review runs afterward.
- Resume eligible paused runs from trusted checkpoints, subject to the same recipe and parameters and no unresolved action effects.
- Block locked screens, unknown actions, and external writes by default. External writes require an explicit, scoped grant.
- Avoid storing body text by default. User-supplied recipes, labels, logs, and opt-in artifacts still need privacy review before sharing.

## Requirements

- Python 3.11 or later.
- Android Debug Bridge (`adb`).
- [phone-harness](https://github.com/ShawnPana/phone-harness), installed separately and available on PATH, or specified with `PHONE_HARNESS_BIN`.
- For device commands, a connected, unlocked Android device with USB debugging authorized. The documented setup uses the same Mac and user as the connected device; pixel observation depends on macOS Vision.

## Install

From the repository root, install into a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

## Quick start

```sh
journey-pilot capabilities
journey-pilot status
journey-pilot task-list
```

`capabilities` describes supported commands and limitations without accessing a device. `status` checks device availability; `task-list` lists registered tasks. See the [quickstart](docs/quickstart.md) for a read-only task and the [agent interface](docs/agent-interface.md) for JSON requests, validation, and execution. The default store is `~/.local/share/journey-pilot`; set `JOURNEY_PILOT_HOME` to share a store among local agents.

## FAQ

### How is Journey Pilot different from Appium or uiautomator2?

Journey Pilot focuses on shareable agent experience and validated Journey replay. It uses the separately installed phone-harness for device observation and actions; it does not bundle Appium or uiautomator2.

### Does Journey Pilot need a rooted Android phone?

The documented setup requires adb, an authorized Android device, and phone-harness; it does not specify root as a requirement. Check the requirements of your device and phone-harness separately.

### Is it safe to run on my own phone?

It has default guards for locked screens, unknown actions, and external writes, but beta software and device interactions still require supervision. Review recipes, permissions, logs, and artifacts before running or sharing them.

### Can multiple AI agents share what they learned?

Yes. Local agents can use the same store through `JOURNEY_PILOT_HOME` to discover recorded experience. Discovery alone does not make a Journey verified or grant permission to execute it.

### Does it save the text displayed on my phone?

Body text is not stored by default. User-supplied recipes and labels and opt-in artifacts can still contain sensitive information; review them before sharing.

### Can it automate any Android app or recover from any failure?

No. Replay is bounded by current observations, guards, and validated conditions. The Xiaohongshu and Douyin task scripts are examples, not guaranteed platform integrations; interfaces change and each use needs independent verification.

## Status

This is **v0.1.0-beta**. The repository documents limited real-device observations, including specific Douyin visual branches; it does not establish general device compatibility, platform reliability, or cross-agent acceptance. Offline tests do not substitute for device testing. Verify your own phone and phone-harness version independently. See [requirements](docs/requirements.md) and [task scripts](docs/task-scripts.md) for the acceptance boundaries.

## License

[MIT](LICENSE). See [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) for upstream attribution.
