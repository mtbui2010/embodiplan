# embodiplan/action_model.py
# ─────────────────────────────────────────────────────────────────────
# The action model: the ONE public, versioned source of the symbolic
# action rules (preconditions + effects) used by SAGE's gate, the SayCan
# affordance, the evaluation gate and, through this API, by robot_agent's
# plan checker, per-step guard and post-step symbolic verify.
#
#     check(step, world)     -> list[Violation]   (errors + warnings)
#     apply(step, world)     -> world             (effect of a successful step)
#     simulate(plan, world)  -> VerifierReport    (check + apply over a plan)
#
# `world` is duck-typed: any object with the five fields `arrived`,
# `found`, `holding`, `opened` (set), `on` (set) — SymbolicState here,
# robot_agent's WorldState on the robot side.
#
# Rule changes bump ACTION_MODEL_VERSION and are listed in CHANGELOG.md.
# The semantics per edge case are pinned by tests/test_action_model.py.
# ─────────────────────────────────────────────────────────────────────

from __future__ import annotations

import copy as _copy
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Union

from embodiplan.base import ROBOT_ACTIONS

ACTION_MODEL_VERSION = "1.0"

ERROR   = "error"
WARNING = "warning"

# Common containers that gate access to objects inside them (AI2-THOR names).
DEFAULT_CONTAINERS = {
    "Fridge", "Cabinet", "Drawer", "Microwave", "Oven",
    "Safe", "Box", "GarbageCan",
}

# Actions whose target must be within reach (at it, or found when Find reaches).
_REACH_ACTIONS  = {"Open", "Close", "TurnOn", "TurnOff", "Wash"}
# Of those, the ones that grasp a handle: they need a free hand.
_HANDLE_ACTIONS = {"Open", "Close"}
_PLACE_ACTIONS  = {"Place", "PutIn"}
# Cross-domain object-treatment actions (ALFWorld heat/cool/clean), only present
# in ROBOT_ACTIONS when opted in via EMBODIPLAN_EXTRA_ACTIONS. They act on the held
# object (precondition: holding) and leave it in hand.
_TREAT_ACTIONS  = {"Heat", "Cool", "Clean"}

Names = Union[Iterable[str], Callable[[str], bool], None]


# ─────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Capabilities:
    """What the robot's body allows; the rules that depend on it read it here.

    hands:        number of hands. With one hand, Open / Close need it free.
    find_reaches: True when Find brings the target within reach (AI2-THOR:
                  Find navigates to it), so a found handle can be opened
                  without a MoveTo. False for a robot whose Find only looks.
    """
    hands:        int  = 1
    find_reaches: bool = True

    @classmethod
    def from_any(cls, caps: "Capabilities | Mapping | None") -> "Capabilities":
        """Accept a Capabilities, a manifest ``capabilities`` dict
        (``hands: 1`` or ``hands: [right]``; ``arms`` as fallback) or None."""
        if caps is None:
            return DEFAULT_CAPABILITIES
        if isinstance(caps, cls):
            return caps
        hands = caps.get("hands", caps.get("arms", 1))
        if isinstance(hands, (list, tuple, set)):
            hands = len(hands)
        return cls(hands=int(hands or 0),
                   find_reaches=bool(caps.get("find_reaches", True)))


DEFAULT_CAPABILITIES = Capabilities()


@dataclass
class Violation:
    """One rule finding for one step."""
    code:     str            # short machine-readable tag, stable across versions
    reason:   str            # human-readable explanation
    severity: str = ERROR    # "error" or "warning"

    @property
    def is_error(self) -> bool:
        return self.severity == ERROR


@dataclass
class SymbolicState:
    """Tracks robot symbolic state across plan simulation.

    Fields mirror the implicit variables documented in STEP_SCHEMA:
        arrived  : last MoveTo destination (room or furniture)
        found    : last Find target (interactable handle)
        holding  : object currently in the gripper, or None
        opened   : set of currently-opened containers
        on       : set of currently-turned-on appliances
    """
    arrived: str | None       = None
    found:   str | None       = None
    holding: str | None       = None
    opened:  set[str]         = field(default_factory=set)
    on:      set[str]         = field(default_factory=set)
    visible: set[str]         = field(default_factory=set)

    def copy(self) -> "SymbolicState":
        return SymbolicState(
            arrived=self.arrived,
            found=self.found,
            holding=self.holding,
            opened=set(self.opened),
            on=set(self.on),
            visible=set(self.visible),
        )

    def as_text(self) -> str:
        """Human-readable snapshot for prompts."""
        return (
            f"arrived: {self.arrived or '∅'} | "
            f"found: {self.found or '∅'} | "
            f"holding: {self.holding or '∅'} | "
            f"opened: {sorted(self.opened) or '∅'} | "
            f"on: {sorted(self.on) or '∅'}"
        )


