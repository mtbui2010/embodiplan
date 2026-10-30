"""SAGE memory tools (0.2.3): clean a live pool, seed pools in JSONL, leave-one-out retrieval.

Run: PYTHONPATH=. python -m pytest -q tests
"""
import json

import pytest

from embodiplan import memory_tools as mt
from embodiplan.memory_retriever import (SEED_FORMATS, MemoryRetriever, MemoryRetrieverConfig,
                                         load_seed_examples)
from embodiplan.sage import SAGEPlanner


def S(*pairs):
    return [{"action": a, "object": o} for a, o in pairs]


def ep(task, steps, **kw):
    return {"task": task, "reasoning": "r", "plan_text": json.dumps({"steps": steps}), "ts": 1.0, **kw}


MUG = S(("MoveTo", "desk@study room"), ("Find", "mug"), ("Pick", "mug"),
        ("MoveTo", "chest@bedroom"), ("Place", "chest@bedroom"))
MUG3 = S(("MoveTo", "desk@study room"), ("Find", "mug"), ("Find", "mug"), ("Find", "mug"), ("Pick", "mug"),
         ("MoveTo", "chest@bedroom"), ("Place", "chest@bedroom"))
OPEN_BAD = S(("Open", "drawer@kitchen"))
OPEN_OK = S(("MoveTo", "drawer@kitchen"), ("Open", "drawer@kitchen"))
APPLE_BAD = S(("MoveTo", "desk@study room"), ("Find", "apple"), ("Pick", "apple"), ("Find", "apple"))
CLOSE = S(("MoveTo", "drawer@kitchen"), ("Close", "drawer@kitchen"))
T_MUG = "Move the mug from the desk to the chest in the bedroom."


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


# ── task matching ─────────────────────────────────────────────────────
def test_same_task_ignores_case_punctuation_and_articles():
    assert mt.same_task(T_MUG, "move mug from desk to chest in bedroom")
    assert mt.same_task("Put the banana on the kitchen counter.", "Please put a banana on the kitchen countr")


def test_other_destination_is_another_task():
    assert not mt.same_task("Move the mug from the desk to the table.", "Move the mug from the desk to the dining table.")
    assert not mt.same_task("Open the drawer in the kitchen.", "Close the drawer in the kitchen.")


def test_plan_signature_collapses_repeats():
    assert mt.plan_signature(MUG3) == mt.plan_signature(MUG)
    assert mt.plan_signature(OPEN_BAD) != mt.plan_signature(OPEN_OK)


# ── verification ──────────────────────────────────────────────────────
def test_verify_episode_from_its_start_belief():
    assert not mt.verify_episode(ep("Close the kitchen drawer.", CLOSE))[0]          # closed already
    rec = ep("Close the kitchen drawer.", CLOSE, init_state={"opened": ["drawer@kitchen"]})
    assert mt.verify_episode(rec) == (True, "")
    rec = ep("Close the kitchen drawer.", CLOSE, belief="arrived=- found=- holding=- opened=[drawer@kitchen] on=[-]")
    assert mt.verify_episode(rec)[0]


def test_verify_episode_flags():
    assert not mt.verify_episode(ep(T_MUG, MUG, verified=False))[0]
    assert mt.verify_episode(ep(T_MUG, MUG))[0]
    ok, why = mt.verify_episode(ep(T_MUG, MUG), require_verified=True)
    assert not ok and "no goal verification" in why
    assert mt.verify_episode(ep(T_MUG, MUG, verified=True), require_verified=True)[0]


def test_capabilities_change_the_verdict():
    find_open = S(("Find", "drawer@kitchen"), ("Open", "drawer@kitchen"))
    assert mt.verify_episode(ep("Open the kitchen drawer.", find_open))[0]
    assert not mt.verify_episode(ep("Open the kitchen drawer.", find_open), {"find_reaches": False})[0]


# ── clean ─────────────────────────────────────────────────────────────
def test_clean_drops_each_kind_with_its_reason():
    rows = [ep(T_MUG, MUG), ep(T_MUG, MUG), ep(T_MUG, MUG3), ep("Open the drawer in the kitchen.", OPEN_BAD),
            ep("Bring the apple from the desk to the dining table.", APPLE_BAD), None, {"task": "x"},
            ep("Open the drawer in the kitchen.", OPEN_OK), ep("Put the banana on the kitchen counter.", MUG)]
    rep = mt.clean(rows, exclude_tasks=["Put the banana on the kitchen counter"])
    kinds = [(d.line, d.kind) for d in rep.dropped]
    assert kinds == [(2, "duplicate"), (3, "near_duplicate"), (4, "unverified"), (5, "unverified"),
                     (6, "unparseable"), (7, "unparseable"), (9, "excluded")]
    assert [r["task"] for r in rep.kept] == [T_MUG, "Open the drawer in the kitchen."]
    assert rep.counts() == {"duplicate": 1, "near_duplicate": 1, "unverified": 2, "unparseable": 2, "excluded": 1}
    assert "9 episodes in, 2 kept, 7 dropped" in rep.summary()


def test_max_per_task_keeps_the_shortest():
    long_ = MUG[:1] + S(("MoveTo", "sofa@living room")) + MUG[1:]
    rep = mt.clean([ep(T_MUG, long_), ep(T_MUG, MUG)], max_per_task=1)
    assert [len(mt.plan_steps(r)) for r in rep.kept] == [5]
    assert rep.dropped[0].kind == "over_cap" and rep.dropped[0].line == 1


