"""SAGE's symbolic gate knows the robot's own actions: a plan with Handover / Say / Dock the robot declares is no
``unknown_action`` (the gate used the household vocabulary and rejected every robot-specific action)."""
from embodiplan.sage import SAGEPlanner as SAGE

PLAN = [{"action": "MoveTo", "object": "table@kitchen"}, {"action": "Handover", "object": "person"}]


def _codes(sage):
    return [v.code for v in sage._simulate(PLAN).violations]


def test_robot_actions_are_in_the_gate_vocabulary():
    s = SAGE(actions=["MoveTo <place>: go", "Find <obj>: look", "Handover <person>: give the held object", "Say <text>: speak"],
             enable_memory=False)
    assert "unknown_action" not in _codes(s)
    assert s._simulate([{"action": "Say", "object": "hello"}, {"action": "Dock", "object": "charger"}]).violations[0].index == 1


def test_default_vocabulary_unchanged():
    s = SAGE(enable_memory=False)
    assert _codes(s) == ["unknown_action"]                     # no robot actions declared: the household list
    assert not s._simulate([{"action": "MoveTo", "object": "table@kitchen"}]).violations
