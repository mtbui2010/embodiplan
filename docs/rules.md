# embodiplan rules (full text)

The short form is in [../CLAUDE.md](../CLAUDE.md); the numbers match.

1. **Keep the public API (`embodiplan.__all__`) backwards compatible within 0.x.**
   robot_agent pins `embodiplan>=0.2.1,<0.3` and imports only the public API
   (from its planner backends). A removed or renamed public name needs a minor
   version bump (0.3) and a deprecated alias in the meantime (as `GRACE` ->
   `SAGE`).
2. **SemVer + [CHANGELOG.md](../CHANGELOG.md) on every interface change.** The
   action rules carry their own version, `embodiplan.ACTION_MODEL_VERSION`; bump it
   when a precondition or effect changes. Why: robot_agent's action model reads
   these rules (one source of action rules).
3. **Tests run offline**: no LLM server, no network (fake the LLM backend).
4. **CLAUDE.md stays short**: rules and links only; usage, registry, file map go
   in `docs/*.md`.
