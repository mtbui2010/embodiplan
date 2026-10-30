# embodiplan · Household Robot Task Planning

<div align="center">

**A pluggable, multi-backend LLM planning library for embodied AI — benchmarked end-to-end in AI2-THOR**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Ollama](https://img.shields.io/badge/backend-Ollama-orange.svg)](https://ollama.ai)
[![OpenAI](https://img.shields.io/badge/backend-OpenAI-412991.svg)](https://openai.com)
[![Anthropic](https://img.shields.io/badge/backend-Anthropic-blue.svg)](https://anthropic.com)

</div>

---

## What this project does

embodiplan is a **Python package** that takes a natural-language task description and a list of visible objects, calls an LLM using one of eleven planning algorithms, and returns a structured action plan ready to be executed by a household robot.

```python
import embodiplan

planner = embodiplan.cot(provider="openai", model="gpt-4o-mini", api_key="sk-...")

steps, metrics = planner.generate_plan(
    task="make a cup of coffee",
    obs="Kitchen. Coffee machine on counter. Mug on shelf.",
    visible_objects=["CoffeeMachine", "Mug", "CounterTop", "Fridge"],
)

# steps →
# [{'action': 'Navigate', 'object': 'Mug',           'target': ''},
#  {'action': 'Grab',     'object': 'Mug',           'target': ''},
#  {'action': 'Navigate', 'object': 'CoffeeMachine', 'target': ''},
#  {'action': 'Place',    'object': 'Mug',            'target': 'CoffeeMachine'},
#  {'action': 'TurnOn',   'object': 'CoffeeMachine', 'target': ''}]

print(metrics.to_dict())
# {'method': 'CoT', 'latency_s': 2.3, 'llm_calls': 1,
#  'total_tokens': 580, 'num_steps': 5, 'parse_ok': True}
```

The companion **thor_app** connects embodiplan to [AI2-THOR](https://ai2thor.allenai.org/), providing a full loop: *natural language prompt → LLM plan → robot execution → goal verification → quantitative evaluation*.

---

## Live Demo

> **Try it in your browser — no local setup required**
>
> [Task Planning Demo](http://demo-planner.aistations.org)
>
> Select a room task, choose a planning method and LLM provider, and watch the robot
> navigate and interact with objects in a photorealistic kitchen/living room/bedroom/bathroom.
> Bring your own OpenAI or Anthropic API key, or use the Ollama backend running on the demo server.

---

## Repository layout

```
├── embodiplan/                  ← installable Python package (pip install -e ./embodiplan)
│   ├── __init__.py             ← public API: get(), list_methods(), factory functions
│   ├── base.py                 ← BasePlanner, PlanMetrics, LLMBackend
│   ├── direct.py               ← Method 1: Direct (baseline, 1 call)
│   ├── cot.py                  ← Method 2: Chain-of-Thought
│   ├── few_shot.py             ← Method 3: Few-Shot CoT
│   ├── self_refine.py          ← Method 4: Self-Refine (generate→critique→fix)
│   ├── react.py                ← Method 5: ReAct (Thought+Action per step)
│   ├── hierarchical.py         ← Method 6: Hierarchical (sub-goals → actions)
│   └── llm_router.py           ← Method 7: LLM Router (local generate + API verify)
│
└── thor_app/                    ← AI2-THOR demo application
    ├── app.py                   ← Streamlit UI
    ├── thor_server.py           ← AI2-THOR ZMQ server (skill primitives)
    ├── thor_client.py           ← ZMQ client
    ├── knowledge.py             ← 17 built-in task/scene definitions
    ├── make_dataset.py          ← static eval dataset (38 tasks, 3 difficulty levels)
    ├── make_dataset_from_sim.py ← ground dataset from real simulator state
    ├── record_reference.py      ← executable ground-truth plan recording
    ├── goal_checker.py          ← two-layer task-success verification
    ├── evaluate.py              ← offline benchmark (no simulator needed)
    └── evaluate_sim.py          ← online benchmark (plan + execute + verify)
```

---

## embodiplan — the package

### Eleven planning methods, one interface

| # | Method | LLM calls | Strategy | When to use |
|---|---|---|---|---|
| 1 | **Direct** | 1 | Single prompt → JSON plan | Speed baseline |
| 2 | **CoT** | 1 | `<reasoning>` then `<plan>` | Logical multi-step tasks |
| 3 | **Few-Shot CoT** | 1 | 3 built-in examples anchor the output | Consistent object naming |
| 4 | **Self-Refine** | 1 + 2N | Generate → critique → fix, N rounds | Highest plan quality |
| 5 | **ReAct** | N | Thought + Action interleaved | Step-by-step grounding |
| 6 | **Hierarchical** | 1 + N | Decompose to sub-goals, expand each | Complex multi-phase tasks |
| 7 | **LLM Router** | 2 | Local generates, frontier API verifies | Best of both, cost-efficient |
| 8 | **Hierarchical Few-Shot** | 1 + N | Hierarchical + retrieved few-shot examples | Strong hierarchical baseline |
| 9 | **SAGE** | 1 + N (+R) | Hierarchical + zero-token symbolic gate + sub-goal-local repair + hybrid memory | Valid plans, cheap recovery from failures |
| 10 | **LLM+P** | 1 | LLM writes a PDDL problem, a classical planner solves it (`pip install embodiplan[llmp]`) | Published baseline |
| 11 | **SayCan** | N | Greedy step-by-step: LLM value × verifier affordance | Published baseline |

Every method shares the same interface and returns the same `PlanMetrics` object.

### Three LLM backends

```python
# Ollama (local, free)
p = embodiplan.get("CoT", provider="ollama",    model="llama3.2",
                          host="http://localhost:11434")

# OpenAI
p = embodiplan.get("CoT", provider="openai",    model="gpt-4o-mini",
                          api_key="sk-...")

# Anthropic
p = embodiplan.get("CoT", provider="anthropic", model="claude-haiku-4-5-20251001",
                          api_key="sk-ant-...")
```

Or via shorthand factories:

```python
embodiplan.direct()
embodiplan.cot()
embodiplan.few_shot()
embodiplan.self_refine(max_iterations=3)
embodiplan.react(max_steps=12)
embodiplan.hierarchical()
embodiplan.llm_router(verifier_backend="anthropic", anthropic_api_key="sk-ant-...")
```

### Metrics every call returns

```python
steps, m = planner.generate_plan(task, obs, visible_objects)

m.latency_s        # wall-clock seconds
m.llm_calls        # round-trips (1 for Direct/CoT, N for ReAct)
m.total_tokens     # input + output tokens
m.tokens_per_step  # efficiency metric
m.parse_ok         # True if output parsed successfully
m.to_dict()        # serialisable dict for CSV / logging
```

### Replan on step failure

```python
new_steps, m = planner.replan(
    task           = "make coffee",
    completed      = steps_executed_so_far,
    failed_step    = {"action": "Grab", "object": "Mug", ...},
    failure_reason = "Mug not reachable from current position",
    obs            = updated_observation,
    visible_objects= updated_visible,
)
```

### Action model (public, versioned)

The symbolic rules (preconditions and effects) live in one module,
`embodiplan.action_model` (`ACTION_MODEL_VERSION`). SAGE's gate, SayCan's
affordance, the evaluation gate and robot_agent all call it.

```python
from embodiplan import action_model as am

am.check(step, world, capabilities={"hands": 1})   # -> [Violation(code, reason, severity)]
am.apply(step, world)                               # -> world after the step (a copy)
am.simulate(plan, world)                            # -> VerifierReport(ok, violations, warnings, final_state)
```

`world` is any object with `arrived`, `found`, `holding`, `opened`, `on`.
Rule changes are listed in [CHANGELOG.md](CHANGELOG.md). SAGE's sub-goal-local
edit is public too: `planner.edit_block(block, feedback, task=...)`,
`planner.last_blocks`, `planner.retrieve_examples(task)`, `embodiplan.sage.flatten(blocks)`.

---

## thor_app — the demo application

### Streamlit UI features

- **Task browser** — 17 built-in tasks across kitchen, living room, bedroom, bathroom; or free-form text input
- **Scene picker** — manual FloorPlan selector; scene auto-loads in AI2-THOR on change
- **Planning config** — method, provider, model, method-specific options (max steps, refine iterations)
- **Direct command box** — type `Navigate apple` / `Place mug → CoffeeMachine`, press Ctrl+Enter; object names resolve automatically (`coffee_machine` → `CoffeeMachine`)
- **Live camera feed** — frame from AI2-THOR updated after each executed step
- **Execution log** — per-step success/fail, reward, observation
- **Benchmark panel** — latency, LLM calls, tokens/step, parse rate across multiple runs

### Setup

```bash
# 1. Install embodiplan
pip install -e ./embodiplan

# 2. Install application dependencies
pip install -r ./thor_app/requirements.txt

# 3. Start the AI2-THOR server (leave running in a separate terminal)
cd thor_app
python thor_server.py

# 4. Start the Streamlit UI
cd thor_app
streamlit run app.py
```

> **Windows/WSL note:** if `pip install -e` fails, `app.py` contains an automatic
> `sys.path` fallback that resolves embodiplan at runtime without installation.

---

## Evaluation pipeline

### The ground-truth problem

LLM-generated or hand-written reference plans have no guarantee of executability.
`navigate coffee_machine` fails if AI2-THOR expects `CoffeeMachine`.
`TurnOn CoffeeMachine` succeeds as a step but doesn't make coffee if the mug was never placed.

This project addresses both problems with a three-stage pipeline.

### Stage 1 — Record executable ground truth

```bash
python record_reference.py          # --ref-source manual (default) or llm
# outputs: eval_dataset_gt.json
```

Loads each scene in AI2-THOR, executes candidate steps, and records **only steps that return `success=True` from the simulator**. Object names are auto-resolved to real AI2-THOR types. The saved plan is what the robot actually did — not what the LLM suggested.

### Stage 2 — Verify goal completion

```bash
python goal_checker.py --dataset eval_dataset_gt.json
# outputs: eval_dataset_verified.json
```

Step success ≠ task success. The checker uses two layers:

1. **GoalCondition** (deterministic) — per-task state assertions checked via the simulator. For *"make coffee"*: `mug placed on CoffeeMachine AND CoffeeMachine.isToggled == True`. Fast, no LLM needed.
2. **LLM judge** (semantic fallback) — when state is ambiguous, asks the LLM: *"Did these steps achieve the goal?"* Returns `success`, `confidence`, and a one-sentence `reason`.

### Stage 3 — Benchmark planners

```bash
# Offline: static plan quality metrics (no simulator)
python evaluate.py --methods Direct CoT "Self-Refine" Hierarchical \
                   --model llama3.2

# Online: execute plans in AI2-THOR, verify goal completion
python evaluate_sim.py --model llama3.2 --max-samples 10

# With OpenAI
python evaluate_sim.py --provider openai --model gpt-4o-mini --api-key sk-...

# Dry-run (no LLM, tests the pipeline)
python evaluate.py --dry-run
```

**Metrics output (CSV):**

| Group | Metrics |
|---|---|
| Plan quality | executability rate, precondition score, completeness, redundancy, hallucination rate |
| Efficiency | latency (s), LLM calls, total tokens, tokens per step |
| Robustness | replan success rate, replan latency, step overlap ratio |
| Execution | task success rate, step success rate, cumulative reward |
| Aggregate | quality score, efficiency score, combined score (0–1) |

---

## Design decisions

**Object name resolution** — AI2-THOR uses CamelCase (`CoffeeMachine`); LLMs generate snake_case (`coffee_machine`). embodiplan resolves this at execution time via a 4-step fuzzy matcher: exact match → CamelCase conversion → substring match → reverse substring. Applied to both LLM plans and direct user commands.

**Simulator as ground truth** — `record_reference.py` executes steps and records only those that succeed. This avoids the fundamental issue where LLM-generated reference plans may describe physically impossible or incorrectly sequenced actions.

**Two-layer goal verification** — `goal_checker.py` separates *step success* from *task success*, catching cases where all steps pass but the actual goal (e.g. coffee brewed, hands washed) was never reached.

**Backend-agnostic core** — `LLMBackend` in `base.py` routes to Ollama, OpenAI, or Anthropic through a single `chat()` interface. Every planner method and every planning algorithm works identically across all three backends with no code changes.

---

## Quick performance comparison

```python
import embodiplan

task  = "boil water in a pot on the stove"
obs   = "Kitchen. Stove visible. Pot on counter. Sink accessible."
vis   = ["Pot", "StoveBurner", "Sink", "Faucet", "CounterTop"]

for name in embodiplan.REGISTRY:
    p = embodiplan.get(name, model="llama3.2")
    steps, m = p.generate_plan(task, obs, vis)
    print(f"{name:15} {m.latency_s:5.1f}s  "
          f"{m.llm_calls} call(s)  {m.num_steps} steps  {m.total_tokens} tok")
```

```
Direct           1.8s  1 call(s)  6 steps   480 tok
CoT              2.9s  1 call(s)  7 steps   820 tok
Few-Shot CoT     3.1s  1 call(s)  6 steps   910 tok
Self-Refine      7.4s  5 call(s)  6 steps  2340 tok
ReAct           14.2s  6 call(s)  6 steps  1980 tok
Hierarchical     5.1s  3 call(s)  7 steps  1450 tok
LLM Router       4.3s  2 call(s)  6 steps   960 tok
```

---

## Install

```bash
pip install embodiplan              # core
pip install "embodiplan[memory]"    # + Chroma backend for SAGE's memory
pip install "embodiplan[llmp]"      # + pyperplan for the LLM+P baseline
pip install -e ./embodiplan         # from a source checkout
```

The curated 38-task AI2-THOR benchmark ships with the package:

```python
import embodiplan

planner = embodiplan.sage(model="qwen2.5:7b", gt_path=embodiplan.GT_DATASET)
```

`SAGE` was called `GRACE` in earlier versions; the old registry key `"GRACE"` still works.

**Core requirements:** Python 3.10+, `ollama>=0.2.0`, `requests>=2.31.0`

**For thor_app:** `streamlit`, `ai2thor`, `pyzmq`, `pillow`, `opencv-python`, `pandas`

---

<div align="center">
<sub>Built with <a href="https://ai2thor.allenai.org/">AI2-THOR</a> · <a href="https://ollama.ai">Ollama</a> · <a href="https://streamlit.io">Streamlit</a> · Python 3.10+</sub>
</div>