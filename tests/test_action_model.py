"""Regression tests for embodiplan.action_model (the one source of the action rules).

Run: PYTHONPATH=. python -m pytest -q tests
"""
import glob
import json
import os

import pytest

import embodiplan
from embodiplan import action_model as am
from embodiplan import verifier
from embodiplan.action_model import Capabilities, SymbolicState, apply, check, simulate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T, S, D = "table@kitchen", "shelf@living room", "drawer@bedroom"


def W(**kw):
    kw["opened"] = set(kw.get("opened", ()))
    kw["on"] = set(kw.get("on", ()))
    return SymbolicState(**kw)


def codes(step, world, **kw):
    vs = check(step, world, **kw)
    return [v.code for v in vs if v.is_error], [v.code for v in vs if not v.is_error]


def st(action, obj=""):
    return {"action": action, "object": obj}


# (world, step, expected first error or None, expected warnings)
EDGE_CASES = {
    "moveto_empty":            (W(), st("MoveTo"), "empty_object", []),
    "moveto_already_there":    (W(arrived=T), st("MoveTo", T), None, ["already_there"]),
    "find_empty":              (W(arrived=T), st("Find"), "empty_object", []),
    "find_before_any_moveto":  (W(), st("Find", "cup"), None, []),
    "find_held":               (W(arrived=T, holding="cup"), st("Find", "cup"), "find_held", []),
    "pick_without_find":       (W(arrived=T), st("Pick", "cup"), "no_find_before_pick", []),
    "pick_while_holding":      (W(arrived=T, found="apple", holding="cup"), st("Pick", "apple"),
                                "already_holding", []),
    "pick_other_than_found":   (W(arrived=T, found="apple"), st("Pick", "cup"), "pick_not_found", []),
    "pick_no_object":          (W(arrived=T, found="cup"), st("Pick"), None, []),
    "pick_held_again":         (W(arrived=T, holding="cup"), st("Pick", "cup"), "already_holding", []),
    "place_without_holding":   (W(arrived=S), st("Place", S), "place_without_holding", []),
    "place_wrong_location":    (W(arrived=T, holding="cup"), st("Place", S), "place_wrong_location", []),
    "place_ok":                (W(arrived=S, holding="cup"), st("Place", S), None, []),
    "place_on_closed_drawer":  (W(arrived=D, holding="cup"), st("Place", D), None, []),   # on top
    "putin_closed_drawer":     (W(arrived=D, holding="cup"), st("PutIn", D), "putin_closed_container", []),
    "putin_open_drawer":       (W(arrived=D, holding="cup", opened={D}), st("PutIn", D), None, []),
    "putin_non_container":     (W(arrived=S, holding="cup"), st("PutIn", S), None, []),
    "putin_not_there":         (W(arrived=T, holding="cup", opened={D}), st("PutIn", D),
                                "place_wrong_location", []),
    "open_twice":              (W(arrived=D, opened={D}), st("Open", D), "already_open", []),
    "open_while_holding":      (W(arrived=D, holding="cup"), st("Open", D), "hand_full", []),
    "open_not_there":          (W(arrived=T), st("Open", D), "interact_without_find", []),
    "open_found_not_there":    (W(arrived=T, found=D), st("Open", D), None, []),   # Find reaches (default)
    "open_ok":                 (W(arrived=D), st("Open", D), None, []),
    "close_already_closed":    (W(arrived=D), st("Close", D), "close_not_opened", []),
    "close_while_holding":     (W(arrived=D, opened={D}, holding="cup"), st("Close", D), "hand_full", []),
    "turnon_twice":            (W(found="lamp", on={"lamp"}), st("TurnOn", "lamp"), "already_on", []),
    "turnoff_when_off":        (W(found="lamp"), st("TurnOff", "lamp"), "turnoff_not_on", []),
    "turnon_while_holding":    (W(found="lamp", holding="cup"), st("TurnOn", "lamp"), None, []),
    "wash_unreached":          (W(arrived=T), st("Wash", "cup"), "interact_without_find", []),
    "wash_held":               (W(arrived=T, holding="cup"), st("Wash", "cup"), None, []),
    "serve_empty_hand":        (W(), st("Serve", "cup"), None, ["serve_without_holding"]),
    "unknown_action":          (W(), st("Fly", "cup"), "unknown_action", []),
    "open_empty_object":       (W(arrived=D), st("Open"), "empty_object", []),
}


@pytest.mark.parametrize("name", sorted(EDGE_CASES))
def test_edge_case(name):
    world, step, first_err, warns = EDGE_CASES[name]
    errs, ws = codes(step, world)
    assert (errs[0] if errs else None) == first_err
    assert ws == warns
    # the legacy facade reports the same first error
    ok, code, _ = verifier.verify_step(step, world)
    assert ok == (first_err is None) and code == first_err


