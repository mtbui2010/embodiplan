"""SAGE's system prompts take the robot's actions and description (0.2.4)."""

import pathlib

from embodiplan.base import ACTIONS_STR
from embodiplan.robot_prompt import action_names, json_example, step_schema, system_prompts
from embodiplan.sage import SAGEPlanner, _DEFAULT_SYSTEM

BASE = ["MoveTo <place>: drive to a place", "Say <text>: say it", "Dock", "WaitForUnload"]
ARM = ["MoveTo", "Find", "Pick", "Place", "PutIn", "Open", "Close"]


def planner(**kw):
    return SAGEPlanner(host="http://127.0.0.1:1", model="scripted", **kw)


def test_default_prompts_unchanged():
    p = planner()
    assert p.system_prompts == _DEFAULT_SYSTEM
    assert system_prompts(_DEFAULT_SYSTEM) == _DEFAULT_SYSTEM
    assert ACTIONS_STR in p._system("expand") and "household" in p._system("decompose")


def test_base_robot_prompts_have_only_its_actions():
    p = planner(actions=BASE, robot_description="A delivery base without an arm.")
    for key, text in p.system_prompts.items():
        for word in ("Pick", "Place", "Open", "TurnOn", "Wash", "household"):
            assert word not in text, (key, word)
        assert "A delivery base without an arm." in text
    assert "  Say <text>: say it" in p._system("expand")
    assert "MoveTo, Say, Dock, WaitForUnload" in p._system("decompose")


def test_arm_robot_keeps_the_container_rules():
    p = planner(actions=ARM)
    exp = p._system("expand")
    assert "Container rule" in exp and "Example with container" in exp
    assert "TurnOn" not in exp and "Wash" not in exp
    assert "household" in exp            # no description given: the default head stays


def test_schema_and_example_follow_the_actions():
    assert "Container rule" not in step_schema(["MoveTo", "Find", "Pick", "Place"])
    assert "Example with container" not in json_example(["MoveTo", "Find", "Pick", "Place"])
    assert '"Say"' in json_example(["Say", "MoveTo"])
    assert action_names(["MoveTo <x>: go", "Say", "MoveTo"]) == ["MoveTo", "Say"]


def test_defaults_byte_identical_to_0_2_3():
    """The default system prompts equal the ones 0.2.3 shipped (golden file)."""
    old = pathlib.Path(__file__).with_name("data") / "sage_prompts_0_2_3.txt"
    assert "\n=====\n".join(_DEFAULT_SYSTEM[k] for k in ("decompose", "expand", "refine", "repair")) \
        == old.read_text(encoding="utf-8")
