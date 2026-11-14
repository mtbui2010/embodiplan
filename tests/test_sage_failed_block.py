"""SAGE ``replan_block_fix``: the failed block is found on the plan's own steps, not by counting completed steps.

Made-up blocks and actions only. The LLM is a script keyed on the prompt (``_chat`` is SAGE's one LLM call).
Run: PYTHONPATH=. python -m pytest -q tests/test_sage_failed_block.py
"""
import json
import re

from embodiplan.sage import SAGEPlanner, flatten


def S(*pairs):
    return [{"action": a, "object": o} for a, o in pairs]


SG = ["fetch the urn", "bring urn to bench", "fetch the lamp", "bring lamp to desk"]
EXPAND = {
    SG[0]: S(("MoveTo", "Attic"), ("Find", "Urn"), ("Pick", "Urn")),
    SG[1]: S(("MoveTo", "Bench"), ("Place", "Bench")),
    SG[2]: S(("MoveTo", "Cellar"), ("Find", "Lamp"), ("Pick", "Lamp")),
    SG[3]: S(("MoveTo", "Desk"), ("Place", "Desk")),
}


class Scripted(SAGEPlanner):
    def __init__(self, subgoals=SG, expand=EXPAND, repairs=(), **kw):
        super().__init__(host="http://127.0.0.1:1", model="scripted", enable_memory=False, **kw)
        self.subgoals, self.expand_by, self.repairs, self.prompts = list(subgoals), dict(expand), list(repairs), []

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
        return '{"steps": []}', 0, 0

    def repaired(self):
        sent = [u for u in self.prompts if "=== LOCAL REPAIR ===" in u]
        return re.search(r"Current sub-goal: (.*)", sent[-1]).group(1).strip() if sent else None


def planned(**kw):
    p = Scripted(**kw)
    flat, _ = p.generate_plan("t", "", [])
    return p, flat


def test_key_is_a_bool_and_defaults_false():
    assert Scripted().replan_block_fix is False
    assert Scripted(replan_block_fix=True).replan_block_fix is True


def test_replan_block_fix_off_counts_completed_steps_like_the_base():
    # two steps the caller ADDED before the failure: by count the failed step is taken for a later one
    p, flat = planned(repairs=[S(("Find", "Urn"))])
    done = flat[:1] + S(("Find", "Hall"), ("Find", "Annex")) + flat[1:2]
    p.replan("t", done, flat[2], "gone", "", [])
    assert p.repaired() == SG[1]                       # wrong block: the count ran two steps ahead


def test_added_steps_do_not_push_the_failed_block_forward():
    p, flat = planned(replan_block_fix=True, repairs=[S(("Find", "Urn"))])
    done = flat[:1] + S(("Find", "Hall"), ("Find", "Annex")) + flat[1:2]
    p.replan("t", done, flat[2], "gone", "", [])
    assert p.repaired() == SG[0]


def test_dropped_steps_do_not_pull_the_failed_block_back():
    p, flat = planned(replan_block_fix=True, repairs=[S(("Find", "Lamp"))])
    done = flat[:3] + flat[4:5]                         # the "MoveTo Bench" was dropped by the caller
    p.replan("t", done, flat[5], "gone", "", [])
    assert p.repaired() == SG[2]


def test_grounded_object_names_still_line_up():
    p, flat = planned(replan_block_fix=True, repairs=[S(("Place", "Bench"))])
    done = [dict(s, object=s["object"] + "@hall") for s in flat[:4]]
    p.replan("t", done, dict(flat[4], object="Bench@hall"), "full", "", [])
    assert p.repaired() == SG[1]


def test_failed_step_that_is_not_in_the_plan_gets_the_global_replan():
    p, flat = planned(replan_block_fix=True, repairs=[S(("MoveTo", "Desk"))])
    _s, m = p.replan("t", flat[:2], {"action": "Change", "object": "goals"}, "goals changed", "", [])
    assert m.notes == "global_replan_fallback" and p.repaired() is None


def test_all_steps_done_does_not_repair_the_last_block_again():
    p, flat = planned(replan_block_fix=True)
    _s, m = p.replan("t", flat, {"action": "Say", "object": "done"}, "check", "", [])
    assert m.notes == "global_replan_fallback" and p.repaired() is None


def test_all_steps_done_off_repairs_the_last_block_as_the_base_does():
    p, flat = planned(repairs=[S(("Place", "Desk"))])
    p.replan("t", flat, {"action": "Say", "object": "done"}, "check", "", [])
    assert p.repaired() == SG[3]


def test_replan_block_fix_empty_plan_and_no_blocks_fall_back_globally():
    p = Scripted(replan_block_fix=True)
    _s, m = p.replan("t", [], {"action": "Find", "object": "Urn"}, "none", "", [])
    assert m.notes == "global_replan_fallback"


def test_no_repeat_the_next_replan_after_a_repair_lands_in_the_next_block():
    p, flat = planned(replan_block_fix=True, repairs=[S(("Find", "Urn"), ("Pick", "Urn")), S(("Place", "Bench"))])
    suffix, _ = p.replan("t", flat[:1], flat[1], "gone", "", [])
    assert p.repaired() == SG[0]
    done = flat[:1] + suffix[:2]
    p.replan("t", done + S(("MoveTo", "Bench")), S(("Place", "Bench"))[0], "full", "", [])
    assert p.repaired() == SG[1]


def test_owners_stale_after_an_outside_edit_still_find_the_block():
    p, flat = planned(replan_block_fix=True, repairs=[S(("Find", "Lamp"))])
    p.last_blocks[0].steps = S(("MoveTo", "Attic"), ("Open", "Chest"), ("Find", "Urn"), ("Pick", "Urn"))
    flat = flatten(p.last_blocks)
    p.replan("t", flat[:5], flat[6], "gone", "", [])
    assert p.repaired() == SG[2]


def test_subgoals_are_ignored_with_the_key_off_as_in_the_base():
    p, flat = planned(repairs=[S(("Find", "Lamp"))])
    p.expand_by["fetch the vase"] = S(("Find", "Vase"), ("Pick", "Vase"))
    steps, m = p.replan("t", flat[:1], flat[1], "goal changed", "", [], subgoals=["fetch the vase"])
    assert m.notes != "global_replan_fallback" and "Vase" not in str(steps)


def test_new_subgoals_on_replan_are_planned_afresh_not_repaired_locally():
    p, flat = planned(replan_block_fix=True)
    new = ["fetch the vase"]
    p.expand_by["fetch the vase"] = S(("Find", "Vase"), ("Pick", "Vase"))
    steps, m = p.replan("t", flat[:1], flat[1], "goal changed", "", [], subgoals=new)
    assert m.notes == "global_replan_fallback" and steps == S(("Find", "Vase"), ("Pick", "Vase"))
    assert p.repaired() is None