def test_check_does_not_mutate():
    w = W(arrived=D, holding="cup")
    before = (w.arrived, w.found, w.holding, set(w.opened))
    check(st("Open", D), w)
    assert (w.arrived, w.found, w.holding, set(w.opened)) == before


# ── capabilities ─────────────────────────────────────────────────────────────
def test_two_hands_open_and_close_while_holding():
    two = Capabilities(hands=2)
    assert codes(st("Open", D), W(arrived=D, holding="cup"), capabilities=two) == ([], [])
    assert codes(st("Close", D), W(arrived=D, opened={D}, holding="cup"), capabilities=two) == ([], [])


def test_capabilities_from_manifest_dict():
    assert Capabilities.from_any({"hands": ["right"]}).hands == 1
    assert Capabilities.from_any({"hands": ["left", "right"]}).hands == 2
    assert Capabilities.from_any({"arms": 2}).hands == 2
    assert Capabilities.from_any(None) == am.DEFAULT_CAPABILITIES == Capabilities(hands=1, find_reaches=True)
    errs, _ = codes(st("Open", D), W(arrived=D, holding="cup"), capabilities={"hands": [0, 1]})
    assert errs == []


def test_find_only_looks():
    look = Capabilities(find_reaches=False)
    assert codes(st("Open", D), W(arrived=T, found=D), capabilities=look)[0] == ["interact_without_find"]
    assert codes(st("PutIn", D), W(arrived=T, found=D, holding="cup", opened={D}),
                 capabilities=look)[0] == ["place_wrong_location"]
    assert codes(st("PutIn", D), W(arrived=T, found=D, holding="cup", opened={D}))[0] == []


# ── context arguments ────────────────────────────────────────────────────────
def test_object_places_find_wrong_place():
    assert codes(st("Find", "cup"), W(arrived=S), object_places={"cup": T})[0] == ["find_wrong_place"]
    assert codes(st("Find", "cup"), W(arrived=T), object_places={"cup": T})[0] == []


def test_openable_predicate():
    pred = lambda k: "drawer" in k
    assert codes(st("Open", S), W(arrived=S), openable=pred)[0] == ["not_openable"]
    assert codes(st("Open", D), W(arrived=D), openable=pred)[0] == []
    assert codes(st("Open", S), W(arrived=S))[0] == []          # open vocabulary by default


def test_actions_vocabulary():
    assert codes(st("Say", "hi"), W(), actions={"Say", "MoveTo"}) == ([], [])
    assert codes(st("Pick", "cup"), W(), actions={"Say"})[0] == ["unknown_action"]


def test_container_matching():
    assert am.is_container("Fridge") and am.is_container("drawer@bedroom") and am.is_container("kitchen drawer")
    assert not am.is_container("TissueBox") and not am.is_container("CounterTop") and not am.is_container(S)
    assert am.is_container(S, containers=lambda k: k == S)


def test_aliases_are_normalized():
    assert codes({"action": "grab", "object": "cup"}, W(arrived=T))[0] == ["no_find_before_pick"]


# ── apply ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("world,step,expect", [
    (W(arrived=T, found="cup"), st("MoveTo", S), dict(arrived=S, found=None)),
    (W(arrived=T, found=D), st("MoveTo", D), dict(arrived=D, found=D)),
    (W(arrived=T, found="cup"), st("Pick"), dict(holding="cup", found=None)),
    (W(arrived=T, found="apple"), st("Pick", "cup"), dict(holding="cup", found=None)),
    (W(arrived=S, holding="cup"), st("Place", S), dict(holding=None, arrived=S)),
    (W(arrived=D), st("Open", D), dict(opened={D})),
    (W(arrived=D, opened={D}), st("Close", D), dict(opened=set())),
    (W(found="lamp"), st("TurnOn", "lamp"), dict(on={"lamp"})),
    (W(found="lamp", on={"lamp"}), st("TurnOff", "lamp"), dict(on=set())),
])
def test_apply(world, step, expect):
    before = world.copy()
    out = apply(step, world)
    for k, v in expect.items():
        assert getattr(out, k) == v, k
    assert world == before                       # copy by default
    apply(step, world, inplace=True)
    assert world == out


def test_apply_on_duck_typed_world():
    class Wd:
        def __init__(self):
            self.arrived, self.found, self.holding, self.opened, self.on = T, "cup", None, set(), set()
    w = Wd()
    out = apply(st("Pick"), w)                    # no .copy(): deep-copied
    assert out.holding == "cup" and w.holding is None


# ── simulate ─────────────────────────────────────────────────────────────────
PLAN = [st("MoveTo", D), st("Open", D), st("MoveTo", T), st("Find", "cup"), st("Pick", "cup"),
        st("MoveTo", D), st("PutIn", D), st("Close", D)]


