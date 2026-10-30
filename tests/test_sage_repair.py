"""SAGE suffix repair (0.2.2): which sub-goal is repaired, from which state, and what is remembered.

The LLM is a script keyed on the prompt (``_chat`` is SAGE's one LLM call).
Run: PYTHONPATH=. python -m pytest -q tests
"""
import json
import re

from embodiplan.action_model import SymbolicState
from embodiplan.sage import SAGEPlanner, belief_from_obs, flatten


def S(*pairs):
    return [{"action": a, "object": o} for a, o in pairs]


class Scripted(SAGEPlanner):
    def __init__(self, subgoals, expand, repairs=(), refines=(), **kw):
        super().__init__(host="http://127.0.0.1:1", model="scripted", enable_memory=False, **kw)
        self.subgoals, self.expand_by = list(subgoals), dict(expand)
        self.repairs, self.refines, self.prompts = list(repairs), list(refines), []

    def _chat(self, messages, temperature=0.2):
        user = messages[-1]["content"]
        self.prompts.append(user)
        if "Break this into ordered sub-goals" in user:
            return json.dumps({"subgoals": self.subgoals}), 0, 0
        if "=== NEW SUB-GOAL ===" in user:
            sg = re.search(r"Current sub-goal: (.*)", user).group(1).strip()
            return json.dumps({"steps": self.expand_by.get(sg, [])}), 0, 0
        if "=== LOCAL REPAIR ===" in user:
            return json.dumps({"steps": self.repairs.pop(0) if self.repairs else []}), 0, 0
        if "=== REPAIR SUB-GOAL ===" in user:
            return json.dumps({"steps": self.refines.pop(0) if self.refines else []}), 0, 0
        return '{"steps": []}', 0, 0

    def repairs_sent(self):
        return [u for u in self.prompts if "=== LOCAL REPAIR ===" in u]

    def repaired_subgoal(self):
        return re.search(r"Current sub-goal: (.*)", self.repairs_sent()[-1]).group(1).strip()

    def repair_state(self):
        return re.search(r"Robot symbolic state right now:\n\s*(.*)", self.repairs_sent()[-1]).group(1)


# ── R1: positions after flatten's dedupe ──────────────────────────────
def test_failed_subgoal_located_on_the_deduped_plan():
    sg = ["get apple", "apple to table", "get mug", "mug to sofa"]
    p = Scripted(sg, {
        sg[0]: S(("MoveTo", "Kitchen"), ("Find", "Apple"), ("Pick", "Apple")),
        sg[1]: S(("MoveTo", "Kitchen"), ("MoveTo", "DiningTable"), ("Place", "DiningTable")),
        sg[2]: S(("MoveTo", "DiningTable"), ("Find", "Mug"), ("Pick", "Mug")),
        sg[3]: S(("MoveTo", "Sofa"), ("Place", "Sofa"))},
        repairs=[S(("MoveTo", "Counter"), ("Find", "Mug"), ("Pick", "Mug"))])
    flat, _ = p.generate_plan("t", "", [])
    assert flat == flatten(p.last_blocks) and len(flat) == 9       # 2 MoveTo dropped
    i = flat.index({"action": "Find", "object": "Mug"})
    suffix, m = p.replan("t", flat[:i], flat[i], "Mug not found", "", [])
    assert p.repaired_subgoal() == "get mug" and m.extra["repaired_subgoal"] == "get mug"
    assert suffix == S(("MoveTo", "Counter"), ("Find", "Mug"), ("Pick", "Mug"),
                       ("MoveTo", "Sofa"), ("Place", "Sofa"))


