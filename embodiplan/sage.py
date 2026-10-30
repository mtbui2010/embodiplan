# embodiplan/sage.py
# ─────────────────────────────────────────────────────────────────────
# SAGE — Symbolic Action-Gating and Editing planner
# (formerly GRACE; the "GRACE" registry name is kept as an alias).
#
# Three integrated components:
#
#   1.  Hierarchical decomposition (high-level) → sub-goals, each then
#       expanded to action steps (low-level).  Shared structure with
#       HierarchicalFewShotPlanner but every (sub-goal → steps)
#       expansion is wrapped by:
#
#   2.  Symbolic precondition verifier (embodiplan.verifier).  The
#       verifier simulates the candidate sub-plan against the running
#       SymbolicState.  If any precondition is violated, a single
#       refinement pass is issued to the LLM with typed violation
#       feedback (no expensive Self-Refine loop).
#
#   3.  Hybrid memory-augmented retrieval (embodiplan.memory_retriever).
#       The retriever is seeded from eval_dataset_gt.json AND accumulates
#       successful runtime episodes, so the few-shot context strengthens
#       over time.
#
# Repair (.replan):
#   Unlike all other embodiplan methods, SAGE does NOT re-explain the
#   full task on failure.  It identifies which sub-goal the failed step
#   belongs to, and asks the LLM to regenerate ONLY the suffix of that
#   sub-goal — given the actual state at the failure point.  Sub-goals
#   that are already complete are preserved; sub-goals not yet started
#   are kept intact.
#
# LLM cost:
#   .generate_plan    : 1 (decompose) + N (expand) + R (refines), with
#                       R ≤ N (only fires when verifier rejects).
#   .replan           : 1 (suffix repair only).
# ─────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from embodiplan.base import (
    ACTIONS_STR, JSON_EXAMPLE, STEP_SCHEMA,
    BasePlanner, PlanMetrics, parse_steps,
    DEFAULT_HOST, DEFAULT_MODEL, DEFAULT_BACKEND,
)
from embodiplan.verifier import (
    SymbolicState, simulate, normalize_plan,
    format_violations_for_llm, DEFAULT_CONTAINERS,
)
from embodiplan.action_model import Capabilities
from embodiplan.memory_retriever import (
    MemoryRetriever, MemoryRetrieverConfig, RetrievedExample,
    format_examples_block,
)


# ─────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────
_DECOMPOSE_SYSTEM = """\
You are a household task planner (high-level).
Study the retrieved examples to understand sub-goal granularity, then
break the given task into 2–5 ordered sub-goals (short natural-language
phrases). Each sub-goal should be a distinct phase the robot will
execute end-to-end before moving on.

Return ONLY valid JSON — no markdown, no explanation:
{"subgoals": ["sub-goal 1", "sub-goal 2", ...]}"""


_EXPAND_SYSTEM = f"""\
You are a household assistant robot (low-level executor).
You will receive ONE sub-goal and must expand it into concrete robot
action steps. Stay within this sub-goal — do not anticipate later ones.

Available robot actions:
{ACTIONS_STR}

{STEP_SCHEMA}

Return ONLY valid JSON:
{JSON_EXAMPLE}"""


_REFINE_SYSTEM = f"""\
You are repairing a household plan.  The previous attempt violated
preconditions defined by the action schema.

Available robot actions:
{ACTIONS_STR}

{STEP_SCHEMA}

Return ONLY valid JSON with the corrected steps for this sub-goal:
{JSON_EXAMPLE}"""


_REPAIR_SYSTEM = f"""\
You are repairing a failed sub-goal in a household plan.
Generate ONLY the remaining steps to finish the CURRENT sub-goal.
Do NOT re-execute already-completed steps. Do NOT plan future sub-goals.

Available robot actions:
{ACTIONS_STR}

{STEP_SCHEMA}

Return ONLY valid JSON:
{JSON_EXAMPLE}"""