def test_simulate_ok_plan():
    r = simulate(PLAN)
    assert r.ok and r.num_steps == len(PLAN) and r.final_state.opened == set() and r.final_state.holding is None


def test_simulate_open_twice_and_warnings():
    r = simulate([st("MoveTo", D), st("MoveTo", D), st("Open", D), st("Open", D)], stop_on_error=False)
    assert [(v.index, v.code) for v in r.violations] == [(3, "already_open")]
    assert [(v.index, v.code) for v in r.warnings] == [(1, "already_there")]


def test_simulate_apply_on_error_avoids_cascades():
    plan = [st("MoveTo", T), st("Pick", "cup"), st("MoveTo", S), st("Place", S)]
    skip = simulate(plan, stop_on_error=False)
    assert [v.code for v in skip.violations] == ["no_find_before_pick", "place_without_holding"]
    opt = simulate(plan, stop_on_error=False, apply_on_error=True)
    assert [v.code for v in opt.violations] == ["no_find_before_pick"]


def test_simulate_legacy_signature():
    s0 = W(arrived=D, opened={D})
    a = verifier.simulate([st("Close", D)], initial_state=s0)
    b = simulate([st("Close", D)], s0)
    assert a.ok and b.ok and a.final_state == b.final_state
    s1 = W()
    verifier._apply(st("MoveTo", T), s1)
    assert s1.arrived == T


def test_version_and_public_names():
    assert am.ACTION_MODEL_VERSION == embodiplan.ACTION_MODEL_VERSION == "1.0"
    assert embodiplan.__version__.startswith("0.2.")
    assert verifier.SymbolicState is am.SymbolicState and verifier.simulate is am.simulate


def test_reference_plans_verdicts_unchanged():
    """The bundled reference plans keep their 0.1 verdict counts (no regression on the benchmarks)."""
    want = {"eval_dataset.json": 8, "eval_dataset_expanded.json": 21, "eval_dataset_gt.json": 12,
            "eval_dataset_procthor.json": 8, "eval_dataset_verified.json": 12}
    for f in sorted(glob.glob(os.path.join(ROOT, "eval_dataset*.json"))):
        d = json.load(open(f))
        samples = d["samples"] if isinstance(d, dict) else d
        n_ok = sum(simulate(verifier.normalize_plan(s["reference_steps"])).ok for s in samples)
        assert n_ok == want[os.path.basename(f)], f


# ── SAGE public editing surface ──────────────────────────────────────────────
def _sage(reply):
    p = embodiplan.sage(top_k=1)
    calls = []

    def chat(messages, temperature=0.0, **kw):
        calls.append(messages)
        return reply, 1, 1
    p._chat = chat
    return p, calls


def test_edit_block_wraps_refine():
    from embodiplan.sage import SubgoalBlock
    p, calls = _sage('{"steps": [{"action": "MoveTo", "object": "Fridge"}, {"action": "Open", "object": "Fridge"}]}')
    blk = SubgoalBlock(name="open the fridge", steps=[st("Open", "Fridge")])
    out = p.edit_block(blk, "Checker findings: MoveTo Fridge first", task="open the fridge",
                       state=W(arrived="Kitchen"), examples_block="")
    assert out == [st("MoveTo", "Fridge"), st("Open", "Fridge")]
    assert blk.steps == [st("Open", "Fridge")]            # the block itself is not changed
    user = calls[0][-1]["content"]
    assert "MoveTo Fridge first" in user and "arrived: Kitchen" in user


def test_flatten_public_and_alias():
    from embodiplan.sage import SubgoalBlock, _flatten, flatten
    blocks = [SubgoalBlock("a", [st("MoveTo", D), st("Open", D)]),
              SubgoalBlock("b", [st("MoveTo", D), st("Open", D), st("Close", D)])]
    assert flatten(blocks) == _flatten(blocks) == [st("MoveTo", D), st("Open", D), st("Close", D)]


def test_last_blocks_and_retrieve_examples():
    p, _ = _sage('{"steps": []}')
    assert p.last_blocks is None
    ex, block = p.retrieve_examples("make coffee")
    assert isinstance(ex, list) and isinstance(block, str)


def test_sage_gate_uses_capabilities():
    two = embodiplan.SAGEPlanner(capabilities={"hands": 2}, enable_memory=False)
    one = embodiplan.SAGEPlanner(enable_memory=False)
    plan = [st("MoveTo", T), st("Find", "cup"), st("Pick", "cup"), st("MoveTo", D), st("Open", D)]
    assert two._simulate(plan).ok
    assert one._simulate(plan).violations[0].code == "hand_full"