@dataclass
class StepViolation:
    """One precondition violation report."""
    index:    int
    step:     dict
    code:     str    # short machine-readable tag
    reason:   str    # human-readable explanation
    severity: str = ERROR   # "error" or "warning"


@dataclass
class VerifierReport:
    """Aggregate result of simulating a plan against SymbolicState.

    ``violations`` holds the errors only (so ``ok`` keeps its meaning);
    warnings are in ``warnings``."""
    ok:          bool
    violations:  list[StepViolation] = field(default_factory=list)
    final_state: SymbolicState | None = None
    num_steps:   int                  = 0
    warnings:    list[StepViolation] = field(default_factory=list)

    def first_error_index(self) -> int | None:
        for v in self.violations:
            if v.severity == ERROR:
                return v.index
        return None

    def summary(self, limit: int = 5) -> str:
        if self.ok:
            return f"OK ({self.num_steps} steps verified)"
        head = self.violations[:limit]
        body = "\n".join(
            f"  step {v.index+1}: [{v.code}] {v.reason} "
            f"({v.step.get('action')} {v.step.get('object','')})".rstrip()
            for v in head
        )
        more = f"\n  ... +{len(self.violations) - limit} more" if len(self.violations) > limit else ""
        return f"FAIL ({len(self.violations)} violation(s)):\n{body}{more}"


# ─────────────────────────────────────────────────────────────────────
# Step normalization
# ─────────────────────────────────────────────────────────────────────
# Legacy aliases that appear in eval_dataset_gt.json and external sources.
_ALIAS_MAP = {
    "navigate": "MoveTo",
    "moveto":   "MoveTo",
    "goto":     "MoveTo",
    "grab":     "Pick",
    "pickup":   "Pick",
    "pick":     "Pick",
    "place":    "Place",
    "put":      "Place",
    "putin":    "PutIn",
    "open":     "Open",
    "close":    "Close",
    "turnon":   "TurnOn",
    "turnoff":  "TurnOff",
    "wash":     "Wash",
    "find":     "Find",
    "search":   "Find",
    "locate":   "Find",
    "sit":      "Sit",
    "lieon":    "LieOn",
    "serve":    "Serve",
    "wait":     "Wait",
}


def normalize_action(action: str) -> str:
    """Map legacy / dataset action names to canonical ROBOT_ACTIONS."""
    if not action:
        return ""
    key = action.replace(" ", "").replace("_", "").lower()
    return _ALIAS_MAP.get(key, action)


def normalize_step(step: dict) -> dict:
    """Return a copy of step with action normalized."""
    out = dict(step)
    out["action"] = normalize_action(step.get("action", ""))
    return out


def normalize_plan(steps: Iterable[dict]) -> list[dict]:
    return [normalize_step(s) for s in steps]


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def _member(name: str, names: Names) -> bool:
    """`name` in a name set (exact, case-insensitive, or the head word of a
    robot place key such as 'drawer@bedroom' / 'kitchen drawer'), or a predicate."""
    if not name or names is None:
        return False
    if callable(names):
        return bool(names(name))
    names = set(names)
    if name in names:
        return True
    low = {n.lower() for n in names}
    head = name.split("@")[0].strip().lower()
    return name.lower() in low or head in low or any(t in low for t in re.split(r"[^0-9a-z]+", head) if t)


def is_container(name: str, containers: Names = None) -> bool:
    """Does `name` gate access to its inside (must be open to put things in)?"""
    return _member(name, DEFAULT_CONTAINERS if containers is None else containers)


def _target(step: dict) -> str:
    return (step.get("object") or "").strip()


def _reachable(o: str, world, caps: Capabilities) -> bool:
    return world.arrived == o or (caps.find_reaches and world.found == o)