# ─────────────────────────────────────────────────────────────────────
# Progress payload helpers — keep the streamed `progress` dicts small and
# JSON-friendly so a UI can render the per-sub-goal plan / violations / refine
# trace without pulling in planner internals.
# ─────────────────────────────────────────────────────────────────────
def _ser_steps(steps) -> list[dict]:
    out = []
    for s in steps or []:
        if isinstance(s, dict):
            out.append({"action": s.get("action"), "object": s.get("object")})
    return out


def _ser_violations(report) -> list[dict]:
    out = []
    for v in getattr(report, "violations", []) or []:
        step = getattr(v, "step", {}) or {}
        out.append({
            "index":    getattr(v, "index", None),
            "code":     getattr(v, "code", ""),
            "reason":   getattr(v, "reason", ""),
            "severity": getattr(v, "severity", "error"),
            "action":   step.get("action") if isinstance(step, dict) else None,
            "object":   step.get("object") if isinstance(step, dict) else None,
        })
    return out


# ─────────────────────────────────────────────────────────────────────
# Internal data structures
# ─────────────────────────────────────────────────────────────────────
@dataclass
class _SubgoalBlock:
    """One sub-goal and the steps that implement it."""
    name:    str
    steps:   list[dict] = field(default_factory=list)
    refined: bool       = False   # was at least one refine pass triggered?


def _parse_subgoals(raw: str) -> list[str]:
    raw = re.sub(r"```json|```", "", raw).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return [str(s).strip() for s in data.get("subgoals", []) if str(s).strip()]
        if isinstance(data, list):
            return [str(s).strip() for s in data if str(s).strip()]
    except json.JSONDecodeError:
        pass
    return [m.strip() for m in re.findall(r'"([^"]{5,})"', raw)]


# Public name of the block type (robot_agent drives sub-goal edits on it).
SubgoalBlock = _SubgoalBlock


def flatten(blocks: list[_SubgoalBlock]) -> list[dict]:
    """Concatenate sub-goal blocks, applying symbolic-state-aware dedupe.

    A common failure mode of hierarchical decomposition is that each sub-goal
    re-establishes context the previous sub-goal already left in place
    (e.g. "MoveTo Kitchen" twice across sub-goal boundaries, or "Find Mug"
    after the previous sub-goal already found it). We simulate the running
    symbolic state and drop any step whose effect is already true:

      - MoveTo X    drop if state.arrived == X
      - Find X      drop if state.found == X
      - Open X      drop if X in state.opened
      - Close X     drop if X not in state.opened
      - TurnOn X    drop if X in state.on
      - TurnOff X   drop if X not in state.on

    Pick/Place are never dropped (they have side-effects on holding/arrived).
    """
    from embodiplan.action_model import SymbolicState, apply
    out: list[dict] = []
    state = SymbolicState()
    for b in blocks:
        for s in b.steps:
            a = s.get("action")
            o = s.get("object") or ""
            redundant = False
            if a == "MoveTo" and state.arrived == o:
                redundant = True
            elif a == "Find" and state.found == o and not state.holding:
                redundant = True
            elif a == "Open" and o in state.opened:
                redundant = True
            elif a == "Close" and o and o not in state.opened:
                redundant = True
            elif a == "TurnOn" and o in state.on:
                redundant = True
            elif a == "TurnOff" and o and o not in state.on:
                redundant = True
            if redundant:
                continue
            out.append(s)
            apply(s, state, inplace=True)
    return out


_flatten = flatten  # pre-0.2 private name, still used by older robot_agent


