# embodiplan/verifier.py
# ─────────────────────────────────────────────────────────────────────
# Symbolic precondition verifier for the SAGE planner.
#
# A lightweight, rule-based, LLM-free checker that decides — given the
# robot's symbolic state — whether the next action is admissible.
# The rules (preconditions + effects, per edge case) are defined once in
# embodiplan.action_model (check / apply / simulate, ACTION_MODEL_VERSION);
# this module is the 0.1-compatible facade over it (verify_step, _apply,
# simulate) plus the LLM feedback formatter.
#
# Why rule-based and not LLM:
#   - O(1) per step, zero token cost
#   - Deterministic — same plan always gets the same verdict
#   - Used both as a gate BEFORE execution (catches hallucinations) and
#     as a feedback signal to drive a single LLM refinement pass.
# ─────────────────────────────────────────────────────────────────────

from __future__ import annotations

# The rules themselves live in embodiplan.action_model (the public, versioned
# API). This module keeps the 0.1 names and signatures on top of it.
from embodiplan.action_model import (  # noqa: F401  (re-exported)
    ACTION_MODEL_VERSION,
    Capabilities,
    DEFAULT_CAPABILITIES,
    DEFAULT_CONTAINERS,
    StepViolation,
    SymbolicState,
    VerifierReport,
    Violation,
    apply as _model_apply,
    check,
    normalize_action,
    normalize_plan,
    normalize_step,
    simulate,
)


def _apply(step: dict, state: SymbolicState) -> None:
    """Mutate `state` as if `step` had been executed successfully
    (legacy name of ``action_model.apply(step, state, inplace=True)``)."""
    _model_apply(step, state, inplace=True)


def verify_step(
    step: dict,
    state: SymbolicState,
    containers: set[str] | None = None,
    *,
    capabilities: Capabilities | dict | None = None,
) -> tuple[bool, str | None, str | None]:
    """Check whether `step` is admissible in `state` (legacy shape of
    ``action_model.check``: the first error only, warnings ignored).

    Returns (ok, code, reason).
        ok=True  → step is valid; reason is None
        ok=False → reason describes the violation, code is a short tag
    """
    for v in check(step, state, containers=containers, capabilities=capabilities):
        if v.is_error:
            return False, v.code, v.reason
    return True, None, None


# ─────────────────────────────────────────────────────────────────────
# Feedback formatter for LLM refinement
# ─────────────────────────────────────────────────────────────────────
def format_violations_for_llm(
    report: VerifierReport,
    plan:   list[dict],
    limit:  int = 6,
) -> str:
    """Render violations as a compact, actionable instruction block.

    Designed to be appended to a refine prompt: tells the LLM exactly
    which steps failed, why, and what state the robot was in.
    """
    if report.ok:
        return "All preconditions satisfied."

    lines = ["The previous plan violates these preconditions:"]
    for v in report.violations[:limit]:
        a = v.step.get("action", "?")
        o = v.step.get("object", "")
        lines.append(f"  step {v.index+1} ({a} {o}): {v.reason}")
    if len(report.violations) > limit:
        lines.append(f"  ... and {len(report.violations) - limit} more.")

    lines.append(
        "\nFix the plan so each step satisfies its preconditions. "
        "Common fixes:"
    )
    lines.append("  - Insert MoveTo <container> + Open before Find for items inside containers.")
    lines.append("  - Insert Find <object> before Pick / Open / Close / TurnOn / TurnOff.")
    lines.append("  - Insert MoveTo <receptacle> before Place; the receptacle must match.")
    lines.append("  - Do not Pick while already holding something — Place the held item first.")
    codes = {v.code for v in report.violations}
    if codes & {"already_open", "close_not_opened", "already_on", "turnoff_not_on"}:
        lines.append("  - Do not Open / TurnOn what is already open / on, nor Close / TurnOff what is not.")
    if "hand_full" in codes:
        lines.append("  - The robot has one hand: Open / Close containers before the Pick.")
    return "\n".join(lines)


__all__ = [
    "ACTION_MODEL_VERSION",
    "Capabilities",
    "Violation",
    "check",
    "SymbolicState",
    "StepViolation",
    "VerifierReport",
    "DEFAULT_CONTAINERS",
    "normalize_action",
    "normalize_step",
    "normalize_plan",
    "verify_step",
    "simulate",
    "format_violations_for_llm",
]
