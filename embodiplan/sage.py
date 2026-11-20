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

import inspect
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from embodiplan.base import (
    ACTIONS_STR, JSON_EXAMPLE, STEP_SCHEMA, ROBOT_ACTIONS,
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
from embodiplan.robot_prompt import system_prompts


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


_DEFAULT_SYSTEM = {"decompose": _DECOMPOSE_SYSTEM, "expand": _EXPAND_SYSTEM,
                   "refine": _REFINE_SYSTEM, "repair": _REPAIR_SYSTEM}


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


class _BudgetStop(Exception):
    """The caller's LLM-call budget refused the next call (``generate_plan(budget=)``): SAGE stops
    between two calls and the budget's own exception (``cause``) reaches the caller."""

    def __init__(self, cause: BaseException):
        super().__init__(str(cause))
        self.cause = cause


# ─────────────────────────────────────────────────────────────────────
# Internal data structures
# ─────────────────────────────────────────────────────────────────────
@dataclass
class _SubgoalBlock:
    """One sub-goal and the steps that implement it."""
    name:    str
    steps:   list[dict] = field(default_factory=list)
    refined: bool       = False   # was at least one refine pass triggered?
    refine_rejected: bool = False # a refine output failed re-verification (prior steps kept)
    fixed:   bool       = False   # the caller's own steps (no expand, no refine, no LLM edit)


# keys a model wraps sub-goals in: never a sub-goal themselves
_SUBGOAL_KEYS = {"subgoals", "subgoal", "sub_goals", "steps", "action", "object", "name", "description"}


def _subgoal_text(item) -> str:
    """One sub-goal as text: a string; a step-shaped dict ``{"action", "object"}`` or a dict with a
    ``subgoal`` / ``name`` / ``description``; a list of words (``["MoveTo", "desk"]``)."""
    if isinstance(item, dict):
        for k in ("subgoal", "name", "description", "goal", "text"):
            if isinstance(item.get(k), str) and item[k].strip():
                return item[k].strip()
        a, o = item.get("action"), item.get("object")
        return f"{a} {o or ''}".strip() if a else ""
    if isinstance(item, (list, tuple)):
        return " ".join(str(x).strip() for x in item if str(x).strip())
    return str(item).strip()


def _pair_actions(items: list, actions) -> list:
    """Sub-goals a model split into words ("MoveTo", "desk@study room", "Find", "apple", ...) joined
    back to one per step ("MoveTo desk@study room", "Find apple"): a bare action name takes the item
    after it when that item is not an action name itself. Phrases pass unchanged."""
    names, out = set(actions or ()), []
    for it in items:
        if out and out[-1] in names and it not in names:
            out[-1] = f"{out[-1]} {it}"
        else:
            out.append(it)
    return out


def _subgoal_data(raw: str):
    """The model's JSON: the whole answer, else the first JSON value in it (a model that wrote two
    objects, or prose around one); None when there is none."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", raw):
        try:
            return dec.raw_decode(raw[m.start():])[0]
        except json.JSONDecodeError:
            continue
    return None


def _parse_subgoals(raw: str, actions=ROBOT_ACTIONS) -> list[str]:
    """The decompose answer as sub-goal phrases. 7-8B models also answer with step-shaped items
    (``{"action": "MoveTo", "object": ...}``), with the steps split into words, or with two JSON
    objects in a row; each becomes one sub-goal per step (``actions``: the action names), never one
    per word or JSON key (a 22-block plan of one-word sub-goals, T2 H01)."""
    raw = re.sub(r"```json|```", "", raw).strip()
    data = _subgoal_data(raw)
    if isinstance(data, dict):
        data = next((data[k] for k in ("subgoals", "sub_goals", "steps") if isinstance(data.get(k), list)), [])
    if isinstance(data, list):
        items = [_subgoal_text(s) for s in data]
    else:                                        # no JSON: the quoted strings that are not keys
        items = [t.strip() for t, key in re.findall(r'"((?:[^"\\]|\\.)*)"\s*(:?)', raw) if not key]
        items = [t for t in items if len(t) > 1 and t.lower() not in _SUBGOAL_KEYS]
    return _pair_actions([t for t in items if t], actions)


# Public name of the block type (robot_agent drives sub-goal edits on it).
SubgoalBlock = _SubgoalBlock


def _given_subgoal(item) -> tuple[str, list[dict] | None]:
    """(text, own steps or None) of one caller-given sub-goal: a string, ``(text, steps)`` or
    ``{"name": text, "steps": steps}``."""
    if isinstance(item, dict) and isinstance(item.get("steps"), list) and item.get("name") is not None:
        return str(item["name"]), [dict(x) for x in item["steps"]]
    if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], list):
        return str(item[0]), [dict(x) for x in item[1]]
    return str(item), None


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
    return _flatten_owned(blocks)[0]


def _flatten_owned(blocks: list[_SubgoalBlock]) -> tuple[list[dict], list[int]]:
    """:func:`flatten` plus, for every kept step, the index of its block."""
    from embodiplan.action_model import SymbolicState, apply
    out: list[dict] = []
    owners: list[int] = []
    state = SymbolicState()
    for bi, b in enumerate(blocks):
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
            owners.append(bi)
            apply(s, state, inplace=True)
    return out, owners


_flatten = flatten  # pre-0.2 private name, still used by older robot_agent


def _blocks_sig(blocks) -> tuple:
    """What the blocks plan, to see that someone (robot_agent's edit) changed them."""
    return tuple(tuple((s.get("action"), s.get("object")) for s in b.steps) for b in blocks or ())


_BELIEF_KEYS = re.compile(r"\b(arrived|found|holding|opened|on)\s*[=:]\s*")
_NO_VALUE = {"", "-", "∅", "none", "None", "nothing", "set()"}


def belief_from_obs(obs: str) -> SymbolicState | None:
    """The robot's belief from the last ``Robot state: ...`` line of an observation
    (robot_agent's ``WorldState.as_text()`` ``arrived=X found=- holding=- opened=[A, B] on=[-]``
    or ``SymbolicState.as_text()``); None when the observation has no such line."""
    lines = [l for l in (obs or "").splitlines() if "Robot state:" in l]
    if not lines:
        return None
    parts = _BELIEF_KEYS.split(lines[-1].split("Robot state:", 1)[1])
    vals = {k: v.strip().strip("|").strip() for k, v in zip(parts[1::2], parts[2::2])}
    if not vals:
        return None

    def one(k):
        v = vals.get(k, "").strip("'\"")
        return None if v in _NO_VALUE else v

    def many(k):
        items = (x.strip().strip("'\"") for x in vals.get(k, "").strip().strip("[]{}").split(","))
        return {x for x in items if x not in _NO_VALUE}

    return SymbolicState(arrived=one("arrived"), found=one("found"), holding=one("holding"),
                         opened=many("opened"), on=many("on"))


def _as_state(world) -> SymbolicState:
    """A SymbolicState copy of a duck-typed world (SymbolicState, robot_agent's WorldState, dict)."""
    get = world.get if isinstance(world, dict) else (lambda k, d=None: getattr(world, k, d))
    return SymbolicState(arrived=get("arrived"), found=get("found"), holding=get("holding"),
                         opened=set(get("opened") or ()), on=set(get("on") or ()),
                         visible=set(get("visible") or ()))


def _n_errors(report) -> int:
    return len(getattr(report, "violations", []) or [])


# ─────────────────────────────────────────────────────────────────────
# SAGEPlanner
# ─────────────────────────────────────────────────────────────────────
class SAGEPlanner(BasePlanner):
    name        = "SAGE"
    fixed_subgoals = True      # generate_plan(subgoals=[(text, steps)]) keeps the caller's own steps (0.2.7)
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
        leave_one_out:   bool = False,
        actions:         list[str] | None = None,
        robot_description: str | None = None,
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
        # The robot's own actions (names or "Name <arg>: what it does" lines) and
        # body description replace the household defaults in the system prompts
        # (embodiplan.robot_prompt); None keeps the default prompts unchanged.
        self.actions           = list(actions) if actions is not None else None
        self.robot_description = robot_description
        self.system_prompts    = system_prompts(_DEFAULT_SYSTEM, self.actions, robot_description)

        if memory is not None:
            self.memory = memory
        elif enable_memory:
            self.memory = MemoryRetriever(MemoryRetrieverConfig(
                gt_path     = gt_path,
                live_path   = live_path,
                use_chroma  = use_chroma,
                chroma_path = chroma_path,
                top_k       = top_k,
                leave_one_out = leave_one_out,
            ))
        else:
            self.memory = None  # type: ignore[assignment]

        # Track the last decomposition so .replan can localize the failure.
        self._last_blocks: list[_SubgoalBlock] | None = None
        self._last_task:   str                       = ""
        # Which block every step of the plan being executed belongs to (the
        # plan = completed + what generate_plan / replan returned), for the
        # blocks whose signature is _owners_sig; recomputed by flatten when
        # the blocks were edited from outside (robot_agent's sub-goal edit).
        self._owners:      list[int] | None          = None
        self._owners_sig:  tuple                     = ()
        # Belief at the start of the plan and the number of completed steps
        # before that start (> 0 after a global fallback replan).
        self._init_state:  SymbolicState | None      = None
        self._init_offset: int                       = 0

    def _chat(self, messages, temperature: float = 0.2):
        """One LLM call; with a caller budget (``generate_plan`` / ``replan`` ``budget=``) its
        ``take()`` runs first, and a refusal stops the planner before the call (``_BudgetStop``)."""
        budget = getattr(self, "_budget", None)
        if budget is not None:
            try:
                budget.take(what="planner")
            except Exception as e:                      # the caller's own exception type
                raise _BudgetStop(e) from e
        return super()._chat(messages, temperature=temperature)

    def _action_names(self) -> list:
        """The action names this planner plans with (the robot's, else the default vocabulary)."""
        from embodiplan.robot_prompt import action_names
        return action_names(self.actions) if getattr(self, "actions", None) else list(ROBOT_ACTIONS)

    def _system(self, key: str) -> str:
        """The system prompt ``key`` (decompose / expand / refine / repair) for this robot."""
        prompts = getattr(self, "system_prompts", None) or _DEFAULT_SYSTEM
        return prompts.get(key) or _DEFAULT_SYSTEM[key]

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
        if getattr(block, "fixed", False):
            return list(block.steps)              # the caller's own steps: the LLM does not rewrite them
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
            [{"role": "system", "content": self._system("decompose")},
             {"role": "user",   "content": user_msg}],
            temperature=0.3,
        )
        return _parse_subgoals(raw, self._action_names()), in_tok, out_tok

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
            f"Current sub-goal: {subgoal}\n"
            + ("Plan only this sub-goal; the earlier sub-goals are done (see the state).\n"
               if getattr(self, "_given_subgoals", False) else "")
            + "\n"
            f"Robot symbolic state:\n  {state.as_text()}\n\n"
            f"Scene observation:\n{obs}\n\n"
            f"Visible objects: {obj_str}\n\n"
            "Expand this sub-goal into action steps:"
        )
        raw, in_tok, out_tok = self._chat(
            [{"role": "system", "content": self._system("expand")},
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
            [{"role": "system", "content": self._system("refine")},
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
        *,
        state:           Any = None,
        subgoals:        list[str] | None = None,
        budget:          Any = None,
    ) -> tuple[list[dict], PlanMetrics]:
        """Plan `task`. The symbolic gate starts from `state` (a SymbolicState
        or a duck-typed world), else from the ``Robot state:`` line of `obs`,
        else from an empty state. ``subgoals``: the caller's sub-goals (one per
        goal it understood): no decompose call, one expand per sub-goal, each
        told the earlier ones are done. ``budget``: an object whose ``take()``
        runs before every LLM call; its exception stops the planner between two
        calls and is raised as is. Defaults keep the 0.2.5 behaviour."""
        self._budget, self._given_subgoals = budget, bool(subgoals)
        try:
            return self._generate_plan(task, obs, visible_objects, state, subgoals)
        except _BudgetStop as e:
            raise e.cause from None
        finally:
            self._budget, self._given_subgoals = None, False

    def _generate_plan(self, task, obs, visible_objects, state, given) -> tuple[list[dict], PlanMetrics]:
        t0          = time.perf_counter()
        llm_calls   = 0
        total_in    = 0
        total_out   = 0
        refines     = 0
        verifier_rejections = 0

        # 1. Retrieve few-shot examples.
        retrieved, ex_block = self._retrieve_block(task)
        retrieved_tasks = [e.task for e in retrieved]

        # 2. Decompose (not when the caller gave the sub-goals). A given sub-goal may carry its own steps
        #    (``(text, steps)`` or ``{"name", "steps"}``): a fixed block, written by the caller, never by the LLM.
        fixed_steps: dict[int, list[dict]] = {}
        if given:
            subgoals = []
            for g in given:
                name, own = _given_subgoal(g)
                if not name.strip():
                    continue
                if own is not None:
                    fixed_steps[len(subgoals)] = own
                subgoals.append(name)
        else:
            try:
                subgoals, in_d, out_d = self._decompose(task, obs, visible_objects, ex_block)
                llm_calls += 1
                total_in  += in_d
                total_out += out_d
            except _BudgetStop:
                raise
            except Exception as e:
                print(f"[SAGE] Decompose error: {e} — falling back to single sub-goal")
                subgoals = [task]

        if not subgoals:
            subgoals = [task]
        if self.progress:
            try: self.progress({"phase": "decompose", "subgoals": list(subgoals)})
            except Exception: pass

        # 3. Expand each sub-goal under symbolic-state tracking, from the belief.
        start = state if state is not None else belief_from_obs(obs)
        init_state = _as_state(start) if start is not None else SymbolicState()
        init_state.visible |= set(visible_objects)
        state = init_state.copy()
        blocks: list[_SubgoalBlock] = []

        for _gi, sg in enumerate(subgoals):
            if self.progress:
                try: self.progress({"phase": "expand", "index": _gi,
                                    "total": len(subgoals), "subgoal": sg})
                except Exception: pass
            block = _SubgoalBlock(name=sg)
            if _gi in fixed_steps:
                block.fixed = True
                steps = [dict(x) for x in fixed_steps[_gi]]
            else:
                try:
                    steps, in_e, out_e = self._expand(
                        sg, task, state, obs, visible_objects, ex_block
                    )
                    llm_calls += 1
                    total_in  += in_e
                    total_out += out_e
                except _BudgetStop:
                    raise
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
            if self.enable_verifier and steps and not block.fixed:
                attempt = 0
                report = self._simulate(steps, initial_state=state,
                                        visible_objects=visible_objects)
                while attempt < self.max_refines and not report.ok:
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
                        # Re-verify the refined steps before using them: an
                        # edit with more violations than its input is dropped.
                        new_report = self._simulate(new_steps, initial_state=state,
                                                    visible_objects=visible_objects)
                        if self._worse(new_steps, new_report, steps, state, visible_objects):
                            block.refine_rejected = True
                            break
                        steps, report = new_steps, new_report
                        # Stream the steps produced by this refine pass.
                        if self.progress:
                            try: self.progress({"phase": "refined", "index": _gi,
                                                "subgoal": sg, "attempt": attempt + 1,
                                                "steps": _ser_steps(steps)})
                            except Exception: pass
                    except _BudgetStop:
                        raise
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
                "refine_rejected":     [b.refine_rejected for b in blocks],
            },
        )
        self._remember(task, blocks, _flatten_owned(blocks)[1], init_state, 0)
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
        *,
        state:           Any = None,
        budget:          Any = None,
    ) -> tuple[list[dict], PlanMetrics]:
        """Repair the sub-goal the failed step belongs to. The repair starts
        from the belief at the failure: `state` when given, else the
        ``Robot state:`` line of `obs`, else the plan's starting belief
        replayed through `completed`. ``budget``: as in :meth:`generate_plan`."""
        self._budget = budget
        try:
            return self._replan(task, completed, failed_step, failure_reason, obs, visible_objects, state)
        except _BudgetStop as e:
            raise e.cause from None
        finally:
            self._budget = None

    def _replan(self, task, completed, failed_step, failure_reason, obs, visible_objects, state):
        t0 = time.perf_counter()
        completed = normalize_plan(completed)

        # If local repair is disabled or we have no prior decomposition,
        # fall back to a fresh hierarchical plan over the remainder
        # (same behavior as HierarchicalFewShotPlanner).
        # The symbolic state at the moment of failure (see the docstring);
        # cheaper and more reliable than the LLM reading the obs as "the world".
        state_at_failure = self._state_at_failure(completed, visible_objects, state, obs)
        if not self.enable_local_repair or not self._last_blocks or self._last_task != task:
            return self._global_replan(task, completed, failed_step, failure_reason, obs,
                                       visible_objects, t0, state_at_failure)

        cur_idx = self._failed_block_index(len(completed))
        current_sg = self._last_blocks[cur_idx].name
        future_blocks = self._last_blocks[cur_idx + 1:]
        remaining_after, future_owners = _flatten_owned(future_blocks)
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
                [{"role": "system", "content": self._system("repair")},
                 {"role": "user",   "content": user_msg}],
                temperature=0.1,
            )
            suffix = normalize_plan(parse_steps(raw))
        except _BudgetStop:
            raise
        except Exception as e:
            print(f"[SAGE] Local repair failed: {e}")
            suffix, in_tok, out_tok = [], 0, 0

        # Verify the suffix; one refinement pass if invalid.
        refines = 0
        verifier_rejections = 0
        refine_rejected = False
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
                        # Re-verify before use (as in generate_plan).
                        new_report = self._simulate(new_suffix, initial_state=state_at_failure,
                                                    visible_objects=visible_objects)
                        if self._worse(new_suffix, new_report, suffix, state_at_failure,
                                       visible_objects):
                            refine_rejected = True
                        else:
                            suffix = new_suffix
                except _BudgetStop:
                    raise
                except Exception as e:
                    print(f"[SAGE] Repair-refine error: {e}")

        # Append the untouched future sub-goals so the executor still
        # has the rest of the plan.  If we don't have any, just return
        # the suffix.
        full_remaining = suffix + remaining_after
        self._remember_repair(completed, cur_idx, suffix, remaining_after, future_owners)

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
                "refine_rejected":     refine_rejected,
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
        """Which sub-goal the failure belongs to, and the (flattened) steps of
        the sub-goals after it: (subgoal_name, steps_after_current_subgoal)."""
        if not self._last_blocks:
            return None, []
        k = self._failed_block_index(len(completed))
        return self._last_blocks[k].name, _flatten(self._last_blocks[k + 1:])

    def _remember(self, task, blocks, owners, init_state, init_offset) -> None:
        """Book-keep the plan being executed (see __init__)."""
        self._last_blocks = blocks
        self._last_task   = task
        self._owners      = list(owners)
        self._owners_sig  = _blocks_sig(blocks)
        self._init_state  = init_state
        self._init_offset = init_offset

    def _current_owners(self) -> list[int]:
        """Block index of every plan step: the book-kept one, or flatten's
        when the blocks were changed since (positions are post-dedupe)."""
        if self._owners is None or _blocks_sig(self._last_blocks) != self._owners_sig:
            self._owners = _flatten_owned(self._last_blocks)[1]
            self._owners_sig = _blocks_sig(self._last_blocks)
        return self._owners

    def _failed_block_index(self, n_done: int) -> int:
        """The block of the step after the `n_done` completed ones (the failed step)."""
        owners = self._current_owners()
        if n_done < len(owners):
            return owners[n_done]
        return owners[-1] if owners else len(self._last_blocks) - 1

    def _state_at_failure(self, completed, visible_objects, state=None, obs="") -> SymbolicState:
        if state is None:
            state = belief_from_obs(obs)
        if state is not None:
            out = _as_state(state)
            out.visible |= set(visible_objects)
            return out
        init = self._init_state.copy() if self._init_state is not None else SymbolicState()
        init.visible |= set(visible_objects)
        return self._simulate(completed[self._init_offset:], initial_state=init,
                              visible_objects=visible_objects,
                              stop_on_error=False).final_state or init

    def _remember_repair(self, completed, cur_idx, suffix, future, future_owners) -> None:
        """Write a local repair back into the blocks: the done steps stay with
        their blocks, the current block ends with the repaired suffix, the
        later blocks keep their (flattened) steps. The plan being executed is
        now completed + suffix + future, and the next failure is located on it."""
        owners = self._current_owners()
        done_owner = [owners[i] if i < len(owners) else cur_idx for i in range(len(completed))]
        done_owner = [min(o, cur_idx) for o in done_owner]
        fut = [o + cur_idx + 1 for o in future_owners]
        new_blocks = []
        for j, b in enumerate(self._last_blocks):
            if j < cur_idx:
                steps = [s for s, o in zip(completed, done_owner) if o == j]
            elif j == cur_idx:
                steps = [s for s, o in zip(completed, done_owner) if o == j] + list(suffix)
            else:
                steps = [s for s, o in zip(future, fut) if o == j]
            new_blocks.append(_SubgoalBlock(name=b.name, steps=steps, refined=b.refined,
                                            refine_rejected=b.refine_rejected, fixed=b.fixed))
        self._remember(self._last_task, new_blocks,
                       done_owner + [cur_idx] * len(suffix) + fut,
                       self._init_state, self._init_offset)

    def _worse(self, new_steps, new_report, old_steps, state, visible_objects) -> bool:
        """True when a refine output re-verifies with more errors than its input."""
        if new_report.ok:
            return False
        old = self._simulate(old_steps, initial_state=state, visible_objects=visible_objects,
                             stop_on_error=False, apply_on_error=True)
        new = self._simulate(new_steps, initial_state=state, visible_objects=visible_objects,
                             stop_on_error=False, apply_on_error=True)
        return _n_errors(new) > _n_errors(old)

    def _global_replan(
        self,
        task:            str,
        completed:       list[dict],
        failed_step:     dict,
        failure_reason:  str,
        obs:             str,
        visible_objects: list[str],
        t0:              float,
        state:           SymbolicState | None = None,
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
        steps, metrics = self._generate_plan(remaining_task, obs, visible_objects, state, None)
        # The executor's plan is now completed + steps, still for `task`: keep
        # `task` as the plan's task so the next replan(task) is local again,
        # and start the new plan's belief after the completed steps.
        if self._last_blocks is not None:
            owners = self._current_owners()
            self._remember(task, self._last_blocks, [0] * len(completed) + owners,
                           self._init_state, len(completed))
        metrics.method = self.name
        metrics.notes  = "global_replan_fallback"
        # Adjust latency to include the wrapper time.
        metrics.latency_s = time.perf_counter() - t0
        return steps, metrics

    # ─────────────────────────────────────────────────────────────
    # Episode logging (hook for carerobotagent / benchmark scripts)
    # ─────────────────────────────────────────────────────────────
    def record_episode(self, task: str, plan: list[dict], success: bool = True,
                       *, meta: dict | None = None) -> bool:
        """Append a successful trajectory to the live memory pool, only when it
        verifies: `success` and the plan passes the symbolic gate (from the
        plan's starting belief when `task` is the last planned task). A
        trajectory that does not verify would teach the next plans its error.
        The record carries ``verified: true``, its start belief (``init_state``,
        what ``memory_tools clean`` re-checks the plan from), the action-model
        version and the caller's `meta` (e.g. how the goals were verified).
        Returns True when recorded."""
        if self.memory is None or not success or not plan:
            return False
        init, offset = SymbolicState(), 0
        if task == self._last_task and self._init_state is not None:
            init, offset = self._init_state.copy(), self._init_offset
        if not self._simulate(normalize_plan(plan)[offset:], initial_state=init).ok:
            print(f"[SAGE] episode not recorded (plan does not verify): {task!r}")
            return False
        from embodiplan.action_model import ACTION_MODEL_VERSION
        from embodiplan.memory_tools import state_dict
        # the belief at the plan's start (after a global fallback it is the belief after
        # the first `offset` steps: not stored, the whole plan is then re-checked from empty)
        rec = {"verified": True, "action_model": ACTION_MODEL_VERSION, **(meta or {})}
        if offset == 0:
            rec.setdefault("init_state", state_dict(init))
        add = self.memory.add_episode
        try:
            takes_meta = "meta" in inspect.signature(add).parameters
        except (TypeError, ValueError):
            takes_meta = False
        # a memory passed in (duck-typed, older signature) gets the plain call
        add(task=task, plan=plan, success=success, **({"meta": rec} if takes_meta else {}))
        return True


__all__ = ["SAGEPlanner", "SubgoalBlock", "flatten", "belief_from_obs"]
