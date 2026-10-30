# Using embodiplan

Pluggable LLM task planner for household robotics (AI2-THOR). Given a task
description + visible objects, it generates a step-by-step action plan.

## Usage

```python
import embodiplan

# Via registry (recommended)
planner = embodiplan.get("ReAct", host="http://ollama.aistations.org", model="llama3.2")
steps, metrics = planner.generate_plan(
    task="Make a cup of coffee using the coffee machine",
    obs="Kitchen scene loaded.",
    visible_objects=["CoffeeMachine", "Mug", "CounterTop"],
)

# Via shorthand
planner = embodiplan.react(model="llama3.2", max_steps=15)
```

## Registry keys

| Key | Class | Strategy |
|-----|-------|----------|
| `"Direct"` | `DirectPlanner` | Single LLM call → JSON plan |
| `"CoT"` | `CoTPlanner` | Reason first, then plan (2 calls) |
| `"Few-Shot CoT"` | `FewShotPlanner` | Built-in examples anchor output |
| `"Self-Refine"` | `SelfRefinePlanner` | Generate → critique → fix loop |
| `"ReAct"` | `ReActPlanner` | One Thought+Action per call (N calls, most accurate) |
| `"Hierarchical"` | `HierarchicalPlanner` | Sub-goals → action expansion |
| `"LLM Router"` | `LLMRouterPlanner` | Local generate + external API verify |
| `"Hierarchical Few-Shot"` | `HierarchicalFewShotPlanner` | Hierarchical + dynamic example retrieval |
| `"SAGE"` (alias `"GRACE"`) | `SAGEPlanner` | Sub-goal blocks, symbolic gate, suffix repair, memory examples (used by robot_agent) |
| `"LLM+P"` | `LLMPPlanner` | LLM to PDDL + classical planner |
| `"SayCan"` | `SayCanPlanner` | Score candidate actions per step |

`embodiplan.list_methods()` lists them; `ALIASES` maps deprecated names.

## Output

```python
steps: list[dict]      # [{"action": "MoveTo", "object": "Kitchen", ...}, ...]
metrics: PlanMetrics   # .latency_s, .llm_calls, .input_tokens, .output_tokens, .parse_ok
```

Step fields: `action`, `object` (CamelCase AI2-THOR name), `target` (for Place), `reason`.

Valid actions: `MoveTo Find Pick Place PutIn Open Close TurnOn TurnOff Wash Sit LieOn Serve Wait`

### A robot's own actions (SAGE, 0.2.4)

`SAGEPlanner(actions=[...], robot_description="...")` plans for one robot instead of the
generic household one: the decompose / expand / refine / repair system prompts list only
`actions` (bare names or lines `"Name <arg>: what it does"`), keep only the action rules,
container rule and JSON examples whose actions the robot has, and open with the robot's
description instead of "household robot". Both default to None: the prompts are then
byte-identical to 0.2.3 (`tests/data/sage_prompts_0_2_3.txt`). The helpers are in
`embodiplan.robot_prompt` (`system_prompts`, `step_schema`, `json_example`).
robot_agent passes both from the robot's manifest (its docs/planner_guide.md).

## Backends

Configured per planner via constructor args:

```python
planner = embodiplan.get("ReAct",
    host="http://ollama.aistations.org",   # Ollama URL
    model="llama3.2",
    provider="ollama",                      # "ollama" | "openai" | "gemini"
    api_key="",                             # for openai/gemini
)
```

## Key files

| File | What it does |
|------|-------------|
| `embodiplan/base.py` | `BasePlanner` ABC, `PlanMetrics`, `LLMBackend` (handles Ollama/OpenAI/Gemini) |
| `embodiplan/__init__.py` | `REGISTRY`, `get()`, shorthand factories |
| `embodiplan/react.py` | ReAct implementation |
| `embodiplan/hierarchical.py` | Hierarchical planner |
| `embodiplan/sage.py` | SAGE planner (robot_agent's backend) |
| `embodiplan/action_model.py` | The action rules (`ACTION_MODEL_VERSION`) shared with robot_agent |
| `apps/thor_knowledge.py` | Task/scene definitions for demo apps |
| `apps/app.py` | Standalone Streamlit demo |