def test_external_block_edit_is_followed():
    """robot_agent edits block steps in place after generate_plan; locating follows the edit."""
    sg = ["get apple", "apple to table"]
    p = Scripted(sg, {sg[0]: S(("MoveTo", "Kitchen"), ("Find", "Apple"), ("Pick", "Apple")),
                      sg[1]: S(("MoveTo", "DiningTable"), ("Place", "DiningTable"))})
    p.generate_plan("t", "", [])
    p.last_blocks[0].steps = S(("MoveTo", "Kitchen"), ("Open", "Fridge"), ("Find", "Apple"), ("Pick", "Apple"))
    flat = flatten(p.last_blocks)
    p.replan("t", flat[:4], flat[4], "blocked", "", [])
    assert p.repaired_subgoal() == "apple to table"


# ── R2: the repair starts from the belief ─────────────────────────────
def test_belief_from_obs_both_formats():
    b = belief_from_obs("Places: x\nRobot state: arrived=drawer@kitchen found=- holding=Mug "
                        "opened=[drawer@kitchen, Fridge] on=[-]")
    assert (b.arrived, b.found, b.holding, b.opened, b.on) == (
        "drawer@kitchen", None, "Mug", {"drawer@kitchen", "Fridge"}, set())
    s = SymbolicState(arrived="Desk", holding="Cup", opened={"Drawer"}, on={"Lamp"})
    b2 = belief_from_obs("Robot state: " + s.as_text())
    assert (b2.arrived, b2.found, b2.holding, b2.opened, b2.on) == ("Desk", None, "Cup", {"Drawer"}, {"Lamp"})
    assert belief_from_obs("Kitchen scene loaded.") is None


def _spoon():
    sg = ["get spoon", "spoon to table"]
    return sg, Scripted(sg, {sg[0]: S(("MoveTo", "Drawer"), ("Find", "Spoon"), ("Pick", "Spoon")),
                             sg[1]: S(("MoveTo", "DiningTable"), ("Place", "DiningTable"))},
                        repairs=[S(("Find", "Spoon"), ("Pick", "Spoon"))])


def test_repair_replays_completed_from_the_plan_start_belief():
    _sg, p = _spoon()
    flat, _ = p.generate_plan("t", "Robot state: arrived=- found=- holding=- opened=[Drawer] on=[-]", [])
    p.replan("t", flat[:2], flat[2], "grasp failed", "fresh camera text, no belief line", [])
    st = p.repair_state()
    assert "arrived: Drawer" in st and "found: Spoon" in st and "opened: ['Drawer']" in st


def test_repair_takes_the_callers_belief():
    _sg, p = _spoon()
    flat, _ = p.generate_plan("t", "", [])
    p.replan("t", flat[:2], flat[2], "grasp failed",
             "Robot state: arrived=Drawer found=Spoon holding=- opened=[Drawer] on=[-]", [])
    assert "opened: ['Drawer']" in p.repair_state()
    p.repairs = [S(("Pick", "Spoon"))]
    p.replan("t", flat[:2], flat[2], "grasp failed", "",
             [], state=SymbolicState(arrived="Drawer", holding="Fork", opened={"Box"}))
    assert "holding: Fork" in p.repair_state() and "Box" in p.repair_state()


def test_plan_gate_starts_from_the_belief():
    """Already holding the cup: 'MoveTo, Place' needs no refine (it did from an empty state)."""
    p = Scripted(["put cup on table"], {"put cup on table": S(("MoveTo", "Table"), ("Place", "Table"))})
    _flat, m = p.generate_plan("t", "Robot state: arrived=Sofa found=- holding=Cup opened=[-] on=[-]", [])
    assert m.extra["verifier_rejections"] == 0
    _flat, m = p.generate_plan("t", "", [])
    assert m.extra["verifier_rejections"] == 1