# ─────────────────────────────────────────────────────────────────────
# check: the preconditions
# ─────────────────────────────────────────────────────────────────────
def check(
    step:          dict,
    world,
    *,
    capabilities:  "Capabilities | Mapping | None" = None,
    containers:    Names = None,
    openable:      Names = None,
    object_places: Mapping[str, str] | None = None,
    actions:       Iterable[str] | None = None,
) -> list[Violation]:
    """All rule findings for `step` in `world` (world is not changed).

    Args:
        capabilities:  Capabilities / manifest dict; default one hand,
                       Find reaches.
        containers:    names (or predicate) of things that must be open to put
                       something in; default DEFAULT_CONTAINERS.
        openable:      names (or predicate) of what Open / Close may target.
                       None = not checked (open vocabulary).
        object_places: belief {object: place} — Find X elsewhere is an error.
        actions:       the action vocabulary; default ROBOT_ACTIONS.

    Returns errors first-cause first (``verify_step`` reports the first one),
    then warnings. An empty list means admissible.
    """
    caps = Capabilities.from_any(capabilities)
    a = normalize_action(step.get("action", "") or "")
    o = _target(step)
    out: list[Violation] = []
    err = lambda code, reason: out.append(Violation(code, reason))
    warn = lambda code, reason: out.append(Violation(code, reason, WARNING))

    known = set(ROBOT_ACTIONS) if actions is None else set(actions)
    if a not in known:
        err("unknown_action", f"action '{a}' not in the action vocabulary")
        return out

    if a == "MoveTo":
        if not o:
            err("empty_object", "MoveTo requires a destination object")
        elif world.arrived == o:
            warn("already_there", f"the robot is already at '{o}'")

    elif a == "Find":
        if not o:
            err("empty_object", "Find requires a target object")
            return out
        if world.holding and world.holding == o:
            err("find_held", f"'{o}' is already in the hand")
        where = (object_places or {}).get(o)
        if where and world.arrived != where:
            err("find_wrong_place", f"'{o}' is believed to be at '{where}'; MoveTo '{where}' first")

    elif a == "Pick":
        if world.holding:
            err("already_holding", f"cannot Pick while already holding '{world.holding}'")
        if world.found is None:
            err("no_find_before_pick", "Pick requires a preceding Find of the target object")
        elif o and world.found != o:
            err("pick_not_found", f"Pick '{o}' but the last Find was '{world.found}'")

    elif a in _PLACE_ACTIONS:
        if not world.holding:
            err("place_without_holding", f"{a} requires holding an object")
        if not o:
            err("empty_object", f"{a} requires a receptacle/container")
            return out
        at_it = world.arrived == o or (a == "PutIn" and caps.find_reaches and world.found == o)
        if not at_it:
            err("place_wrong_location",
                f"{a} requires MoveTo '{o}' first (currently at '{world.arrived}')")
        # Place puts the object ON the receptacle (the top of a closed chest of
        # drawers is a surface); only PutIn goes inside and needs it open.
        if a == "PutIn" and is_container(o, containers) and o not in world.opened:
            err("putin_closed_container", f"PutIn requires container '{o}' to be Open")

    elif a in _REACH_ACTIONS:
        if not o and a != "Wash":
            err("empty_object", f"{a} requires a target object")
            return out
        if a in _HANDLE_ACTIONS and openable is not None and not _member(o, openable):
            err("not_openable", f"'{o}' cannot be opened or closed")
            return out
        if not (_reachable(o, world, caps) or (a == "Wash" and o and world.holding == o)):
            err("interact_without_find", f"{a} requires a preceding Find of (or MoveTo) '{o or 'target'}'")
        if a == "Open" and o in world.opened:
            err("already_open", f"Open '{o}' but it is already Open")
        elif a == "Close" and o not in world.opened:
            err("close_not_opened", f"Close '{o}' but it is not Open")
        elif a == "TurnOn" and o in world.on:
            err("already_on", f"TurnOn '{o}' but it is already On")
        elif a == "TurnOff" and o not in world.on:
            err("turnoff_not_on", f"TurnOff '{o}' but it is not On")
        if a in _HANDLE_ACTIONS and world.holding and caps.hands < 2:
            err("hand_full", f"cannot {a} '{o}' while holding '{world.holding}' (one hand)")

    elif a in _TREAT_ACTIONS:
        # Heat/Cool/Clean require the object to be in hand.
        if not world.holding:
            err("treat_without_holding", f"{a} requires holding the object first")

    elif a == "Serve":
        if not world.holding:
            warn("serve_without_holding", "Serve with an empty hand")

    # Sit / LieOn / Wait and robot-specific actions: no symbolic precondition.
    out.sort(key=lambda v: v.severity != ERROR)
    return out


