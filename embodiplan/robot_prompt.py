"""SAGE's system prompts for one robot: its own actions and description (0.2.4).

By default SAGE plans for a generic household robot: the fixed
``base.ACTIONS_STR`` vocabulary (14 AI2-THOR actions), its ``STEP_SCHEMA`` rules
and ``JSON_EXAMPLE``, and system prompts that say "household robot". A client that
knows the robot (robot_agent reads it from the robot's manifest) passes

* ``actions``: one entry per action the robot can plan with, either a bare name
  (``"MoveTo"``) or a line that starts with it (``"MoveTo <place>: drive there"``);
* ``robot_description``: one or two sentences about the robot's body.

:func:`system_prompts` then lists only those actions, keeps only the action rules
and examples whose actions the robot has, and describes the robot. With neither
given it returns the default prompts byte for byte.
"""

from __future__ import annotations

import re
from typing import Iterable

from embodiplan.base import ACTIONS_STR, JSON_EXAMPLE, STEP_SCHEMA

_NAME_RX = re.compile(r"\s*([A-Za-z][A-Za-z0-9_]*)")

_SCHEMA_HEAD = """\
Each step is a JSON object with exactly two fields:
  "action" : one action from the list above (exact spelling)
  "object" : target in CamelCase, e.g. Apple, CoffeeMachine, Kitchen, DiningTable"""

# the rule text of base.STEP_SCHEMA, per action (kept only when the robot has it)
_RULES = {
    "MoveTo": """\
- MoveTo <room|furniture>  : navigate to a room (Kitchen, LivingRoom, Bedroom, Bathroom)
                             or furniture/receptacle in current room (DiningTable, Sofa).
                             Updates 'arrived' in robot state.
                             Use MoveTo for ALL navigation — rooms AND furniture.""",
    "Find": """\
- Find <object>            : find a PICKUPABLE object in the current room (e.g. Apple, Mug,
                             RemoteControl, Towel, SoapBottle). DO NOT use Find for rooms
                             or furniture — use MoveTo for those.
                             Updates 'found' in robot state.""",
    "Pick": "- Pick                     : assert 'found' is set, then pick it up. object field ignored.",
    "Place": """\
- Place <receptacle>       : assert 'holding' and 'arrived', then place held object.
                             object = receptacle name (must match 'arrived').""",
}
_SWITCHES = ("Open", "Close", "TurnOn", "TurnOff")
_CONTAINER_RULE = """\
Container rule (REQUIRED even if not stated in the task):
  If the target object is inside a container (Fridge, Cabinet, Drawer, Microwave, etc.),
  you MUST navigate to the container, open it, find the object inside, pick it, then close.
  Pattern: MoveTo <container> → Open <container> → Find <object> → Pick → Close <container>"""
_TYPICAL_SIMPLE = """\
  Simple grab and place:
    MoveTo Kitchen → Find Apple → Pick → MoveTo LivingRoom → MoveTo DiningTable → Place DiningTable"""
_TYPICAL_CONTAINER = """\
  Object inside container (e.g. apple in fridge):
    MoveTo Kitchen → MoveTo Fridge → Open Fridge → Find Apple → Pick → Close Fridge → MoveTo LivingRoom → MoveTo DiningTable → Place DiningTable"""
_TREATMENT = ("Heat", "Cool", "Clean")

_CARRY = {"MoveTo", "Find", "Pick", "Place"}
_CONTAINER = _CARRY | {"Open", "Close"}

# the household lines of the default prompts -> the robot's (``{robot}`` = its description)
_HEADS = {
    "decompose": ("You are a household task planner (high-level).",
                  "You are the high-level task planner of this robot: {robot}"),
    "expand": ("You are a household assistant robot (low-level executor).",
               "You are the low-level executor of this robot: {robot}"),
    "refine": ("You are repairing a household plan.",
               "You are repairing a plan of this robot: {robot}"),
    "repair": ("You are repairing a failed sub-goal in a household plan.",
               "You are repairing a failed sub-goal in a plan of this robot: {robot}"),
}