def test_clean_file_never_edits_the_input(tmp_path, capsys):
    src = write_jsonl(tmp_path / "live.jsonl", [ep(T_MUG, MUG), ep(T_MUG, MUG), ep("Open it.", OPEN_BAD)])
    before = src.read_bytes()
    excl = tmp_path / "test.txt"
    excl.write_text("# test set\nOpen it.\n")
    out = tmp_path / "out" / "clean.jsonl"
    assert mt.main(["clean", str(src), "--out", str(out), "--exclude-tasks", str(excl), "--report"]) == 0
    assert src.read_bytes() == before
    assert len(out.read_text().splitlines()) == 1
    printed = capsys.readouterr().out
    assert "3 episodes in, 1 kept, 2 dropped" in printed and "line 3" in printed
    assert mt.main(["clean", str(src), "--out", str(src)]) == 2           # refuses to overwrite IN
    assert src.read_bytes() == before


def test_load_tasks_formats(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"samples": [{"task_desc": "Put the cup away."}]}))
    (tmp_path / "b.json").write_text(json.dumps({"S01": ["simple", "Bring the apple to the table.", []]}))
    (tmp_path / "c.jsonl").write_text(json.dumps({"task": "Open the drawer."}) + "\n")
    assert mt.load_tasks(str(tmp_path / "a.json")) == ["Put the cup away."]
    assert mt.load_tasks(str(tmp_path / "b.json")) == ["Bring the apple to the table."]
    assert mt.load_tasks(str(tmp_path / "c.jsonl")) == ["Open the drawer."]


def test_capabilities_from_a_manifest(tmp_path):
    m = tmp_path / "manifest.json"
    m.write_text(json.dumps({"robot": "r", "capabilities": {"hands": 2, "find_reaches": False}}))
    assert mt.load_capabilities(str(m)) == {"hands": 2, "find_reaches": False}
    assert mt.load_capabilities('{"hands": 1}') == {"hands": 1}


# ── seed pools and leave-one-out ──────────────────────────────────────
def test_jsonl_seed_pool(tmp_path):
    assert "jsonl" in SEED_FORMATS
    seed = write_jsonl(tmp_path / "seed.jsonl", [ep(T_MUG, MUG), {"task": "Open the kitchen drawer.", "steps": OPEN_OK}])
    ex = load_seed_examples(str(seed))
    assert [e.source for e in ex] == ["seed", "seed"]
    assert json.loads(ex[1].plan_text)["steps"][1] == {"action": "Open", "object": "drawer@kitchen"}
    m = MemoryRetriever(MemoryRetrieverConfig(gt_path=str(seed), live_path=str(tmp_path / "none.jsonl")))
    assert m.size == {"seed": 2, "live": 0}
    assert m.retrieve("open the drawer in the kitchen", k=1)[0].task == "Open the kitchen drawer."


def test_leave_one_out_never_returns_the_query(tmp_path, monkeypatch):
    monkeypatch.delenv("MEM_LEAVE_ONE_OUT", raising=False)
    live = write_jsonl(tmp_path / "live.jsonl", [ep(T_MUG, MUG), ep(T_MUG.lower().rstrip("."), MUG3),
                                                  ep("Move the mug from the desk to the dining table.", MUG),
                                                  ep("Open the drawer in the kitchen.", OPEN_OK)])
    m = MemoryRetriever(MemoryRetrieverConfig(live_path=str(live)))
    assert m.retrieve(T_MUG, k=1)[0].task == T_MUG                       # default: unchanged
    got = m.retrieve(T_MUG, k=3, leave_one_out=True)
    assert got and not any(mt.same_task(e.task, T_MUG) for e in got)
    assert got[0].task == "Move the mug from the desk to the dining table."
    m2 = MemoryRetriever(MemoryRetrieverConfig(live_path=str(live), leave_one_out=True))
    assert not any(mt.same_task(e.task, T_MUG) for e in m2.retrieve(T_MUG, k=4))
    monkeypatch.setenv("MEM_LEAVE_ONE_OUT", "1")
    assert not any(mt.same_task(e.task, T_MUG) for e in m.retrieve(T_MUG, k=4))


def test_sage_leave_one_out_option(tmp_path):
    live = write_jsonl(tmp_path / "live.jsonl", [ep(T_MUG, MUG), ep("Open the drawer in the kitchen.", OPEN_OK)])
    p = SAGEPlanner(host="http://127.0.0.1:1", model="x", live_path=str(live), leave_one_out=True)
    ex, block = p.retrieve_examples(T_MUG)
    assert [e.task for e in ex] == ["Open the drawer in the kitchen."] and T_MUG not in block


def test_record_episode_writes_verified_and_belief(tmp_path):
    live = tmp_path / "live.jsonl"
    p = SAGEPlanner(host="http://127.0.0.1:1", model="x", live_path=str(live))
    assert p.record_episode(T_MUG, MUG, meta={"evidence": "goal_observation"})
    assert not p.record_episode("Open it.", OPEN_BAD)
    rows = [json.loads(l) for l in live.read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["verified"] is True and r["evidence"] == "goal_observation" and r["action_model"]
    assert r["init_state"] == {"arrived": None, "found": None, "holding": None, "opened": [], "on": []}
    assert mt.clean(rows, require_verified=True).kept == rows