# ─────────────────────────────────────────────────────────────────────
# SAGEPlanner
# ─────────────────────────────────────────────────────────────────────
class SAGEPlanner(BasePlanner):
    name        = "SAGE"
    description = (
        "Symbolic Action-Gating and Editing planner: hierarchical "
        "decomposition + symbolic precondition verifier + hybrid memory "
        "retrieval (seed GT + live episodes) + suffix-only sub-goal repair."
    )

    def __init__(
        self,
        host:           str = DEFAULT_HOST,
        model:          str = DEFAULT_MODEL,
        provider:       str = DEFAULT_BACKEND,
        api_key:        str = "",
        top_k:          int = 3,
        max_refines:    int = 1,
        gt_path:        str = "",
        live_path:      str = "",
        use_chroma:     bool = False,
        chroma_path:    str = "",
        memory:         MemoryRetriever | None = None,
        enable_verifier: bool = True,
        enable_memory:   bool = True,
        enable_local_repair: bool = True,
        safe_refine:     bool = False,
        capabilities:    Capabilities | dict | None = None,
        **kwargs,
    ):
        super().__init__(host=host, model=model, provider=provider, api_key=api_key)
        self.top_k                = top_k
        self.max_refines          = max_refines
        self.enable_verifier      = enable_verifier
        self.enable_memory        = enable_memory
        self.enable_local_repair  = enable_local_repair
        # If True, a refine call returning [] (parse failure) is a no-op:
        # keep prior steps and stop further refines. Default False preserves
        # legacy behaviour (overwrite even with empty).
        self.safe_refine          = safe_refine
        # Optional live-progress hook: callable(dict) invoked at decompose /
        # expand / refine boundaries. Default None -> no-op (no behavior change).
        self.progress = None
        # Robot body the symbolic gate assumes (action_model.Capabilities or a
        # manifest dict); default one hand, Find reaches.
        self.capabilities = Capabilities.from_any(capabilities)

        if memory is not None:
            self.memory = memory
        elif enable_memory:
            self.memory = MemoryRetriever(MemoryRetrieverConfig(
                gt_path     = gt_path,
                live_path   = live_path,
                use_chroma  = use_chroma,
                chroma_path = chroma_path,
                top_k       = top_k,
            ))
        else:
            self.memory = None  # type: ignore[assignment]

        # Track the last decomposition so .replan can localize the failure.
        self._last_blocks: list[_SubgoalBlock] | None = None
        self._last_task:   str                       = ""

    def _simulate(self, plan, **kw):
        """action_model.simulate under this planner's capabilities."""
        return simulate(plan, capabilities=self.capabilities, **kw)

    # ── public editing surface (used by robot_agent's assist loop) ─
    @property
    def last_blocks(self) -> list[SubgoalBlock] | None:
        """Sub-goal blocks of the last generate_plan (None before the first)."""
        return self._last_blocks

    def retrieve_examples(self, task: str) -> tuple[list[RetrievedExample], str]:
        """Memory examples for `task` and their prompt block ("" without memory)."""
        return self._retrieve_block(task)

    def edit_block(
        self,
        block:           SubgoalBlock,
        feedback:        str,
        *,
        task:            str,
        state:           SymbolicState | None = None,
        obs:             str = "",
        visible_objects: list[str] | None = None,
        examples_block:  str | None = None,
    ) -> list[dict]:
        """SAGE's sub-goal-local Editing: re-do one block's steps given
        `feedback` (e.g. checker findings). `state` is the symbolic state at
        the start of the block; `examples_block` defaults to the memory
        examples for `task`. Returns the new steps (the block is not changed;
        [] when the LLM output did not parse). Raises on LLM errors."""
        if examples_block is None:
            examples_block = self._retrieve_block(task)[1]
        steps, _in, _out = self._refine(block.name, task, list(block.steps), feedback,
                                        state or SymbolicState(), obs,
                                        list(visible_objects or []), examples_block)
        return steps

    # ── retrieval helper ──────────────────────────────────────────
    def _retrieve_block(self, task: str) -> tuple[list[RetrievedExample], str]:
        if not self.enable_memory or self.memory is None:
            return [], ""
        try:
            ex = self.memory.retrieve(task, k=self.top_k)
        except Exception:
            ex = []
        return ex, format_examples_block(ex)

    # ── decompose ──────────────────────────────────────────────────
    def _decompose(
        self,
        task:            str,
        obs:             str,
        visible_objects: list[str],
        examples_block:  str,
    ) -> tuple[list[str], int, int]:
        obj_str = ", ".join(visible_objects[:30]) if visible_objects else "none"
        user_msg = (
            (examples_block + "\n\n" if examples_block else "")
            + f"=== NEW TASK ===\nTask: {task}\n"
            f"Scene observation: {obs}\n"
            f"Visible objects: {obj_str}\n\n"
            "Break this into ordered sub-goals:"
        )
        raw, in_tok, out_tok = self._chat(
            [{"role": "system", "content": _DECOMPOSE_SYSTEM},
             {"role": "user",   "content": user_msg}],
            temperature=0.3,
        )
        return _parse_subgoals(raw), in_tok, out_tok

    # ── expand one sub-goal ────────────────────────────────────────
    def _expand(
        self,
        subgoal:         str,
        task:            str,
        state:           SymbolicState,
        obs:             str,
        visible_objects: list[str],
        examples_block:  str,
    ) -> tuple[list[dict], int, int]:
        obj_str = ", ".join(visible_objects[:30]) if visible_objects else "none"
        user_msg = (
            (examples_block + "\n\n" if examples_block else "")
            + f"=== NEW SUB-GOAL ===\n"
            f"Overall task: {task}\n"
            f"Current sub-goal: {subgoal}\n\n"
            f"Robot symbolic state:\n  {state.as_text()}\n\n"
            f"Scene observation:\n{obs}\n\n"
            f"Visible objects: {obj_str}\n\n"
            "Expand this sub-goal into action steps:"
        )
        raw, in_tok, out_tok = self._chat(
            [{"role": "system", "content": _EXPAND_SYSTEM},
             {"role": "user",   "content": user_msg}],
            temperature=0.2,
        )
        steps = normalize_plan(parse_steps(raw))
        return steps, in_tok, out_tok

    # ── refine with verifier feedback ──────────────────────────────
    def _refine(
        self,
        subgoal:         str,
        task:            str,
        prev_steps:      list[dict],
        feedback:        str,
        state:           SymbolicState,
        obs:             str,
        visible_objects: list[str],
        examples_block:  str,
    ) -> tuple[list[dict], int, int]:
        obj_str = ", ".join(visible_objects[:30]) if visible_objects else "none"
        prev_plan_text = json.dumps({"steps": prev_steps}, indent=None)
        user_msg = (
            (examples_block + "\n\n" if examples_block else "")
            + f"=== REPAIR SUB-GOAL ===\n"
            f"Overall task: {task}\n"
            f"Current sub-goal: {subgoal}\n\n"
            f"Robot symbolic state at start of sub-goal:\n  {state.as_text()}\n\n"
            f"Visible objects: {obj_str}\n"
            f"Scene observation:\n{obs}\n\n"
            f"Previous attempt:\n{prev_plan_text}\n\n"
            f"{feedback}"
        )
        raw, in_tok, out_tok = self._chat(
            [{"role": "system", "content": _REFINE_SYSTEM},
             {"role": "user",   "content": user_msg}],
            temperature=0.1,
        )
        return normalize_plan(parse_steps(raw)), in_tok, out_tok

    # ─────────────────────────────────────────────────────────────
    # Public API: generate_plan
    # ─────────────────────────────────────────────────────────────
    def generate_plan(
        self,
        task:            str,
        obs:             str,
        visible_objects: list[str],
    ) -> tuple[list[dict], PlanMetrics]:
        t0          = time.perf_counter()
        llm_calls   = 0
        total_in    = 0
        total_out   = 0
        refines     = 0
        verifier_rejections = 0

        # 1. Retrieve few-shot examples.
        retrieved, ex_block = self._retrieve_block(task)
        retrieved_tasks = [e.task for e in retrieved]

        # 2. Decompose.
        try:
            subgoals, in_d, out_d = self._decompose(task, obs, visible_objects, ex_block)
            llm_calls += 1
            total_in  += in_d
            total_out += out_d
        except Exception as e:
            print(f"[SAGE] Decompose error: {e} — falling back to single sub-goal")
            subgoals = [task]

        if not subgoals:
            subgoals = [task]
        if self.progress:
            try: self.progress({"phase": "decompose", "subgoals": list(subgoals)})
            except Exception: pass

        # 3. Expand each sub-goal under symbolic-state tracking.
        state = SymbolicState(visible=set(visible_objects))
        blocks: list[_SubgoalBlock] = []

        for _gi, sg in enumerate(subgoals):
            if self.progress:
                try: self.progress({"phase": "expand", "index": _gi,
                                    "total": len(subgoals), "subgoal": sg})
                except Exception: pass
            block = _SubgoalBlock(name=sg)
            try:
                steps, in_e, out_e = self._expand(
                    sg, task, state, obs, visible_objects, ex_block
                )
                llm_calls += 1
                total_in  += in_e
                total_out += out_e
            except Exception as e:
                print(f"[SAGE] Expand error on '{sg}': {e}")
                steps = []

            # Stream the expanded steps for this sub-goal (post-expansion, so a
            # UI can show the concrete plan under each sub-goal).
            if self.progress:
                try: self.progress({"phase": "expanded", "index": _gi,
                                    "subgoal": sg, "steps": _ser_steps(steps)})
                except Exception: pass

            # 4. Symbolic verification + (≤max_refines) refinements.
            if self.enable_verifier and steps:
                attempt = 0
                while attempt < self.max_refines:
                    report = self._simulate(steps, initial_state=state,
                                      visible_objects=visible_objects)
                    if report.ok:
                        break
                    verifier_rejections += 1
                    # Emit the violations that triggered this refinement.
                    if self.progress:
                        try: self.progress({"phase": "refine", "index": _gi,
                                            "subgoal": sg, "attempt": attempt + 1,
                                            "violations": _ser_violations(report)})
                        except Exception: pass
                    feedback = format_violations_for_llm(report, steps)
                    try:
                        new_steps, in_r, out_r = self._refine(
                            sg, task, steps, feedback,
                            state, obs, visible_objects, ex_block
                        )
                        llm_calls += 1
                        refines   += 1
                        total_in  += in_r
                        total_out += out_r
                        block.refined = True
                        if self.safe_refine and not new_steps:
                            # Refine returned empty (parse failure) — keep
                            # the prior steps rather than wiping them.
                            break
                        steps = new_steps
                        # Stream the steps produced by this refine pass.
                        if self.progress:
                            try: self.progress({"phase": "refined", "index": _gi,
                                                "subgoal": sg, "attempt": attempt + 1,
                                                "steps": _ser_steps(steps)})
                            except Exception: pass
                    except Exception as e:
                        print(f"[SAGE] Refine error on '{sg}': {e}")
                        break
                    attempt += 1

            block.steps = steps

            # Final per-sub-goal verification verdict on the committed steps
            # (re-simulate so it reflects the last refine, not a stale report).
            if self.progress and steps:
                try:
                    final = self._simulate(steps, initial_state=state, visible_objects=visible_objects)
                    self.progress({"phase": "verified", "index": _gi, "subgoal": sg,
                                   "ok": bool(final.ok),
                                   "violations": _ser_violations(final),
                                   "steps": _ser_steps(steps)})
                except Exception: pass

            # 5. Roll symbolic state forward by the (possibly imperfect) steps
            #    we are committing to.  Subsequent sub-goals see the
            #    post-condition.  Use stop_on_error=False so optimistic
            #    progress is preserved even if some violations remain.
            sim = self._simulate(steps, initial_state=state,
                           visible_objects=visible_objects,
                           stop_on_error=False)
            state = sim.final_state or state
            blocks.append(block)

        # 6. Flatten + dedupe.
        flat = _flatten(blocks)

        metrics = PlanMetrics(
            method        = self.name,
            model         = self.model,
            backend       = self.provider,
            latency_s     = time.perf_counter() - t0,
            llm_calls     = llm_calls,
            input_tokens  = total_in,
            output_tokens = total_out,
            num_steps     = len(flat),
            parse_ok      = bool(flat),
            extra         = {
                "subgoals":            [b.name for b in blocks],
                "retrieved_tasks":     retrieved_tasks,
                "refines":             refines,
                "verifier_rejections": verifier_rejections,
                "subgoal_block_sizes": [len(b.steps) for b in blocks],
                "refined_blocks":      [b.refined for b in blocks],
            },
        )
        self._last_blocks = blocks
        self._last_task   = task
        print(
            f"[SAGE] subgoals={len(blocks)} steps={len(flat)} "
            f"refines={refines} latency={metrics.latency_s:.1f}s "
            f"retrieved={retrieved_tasks}"
        )
        return flat, metrics

    # ─────────────────────────────────────────────────────────────
    # Public API: replan — LOCAL suffix repair
    # ─────────────────────────────────────────────────────────────
    def replan(
        self,
        task:            str,
        completed:       list[dict],
        failed_step:     dict,
        failure_reason:  str,
        obs:             str,
        visible_objects: list[str],
    ) -> tuple[list[dict], PlanMetrics]:
        t0 = time.perf_counter()
        completed = normalize_plan(completed)

        # If local repair is disabled or we have no prior decomposition,
        # fall back to a fresh hierarchical plan over the remainder
        # (same behavior as HierarchicalFewShotPlanner).
        if not self.enable_local_repair or not self._last_blocks or self._last_task != task:
            return self._global_replan(task, completed, failed_step,
                                       failure_reason, obs, visible_objects, t0)

        # Re-simulate completed steps to recover the symbolic state at
        # the moment of failure.  This is cheaper and more reliable than
        # passing observation strings to the LLM as "the world".
        state_at_failure = self._simulate(
            completed,
            visible_objects=visible_objects,
            stop_on_error=False,
        ).final_state or SymbolicState(visible=set(visible_objects))

        current_sg, remaining_after = self._locate_failed_subgoal(completed)
        retrieved, ex_block = self._retrieve_block(current_sg or task)

        # Build a single LLM call asking only for the suffix of the
        # current sub-goal — explicitly told what has been done and what
        # the symbolic state is.
        completed_desc = ", ".join(
            f"{s.get('action')} {s.get('object','')}" for s in completed[-6:]
        ) or "nothing yet"
        fs_desc = (
            f"{failed_step.get('action','?')} {failed_step.get('object','')}".strip()
        )
        obj_str = ", ".join(visible_objects[:30]) if visible_objects else "none"
        user_msg = (
            (ex_block + "\n\n" if ex_block else "")
            + f"=== LOCAL REPAIR ===\n"
            f"Overall task: {task}\n"
            f"Current sub-goal: {current_sg or task}\n\n"
            f"Last successful steps:\n  {completed_desc}\n\n"
            f"Failed step: {fs_desc}\n"
            f"Failure reason: {failure_reason}\n\n"
            f"Robot symbolic state right now:\n  {state_at_failure.as_text()}\n\n"
            f"Visible objects: {obj_str}\n"
            f"Scene observation:\n{obs}\n\n"
            "Generate ONLY the remaining steps to complete the CURRENT sub-goal."
        )

        try:
            raw, in_tok, out_tok = self._chat(
                [{"role": "system", "content": _REPAIR_SYSTEM},
                 {"role": "user",   "content": user_msg}],
                temperature=0.1,
            )
            suffix = normalize_plan(parse_steps(raw))
        except Exception as e:
            print(f"[SAGE] Local repair failed: {e}")
            suffix, in_tok, out_tok = [], 0, 0

        # Verify the suffix; one refinement pass if invalid.
        refines = 0
        verifier_rejections = 0
        if self.enable_verifier and suffix:
            report = self._simulate(suffix, initial_state=state_at_failure,
                              visible_objects=visible_objects)
            if not report.ok:
                verifier_rejections += 1
                feedback = format_violations_for_llm(report, suffix)
                try:
                    new_suffix, in_r, out_r = self._refine(
                        current_sg or task, task, suffix, feedback,
                        state_at_failure, obs, visible_objects, ex_block
                    )
                    in_tok  += in_r
                    out_tok += out_r
                    refines += 1
                    if not (self.safe_refine and not new_suffix):
                        suffix = new_suffix
                except Exception as e:
                    print(f"[SAGE] Repair-refine error: {e}")

        # Append the untouched future sub-goals so the executor still
        # has the rest of the plan.  If we don't have any, just return
        # the suffix.
        full_remaining = suffix + remaining_after

        metrics = PlanMetrics(
            method        = self.name,
            model         = self.model,
            backend       = self.provider,
            latency_s     = time.perf_counter() - t0,
            llm_calls     = (1 if suffix or in_tok else 0) + refines,
            input_tokens  = in_tok,
            output_tokens = out_tok,
            num_steps     = len(full_remaining),
            parse_ok      = bool(full_remaining),
            notes         = "local_repair",
            extra         = {
                "repaired_subgoal":    current_sg,
                "refines":             refines,
                "verifier_rejections": verifier_rejections,
                "suffix_len":          len(suffix),
                "kept_future_len":     len(remaining_after),
            },
        )
        print(
            f"[SAGE] local_repair sg='{current_sg}' suffix={len(suffix)} "
            f"future={len(remaining_after)} refines={refines} "
            f"latency={metrics.latency_s:.1f}s"
        )
        return full_remaining, metrics

    # ─────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────
    def _locate_failed_subgoal(
        self,
        completed: list[dict],
    ) -> tuple[str | None, list[dict]]:
        """Walk the last decomposition to find which sub-goal the failure
        belongs to, and return (subgoal_name, steps_after_current_subgoal).

        Approach: each block contributed `len(block.steps)` steps; walk
        the completed-counter through the blocks. The first block whose
        cumulative count exceeds the completed length is the active one.
        """
        if not self._last_blocks:
            return None, []

        flat = _flatten(self._last_blocks)
        # Map completed count → block index via cumulative sizes of the
        # flattened (deduped) plan.  We approximate by matching positions
        # in the flat plan: completed_count clamped to len(flat).
        n_done = min(len(completed), len(flat))

        cum = 0
        cur_idx = 0
        for i, b in enumerate(self._last_blocks):
            cum += len(b.steps)
            if n_done < cum:
                cur_idx = i
                break
        else:
            cur_idx = len(self._last_blocks) - 1

        cur_block = self._last_blocks[cur_idx]
        future_blocks = self._last_blocks[cur_idx + 1:]
        future_steps  = _flatten(future_blocks)
        return cur_block.name, future_steps

    def _global_replan(
        self,
        task:            str,
        completed:       list[dict],
        failed_step:     dict,
        failure_reason:  str,
        obs:             str,
        visible_objects: list[str],
        t0:              float,
    ) -> tuple[list[dict], PlanMetrics]:
        """Fallback path: same logic as HierarchicalFewShot.replan()."""
        completed_desc = ", ".join(
            f"{s.get('action')} {s.get('object','')}" for s in completed
        ) or "nothing yet"
        remaining_task = (
            f"{task}. Already done: {completed_desc}. "
            f"Last action failed "
            f"({failed_step.get('action','')} {failed_step.get('object','')}): "
            f"{failure_reason}. Generate only the remaining steps."
        )
        steps, metrics = self.generate_plan(remaining_task, obs, visible_objects)
        metrics.method = self.name
        metrics.notes  = "global_replan_fallback"
        # Adjust latency to include the wrapper time.
        metrics.latency_s = time.perf_counter() - t0
        return steps, metrics

    # ─────────────────────────────────────────────────────────────
    # Episode logging (hook for carerobotagent / benchmark scripts)
    # ─────────────────────────────────────────────────────────────
    def record_episode(self, task: str, plan: list[dict], success: bool = True) -> None:
        """Append a successful trajectory to the live memory pool."""
        if self.memory is not None:
            self.memory.add_episode(task=task, plan=plan, success=success)


__all__ = ["SAGEPlanner", "SubgoalBlock", "flatten"]
