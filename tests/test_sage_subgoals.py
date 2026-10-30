"""SAGE's decompose answer parsed to one sub-goal per phase or step, never per word or JSON key (0.2.5).

T2 (qwen3-vl:8b): the model answered the sub-goals as step words ("MoveTo", "counter@kitchen", "Find",
...) or as two JSON objects in a row; every word became a sub-goal, each expanded to a whole plan
(61 steps, 22 blocks) and the client's gate spent its LLM budget editing them."""

import json

from embodiplan.sage import _parse_subgoals


def test_phrases_unchanged():
    raw = json.dumps({"subgoals": ["Put the apple on the counter", "Put the cracker box on the dining table"]})
    assert _parse_subgoals(raw) == ["Put the apple on the counter", "Put the cracker box on the dining table"]


def test_words_of_steps_are_joined_per_step():
    raw = json.dumps({"subgoals": ["MoveTo", "counter@kitchen", "Find", "apple", "Pick", "apple",
                                   "MoveTo", "dining table@kitchen", "Place", "apple"]})
    assert _parse_subgoals(raw) == ["MoveTo counter@kitchen", "Find apple", "Pick apple",
                                    "MoveTo dining table@kitchen", "Place apple"]


def test_step_shaped_items_and_two_objects():
    raw = ('{"subgoals": [{"action": "MoveTo", "object": "desk@study room"}, {"action": "Find", "object": "mug"}]}'
           ' {"subgoals": ["Go to the desk", "Pick up the mug"]}')
    assert _parse_subgoals(raw) == ["MoveTo desk@study room", "Find mug"]
    assert _parse_subgoals('[["MoveTo", "desk"], ["Find", "mug"]]') == ["MoveTo desk", "Find mug"]
    assert _parse_subgoals('{"subgoals": [{"subgoal": "fetch the mug"}]}') == ["fetch the mug"]


def test_no_json_keeps_values_not_keys():
    raw = 'Here: {"subgoals": ["MoveTo", "desk", "Find", "mug", "Pick"'
    assert _parse_subgoals(raw) == ["MoveTo desk", "Find mug", "Pick"]
    assert _parse_subgoals("```json\n{\"subgoals\": [\"Go to the sofa\"]}\n```") == ["Go to the sofa"]


def test_robot_action_names():
    raw = json.dumps({"subgoals": ["Dock", "charger@hall", "Say", "hello"]})
    assert _parse_subgoals(raw, actions=["Dock", "Say"]) == ["Dock charger@hall", "Say hello"]
    assert _parse_subgoals(raw) == ["Dock", "charger@hall", "Say", "hello"]
