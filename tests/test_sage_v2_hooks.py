"""0.2.6 hooks for a caller that understood the goals itself (robot_agent's AssistLoop, SAGE-v2):

* ``generate_plan(subgoals=[...])``: no decompose call, one expand per given sub-goal, each told that the
  earlier ones are done (sub-goal explosion: a decompose that splits one goal into steps re-plans the whole
  task in every block);
* ``generate_plan(budget=...)``: the caller's budget is taken before every LLM call; its exception stops SAGE
  between two calls, never inside a parse, and reaches the caller as is;
* ``Capabilities.release_actions``: a robot action that releases the held object (a hand-over) empties the
  hand in the action model, so SAGE's own simulation of "MoveTo person, Handover" is consistent;
* ``parse_steps``: a number in a step field is text (``{"action": "Wait", "object": 5}`` crashed callers).

The LLM is a fake backend (``BasePlanner._chat`` -> ``self._backend.chat``), so SAGE's own ``_chat`` (with the
budget hook) runs.
"""
import json
import re

import pytest

from embodiplan.action_model import Capabilities, SymbolicState, apply, simulate
from embodiplan.base import parse_steps
from embodiplan.sage import SAGEPlanner


def S(*pairs):
    return [{"action": a, "object": o} for a, o in pairs]


class Backend:
    def __init__(self, expand, subgoals=("whole task",)):
        self.expand, self.subgoals, self.prompts = dict(expand), list(subgoals), []

    def chat(self, messages, temperature=None):
        user = messages[-1]["content"]
        self.prompts.append(user)
        if "Break this into ordered sub-goals" in user:
            return json.dumps({"subgoals": self.subgoals}), 0, 0
        m = re.search(r"Current sub-goal: (.*)", user)
        return json.dumps({"steps": self.expand.get(m.group(1).strip() if m else "", [])}), 0, 0


def planner(backend, **kw):
    p = SAGEPlanner(host="http://127.0.0.1:1", model="scripted", enable_memory=False, **kw)
    p._backend = backend
    return p


class Budget:
    def __init__(self, limit):
        self.limit, self.used = limit, 0

    def take(self, n=1, what=""):
        if self.used + n > self.limit:
            raise OverflowError(f"{what}: budget {self.limit} used")
        self.used += n


A = "put the cup on the table"
B = "put the book on the shelf"
PLAN = {A: S(("MoveTo", "counter"), ("Find", "cup"), ("Pick", "cup"), ("MoveTo", "table"), ("Place", "table")),
        B: S(("MoveTo", "desk"), ("Find", "book"), ("Pick", "book"), ("MoveTo", "shelf"), ("Place", "shelf"))}


def test_given_subgoals_skip_decompose_and_expand_each_once():
    be = Backend(PLAN)
    steps, m = planner(be).generate_plan("tidy up", "Robot state: arrived=- holding=-", [], subgoals=[A, B])
    assert not any("Break this into ordered sub-goals" in p for p in be.prompts)
    assert len(be.prompts) == 2 and m.llm_calls == 2
    assert m.extra["subgoals"] == [A, B]
    assert [s["object"] for s in steps if s["action"] == "Place"] == ["table", "shelf"]
    # the second block is told that the first one is done
    assert all("earlier sub-goals are done" in p for p in be.prompts)


def test_without_subgoals_unchanged():
    be = Backend({"whole task": PLAN[A]})
    _steps, m = planner(be).generate_plan("put the cup on the table", "", [])
    assert "Break this into ordered sub-goals" in be.prompts[0]
    assert m.llm_calls == 2 and not any("earlier sub-goals are done" in p for p in be.prompts)


def test_budget_taken_per_call_and_stops_between_calls():
    be, b = Backend(PLAN), Budget(1)
    with pytest.raises(OverflowError):
        planner(be).generate_plan("tidy up", "", [], subgoals=[A, B], budget=b)
    assert b.used == 1 and len(be.prompts) == 1          # the second call was never sent
    b2 = Budget(5)
    planner(be).generate_plan("tidy up", "", [], subgoals=[A, B], budget=b2)
    assert b2.used == 2


def test_budget_none_after_the_call():
    be = Backend(PLAN)
    p = planner(be)
    p.generate_plan("tidy up", "", [], subgoals=[A], budget=Budget(9))
    assert p._budget is None and p._given_subgoals is False


def test_release_action_clears_hand():
    caps = Capabilities.from_any({"hands": 1, "release_actions": ["Handover"]})
    w = SymbolicState(arrived="kid", holding="toy car")
    assert apply({"action": "Handover", "object": "kid"}, w, capabilities=caps).holding is None
    # default: no release action (other callers unchanged)
    assert apply({"action": "Handover", "object": "kid"}, w).holding == "toy car"
    rep = simulate(S(("Handover", "kid"), ("MoveTo", "desk"), ("Find", "book"), ("Pick", "book")),
                   initial_state=w, capabilities=caps, actions=["MoveTo", "Find", "Pick", "Handover"])
    assert rep.ok, rep.violations


def test_parse_steps_numbers():
    steps = parse_steps('{"steps": [{"action": "Wait", "object": 5}, {"action": "Say", "object": 1.5}, 7]}')
    assert steps == [{"action": "Wait", "object": "5"}, {"action": "Say", "object": "1.5"}]
    assert parse_steps('[{"action": "MoveTo", "object": null}]') == [{"action": "MoveTo", "object": None}]