def action_name(entry) -> str:
    """The action an ``actions`` entry names (its first word)."""
    m = _NAME_RX.match(str(entry or ""))
    return m.group(1) if m else ""


def action_names(actions: Iterable) -> list:
    out: list = []
    for a in actions or ():
        n = action_name(a)
        if n and n not in out:
            out.append(n)
    return out


def actions_block(actions: Iterable) -> str:
    """The "Available robot actions" list: one indented line per entry."""
    lines = [str(a).strip() for a in actions or () if action_name(a)]
    return "\n".join(f"  {ln}" for ln in lines)


def step_schema(actions: Iterable) -> str:
    """base.STEP_SCHEMA with only the rules, container rule and typical sequences
    whose actions are all in ``actions``."""
    have = set(action_names(actions))
    rules = [_RULES[a] for a in ("MoveTo", "Find", "Pick") if a in have]
    sw = [a for a in _SWITCHES if a in have]
    if sw:
        rule = (f"- {'/'.join(sw)}: assert 'found' is set, then act on it. object field ignored.")
        if "Open" in have and "Find" in have:
            rule += ("\n                             Note: use Find before Open/Close only for containers (Fridge,"
                     "\n                             Cabinet, Drawer, Microwave) that hold an object to retrieve.")
        rules.append(rule)
    if "Place" in have:
        rules.append(_RULES["Place"])
    parts = [_SCHEMA_HEAD]
    if rules:
        parts.append("Action rules:\n" + "\n".join(rules))
    if {"Open", "Find", "Pick", "MoveTo"} <= have:
        parts.append(_CONTAINER_RULE)
    typical = [t for t, need in ((_TYPICAL_SIMPLE, _CARRY), (_TYPICAL_CONTAINER, _CONTAINER)) if need <= have]
    if typical:
        parts.append("Typical sequences:\n" + "\n".join(typical))
    treat = [a for a in _TREATMENT if a in have]
    if treat:
        parts.append("Object-treatment actions (" + ", ".join(treat) + "): use them on the object you are "
                     "HOLDING, only when the task requires them.")
    return "\n\n".join(parts)


def json_example(actions: Iterable) -> str:
    """base.JSON_EXAMPLE when the robot has its actions (the container example only
    with Open / Close), else a short example made of the robot's own actions."""
    names = action_names(actions)
    have = set(names)
    simple, _sep, _container = JSON_EXAMPLE.partition("\n\nExample with container:")
    if _CONTAINER <= have:
        return JSON_EXAMPLE
    if _CARRY <= have:
        return simple
    picks = names[:3] or ["Wait"]
    rows = ",\n".join(
        f'  {{"action": "{a}", "object": "{"Kitchen" if a == "MoveTo" else "Target"}"}}' for a in picks)
    return '{"steps": [\n' + rows + "\n]}"


def system_prompts(defaults: dict, actions=None, robot_description: str | None = None) -> dict:
    """``defaults`` ({decompose, expand, refine, repair} -> SAGE's default system
    prompt) for this robot. ``actions`` / ``robot_description`` None = unchanged."""
    out = dict(defaults)
    acts = [a for a in (actions or ()) if action_name(a)] if actions is not None else None
    desc = " ".join(str(robot_description or "").split())
    for key, text in out.items():
        if acts:
            text = (text.replace(STEP_SCHEMA, step_schema(acts))
                        .replace(JSON_EXAMPLE, json_example(acts))
                        .replace(ACTIONS_STR, actions_block(acts)))
            if key == "decompose":
                text = text.replace(
                    "\n\nReturn ONLY valid JSON",
                    "\n\nThe robot can only do these actions: " + ", ".join(action_names(acts))
                    + ". Plan no sub-goal that needs another action.\n\nReturn ONLY valid JSON", 1)
        if desc:
            old, new = _HEADS[key]
            text = text.replace(old, new.format(robot=desc), 1)
        out[key] = text
    return out


__all__ = ["action_name", "action_names", "actions_block", "step_schema", "json_example", "system_prompts"]
