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


# ── the run so far is lined up as a whole: name forms, added searches, retries (made-up worlds) ──

KETTLE = ["fetch the kettle", "bring kettle to shelf", "fetch the lamp"]
KETTLE_STEPS = {
    KETTLE[0]: S(("MoveTo", "Pantry"), ("Find", "Brass_Kettle"), ("Pick", "Brass_Kettle")),
    KETTLE[1]: S(("MoveTo", "Shelf-Top"), ("Place", "Shelf-Top")),
    KETTLE[2]: S(("MoveTo", "Cellar"), ("Find", "Lamp"), ("Pick", "Lamp")),
}
TWICE = ["fetch the urn", "bring urn to bench", "fetch the urn again"]   # the same step in two blocks
TWICE_STEPS = {
    TWICE[0]: S(("MoveTo", "Attic"), ("Find", "Urn"), ("Pick", "Urn")),
    TWICE[1]: S(("MoveTo", "Bench"), ("Place", "Bench")),
    TWICE[2]: S(("MoveTo", "Cellar"), ("Find", "Urn"), ("Pick", "Urn")),
}


def one_replan(p, done, failed):
    """(repaired sub-goal, notes, LLM calls of this replan)."""
    n = len(p.prompts)
    _s, m = p.replan("t", done, failed, "gone", "", [])
    return p.repaired(), m.notes, len(p.prompts) - n


def test_name_forms_with_underscore_dash_or_space_are_one_object():
    p, flat = planned(subgoals=KETTLE, expand=KETTLE_STEPS, replan_block_fix=True, repairs=[S(("Find", "Brass_Kettle"))])
    assert one_replan(p, flat[:1], {"action": "Find", "object": "brass kettle"})[:2] == (KETTLE[0], "local_repair")
    p, flat = planned(subgoals=KETTLE, expand=KETTLE_STEPS, replan_block_fix=True, repairs=[S(("Place", "Shelf-Top"))])
    done = flat[:3] + [{"action": "MoveTo", "object": "shelf top"}]
    assert one_replan(p, done, {"action": "Place", "object": "shelf_top@hall"})[0] == KETTLE[1]


def test_added_search_equal_to_a_later_block_start_does_not_pull_the_failed_step_forward():
    p, flat = planned(replan_block_fix=True, repairs=[S(("Find", "Urn"))])
    done = flat[:1] + S(("MoveTo", "Bench"))           # a search the caller added; block 1 begins with it
    assert one_replan(p, done, flat[1])[0] == SG[0]


def test_retry_of_a_step_already_run_stays_in_its_block():
    p, flat = planned(replan_block_fix=True, repairs=[S(("Find", "Urn"))])
    assert one_replan(p, flat[:2], flat[1])[0] == SG[0]          # [MoveTo, Find Urn] done, Find Urn again


def test_same_step_in_two_blocks_retry_stays_early_progress_goes_late():
    p, flat = planned(subgoals=TWICE, expand=TWICE_STEPS, replan_block_fix=True, repairs=[S(("Pick", "Urn"))])
    assert one_replan(p, flat[:2], flat[1])[0] == TWICE[0]
    p, flat = planned(subgoals=TWICE, expand=TWICE_STEPS, replan_block_fix=True, repairs=[S(("Pick", "Urn"))])
    assert one_replan(p, flat[:6], flat[6])[0] == TWICE[2]
    p, flat = planned(subgoals=TWICE, expand=TWICE_STEPS, replan_block_fix=True, repairs=[S(("Pick", "Urn"))])
    done = flat[:1] + S(("MoveTo", "Cellar"))          # a search to the later block's place, then the first Find
    assert one_replan(p, done, flat[1])[0] == TWICE[0]


def test_a_dropped_first_step_still_lets_its_twin_in_a_later_block_be_found():
    sg = ["fetch the urn", "bring urn to bench", "fetch the lamp"]
    ex = {sg[0]: S(("MoveTo", "Attic"), ("Find", "Urn"), ("Pick", "Urn")), sg[1]: S(("MoveTo", "Bench"), ("Place", "Bench")),
          sg[2]: S(("MoveTo", "Attic"), ("Find", "Lamp"), ("Pick", "Lamp"))}
    p, flat = planned(subgoals=sg, expand=ex, replan_block_fix=True, repairs=[S(("MoveTo", "Attic"))])
    assert one_replan(p, flat[1:5], flat[5])[0] == sg[2]          # the robot started in the attic


def test_separator_only_name_is_empty_and_lines_up_with_nothing():
    p, flat = planned(replan_block_fix=True)
    assert one_replan(p, flat[:1], {"action": "Find", "object": "_-"})[1] == "global_replan_fallback"


def test_regression_a_correct_local_repair_is_not_sent_to_a_global_replan():
    # the three shapes that sent a repair of block 0 to the global replan: a name form, an added search, a retry
    cases = [(KETTLE, KETTLE_STEPS, lambda f: f[:1], {"action": "Find", "object": "brass kettle"}),
             (SG, EXPAND, lambda f: f[:1] + S(("MoveTo", "Bench")), {"action": "Find", "object": "Urn"}),
             (SG, EXPAND, lambda f: f[:2], {"action": "Find", "object": "urn@attic"})]
    for sg, ex, done, failed in cases:
        p, flat = planned(subgoals=sg, expand=ex, replan_block_fix=True, repairs=[S(("Find", "X"))])
        assert one_replan(p, done(flat), failed) == (sg[0], "local_repair", 1)


def test_replan_block_fix_off_is_the_count_rule_on_every_synthetic_history():
    from embodiplan.sage import _flatten_owned
    worlds = [(SG, EXPAND), (KETTLE, KETTLE_STEPS), (TWICE, TWICE_STEPS)]
    for sg, ex in worlds:
        p0, flat = planned(subgoals=sg, expand=ex)
        owners = _flatten_owned(p0.last_blocks)[1]
        histories = [flat[:n] for n in range(len(flat) + 1)] + [flat[:1] + S(("MoveTo", "Nowhere")), flat[1:3]]
        for done in histories:
            for failed in (flat[min(len(done), len(flat) - 1)], flat[0], {"action": "Find", "object": "x y"}):
                p, _f = planned(subgoals=sg, expand=ex, repairs=[S(("Find", "X"))])
                want = sg[owners[len(done)] if len(done) < len(owners) else owners[-1]]
                assert one_replan(p, done, failed) == (want, "local_repair", 1)
