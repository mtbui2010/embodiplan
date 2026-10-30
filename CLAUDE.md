# embodiplan

Pluggable LLM task planner library for household robotics (AI2-THOR); robot_agent's SAGE backend uses it.
Docs index: [docs/README.md](docs/README.md). Usage, registry, file map: [docs/usage.md](docs/usage.md).

## Rules

1. Keep the public API (`__all__`) backwards compatible within 0.x; robot_agent pins `>=0.2.1,<0.3`.
2. Bump SemVer, `ACTION_MODEL_VERSION` (for action rule changes) and [CHANGELOG.md](CHANGELOG.md) on interface changes.
3. Keep tests offline: no LLM server, no network.
4. Keep this file short: one-line rules and links; details go in `docs/*.md`.

## Test

`PYTHONPATH=$PWD python -m pytest -q tests`

## Docs

- [docs/usage.md](docs/usage.md): usage, registry keys, output, backends, key files
- [docs/rules.md](docs/rules.md): rules with reasons