def errors(step: dict, world, **kw) -> list[Violation]:
    """check() without the warnings."""
    return [v for v in check(step, world, **kw) if v.is_error]


# ─────────────────────────────────────────────────────────────────────
# apply: the effects
# ─────────────────────────────────────────────────────────────────────
def apply(step: dict, world, *, inplace: bool = False):
    """The world after `step` succeeded. Returns a copy unless `inplace`.

    Only the symbolic fields are touched; robot extras (poses, timestamps)
    stay with the caller.
    """
    w = world if inplace else (world.copy() if hasattr(world, "copy") else _copy.deepcopy(world))
    a = normalize_action(step.get("action", "") or "")
    o = _target(step)

    if a == "MoveTo":
        w.arrived = o
        # Moving away from an interactable resets 'found' unless the new
        # destination IS the same handle (e.g. Find Fridge, MoveTo Fridge).
        if w.found and w.found != o:
            w.found = None
    elif a == "Find":
        w.found = o
    elif a == "Pick":
        # The object field is optional; default to the last found.
        held = o or w.found
        if held:
            w.holding = held
        w.found = None  # the object has left the scene
    elif a in _PLACE_ACTIONS:
        w.holding = None  # the robot stays at the receptacle
    elif a == "Open":
        if o:
            w.opened.add(o)
    elif a == "Close":
        w.opened.discard(o)
    elif a == "TurnOn":
        if o:
            w.on.add(o)
    elif a == "TurnOff":
        w.on.discard(o)
    # Heat/Cool/Clean keep the object in hand; Sit / LieOn / Serve / Wait /
    # Wash have no tracked effect.
    return w


# ─────────────────────────────────────────────────────────────────────
# simulate: a whole plan
# ─────────────────────────────────────────────────────────────────────
def simulate(
    plan:            list[dict],
    world=None,
    visible_objects: list[str] | None = None,
    containers:      Names            = None,
    stop_on_error:   bool             = True,
    *,
    initial_state=None,
    capabilities:    "Capabilities | Mapping | None" = None,
    openable:        Names = None,
    object_places:   Mapping[str, str] | None = None,
    actions:         Iterable[str] | None = None,
    apply_on_error:  bool = False,
) -> VerifierReport:
    """Replay a plan symbolically and report all precondition violations.

    Args:
        plan:            list of step dicts (normalized on a copy)
        world:           starting state (SymbolicState / WorldState); a new
                         empty SymbolicState if None. ``initial_state`` is the
                         legacy name of the same argument.
        visible_objects: optional objects assumed visible at start
        stop_on_error:   halt at the first error; else collect all errors and
                         keep simulating.
        apply_on_error:  when continuing, still apply a rejected step's effects
                         (optimistic replay, avoids cascading findings).
        capabilities / containers / openable / object_places / actions: as
                         in :func:`check`.
    """
    start = world if world is not None else initial_state
    state = start.copy() if start is not None else SymbolicState()
    if visible_objects and hasattr(state, "visible"):
        state.visible.update(visible_objects)

    kw = dict(capabilities=capabilities, containers=containers, openable=openable,
              object_places=object_places, actions=actions)
    plan = normalize_plan(plan)
    violations: list[StepViolation] = []
    warnings: list[StepViolation] = []

    for i, step in enumerate(plan):
        found = check(step, state, **kw)
        errs = [v for v in found if v.is_error]
        warnings += [StepViolation(i, step, v.code, v.reason, v.severity) for v in found if not v.is_error]
        if errs:
            v = errs[0]
            violations.append(StepViolation(index=i, step=step, code=v.code, reason=v.reason))
            if stop_on_error:
                break
            if not apply_on_error:
                continue  # skip side-effects of the rejected step
        apply(step, state, inplace=True)

    return VerifierReport(
        ok=not violations,
        violations=violations,
        final_state=state,
        num_steps=len(plan),
        warnings=warnings,
    )


__all__ = [
    "ACTION_MODEL_VERSION", "ERROR", "WARNING",
    "Capabilities", "DEFAULT_CAPABILITIES", "DEFAULT_CONTAINERS",
    "Violation", "SymbolicState", "StepViolation", "VerifierReport",
    "normalize_action", "normalize_step", "normalize_plan", "is_container",
    "check", "errors", "apply", "simulate",
]
