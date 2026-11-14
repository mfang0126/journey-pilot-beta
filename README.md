# Journey Pilot beta

Journey Pilot lets local agents record, discover and share experience as replayable Android Journeys. A Journey is data: validate it successfully for the target context before replay. Record observations and logs before touching the phone. Locked screens, unknown actions and external writes are blocked by default; external writes require an explicit, scoped grant. Body text is not stored by default, although user-supplied recipes, labels and opt-in artifacts still require privacy review.

## Install

Requires Python 3.11 or later, Android Debug Bridge (`adb`), and [phone-harness](https://github.com/ShawnPana/phone-harness) installed separately and available on PATH (or set `PHONE_HARNESS_BIN`). Device commands require a connected, authorized Android device. Install in a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
journey-pilot capabilities
journey-pilot task-list
```

`capabilities` describes the supported commands and limitations. See [quickstart](docs/quickstart.md) and [agent interface](docs/agent-interface.md) for JSON stdin requests, validation and execution. Store data defaults to `~/.local/share/journey-pilot`; set `JOURNEY_PILOT_HOME` to share a store among local agents.

## What it does

Agents can propose and validate Journeys, discover prior experience, execute verified recipes with guards, inspect traces, and resume eligible paused runs from trusted checkpoints. The Xiaohongshu and Douyin thin task scripts are examples, not guaranteed platform integrations; platform interfaces change and each use needs independent verification.

## Limitations

This is a beta. Offline tests do not establish device compatibility or platform reliability. Replay is bounded by current observations, guards and validated conditions; it is not arbitrary autonomous recovery. Resume requires the same recipe and parameters and no unresolved action effects. Phone-harness versions and devices require independent verification. Pixel observation depends on macOS Vision. Logs and opt-in artifacts can contain sensitive information; review them before sharing. See `capabilities` for current limitations and [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) for upstream attribution.