# ── R3: a global fallback does not stick ──────────────────────────────
def test_global_fallback_then_local_again():
    sg = ["get the apple", "put it on the table"]
    p = Scripted(sg, {sg[0]: S(("MoveTo", "Kitchen"), ("Find", "Apple"), ("Pick", "Apple")),
                      sg[1]: S(("MoveTo", "DiningTable"), ("Place", "DiningTable"))},
                 repairs=[S(("MoveTo", "Hall"), ("MoveTo", "DiningTable"), ("Place", "DiningTable"))])
    done = S(("MoveTo", "Hall"))
    steps, m1 = p.replan("t", done, {"action": "MoveTo", "object": "Kitchen"}, "blocked", "", [])
    assert m1.notes == "global_replan_fallback"
    _s, m2 = p.replan("t", done + steps[:3], steps[3], "path blocked", "", [])
    assert m2.notes == "local_repair" and p.repaired_subgoal() == "put it on the table"
    # the belief after the fallback starts after the completed steps (no double replay)
    assert "holding: Apple" in p.repair_state() and "arrived: Kitchen" in p.repair_state()


# ── R4: the repaired suffix is written back ───────────────────────────
def test_repaired_suffix_is_remembered():
    sg = ["get the apple", "apple to table", "get the mug"]
    p = Scripted(sg, {
        sg[0]: S(("MoveTo", "Kitchen"), ("Find", "Apple"), ("Pick", "Apple")),
        sg[1]: S(("MoveTo", "DiningTable"), ("Place", "DiningTable")),
        sg[2]: S(("MoveTo", "Desk"), ("Find", "Mug"), ("Pick", "Mug"))},
        repairs=[S(("MoveTo", "Counter"), ("Find", "Apple"), ("Pick", "Apple")),
                 S(("Place", "DiningTable")), S(("Find", "Mug"), ("Pick", "Mug"))])
    flat, _ = p.generate_plan("t", "", [])
    suffix, _ = p.replan("t", flat[:1], flat[1], "Apple not found", "", [])
    assert [len(b.steps) for b in p.last_blocks] == [4, 2, 3]
    assert p.last_blocks[0].steps == flat[:1] + suffix[:3]
    done = flat[:1] + suffix[:4]
    suffix2, _ = p.replan("t", done, suffix[4], "no free spot", "", [])
    assert p.repaired_subgoal() == "apple to table"
    # a 3rd failure (Find Mug) lands in 'get the mug'
    done = done + suffix2[:2]
    p.replan("t", done, suffix2[2], "Mug not found", "", [])
    assert p.repaired_subgoal() == "get the mug"


# ── G1: refine output is re-verified before use ───────────────────────
def test_worse_refine_is_dropped():
    sg = ["get the cup"]
    bad = S(("MoveTo", "Desk"), ("Pick", "Cup"))                      # Pick without Find: 1 error
    worse = S(("Place", "Desk"), ("Pick", "Cup"), ("Place", "Sofa"))  # more errors
    p = Scripted(sg, {sg[0]: bad}, refines=[worse])
    flat, m = p.generate_plan("t", "", [])
    assert flat == bad and m.extra["refine_rejected"] == [True]
    good = S(("MoveTo", "Desk"), ("Find", "Cup"), ("Pick", "Cup"))
    p = Scripted(sg, {sg[0]: bad}, refines=[good])
    flat, m = p.generate_plan("t", "", [])
    assert flat == good and m.extra["refine_rejected"] == [False]


# ── memory only from verified episodes ────────────────────────────────
class _Mem:
    def __init__(self):
        self.added = []

    def retrieve(self, task, k=3):
        return []

    def add_episode(self, task, plan, success=True):
        self.added.append((task, plan))


def test_only_verified_episodes_are_recorded():
    mem = _Mem()
    p = SAGEPlanner(host="http://127.0.0.1:1", model="x", memory=mem)
    ok = S(("MoveTo", "Desk"), ("Find", "Cup"), ("Pick", "Cup"), ("MoveTo", "Table"), ("Place", "Table"))
    assert p.record_episode("t", ok) is True
    assert p.record_episode("t", ok, success=False) is False
    assert p.record_episode("t", S(("MoveTo", "Table"), ("Place", "Table"))) is False   # nothing held
    assert p.record_episode("t", []) is False
    assert [t for t, _ in mem.added] == ["t"]
