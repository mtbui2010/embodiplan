---
sources: [embodiplan/memory_retriever.py, embodiplan/memory_tools.py, embodiplan/sage.py, embodiplan/action_model.py, embodiplan/verifier.py, embodiplan/data/eval_dataset_gt.json, eval_dataset_gt.json, docs/memory.md, tests/test_memory_tools.py, tests/test_action_model.py]
verified_at: a69064255c69
---

# Retrieval memory (SAGE)

SAGE shows the LLM a few past plans that look like the new task. This page says where these plans come from, how they are found, and which plans are allowed in.

!!! abstract "In this page"
    - Memory has two pools: a fixed **seed** pool and a **live** pool that grows.
    - Examples are found by word overlap (Jaccard). Chroma is an option; it is not installed here, so it was not run.
    - Only plans that pass the symbolic gate are recorded.
    - Seed plans are not all valid under SAGE's own rules.

!!! code "Where in the code"
    [`MemoryRetriever`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/memory_retriever.py#L175-L356) and [`add_episode`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/memory_retriever.py#L228-L255); the caller is [`SAGEPlanner.record_episode`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/sage.py#L987-L1019).

## The idea

A past plan for a similar task is a good few-shot example. SAGE retrieves the top `top_k` (default 3) examples and puts them in its prompts as `=== RETRIEVED EXAMPLE 1 (GT, score=0.62) ===` blocks. `GT` marks a seed example, `EPISODE` a live one. The model tends to copy them closely, so a wrong example hurts more than none.

## Two pools

| pool | option | content |
|---|---|---|
| seed | `gt_path` | the bundled reference plans (`embodiplan.GT_DATASET`), or your own `.jsonl` file |
| live | `live_path` | one JSON line per recorded episode |

With the bundled file the pool is `{'seed': 37, 'live': 0}`. The file has 38 samples; one has no reference steps and is skipped. Seed reasoning is synthetic: it is the same pattern sentence for every seed. The live pool is loaded from `live_path` when the planner starts.

## How examples are found

By default the score is Jaccard similarity of lower-case word sets. With `use_chroma=True` and the `chromadb` package installed, Chroma is used first; if it is not installed or returns nothing, Jaccard is used. Chroma is not installed here, so that path was **not run**. For the task "make a cup of coffee", with a scripted setup (no LLM involved), the top three are:

```text
seed 0.625 Make a cup of coffee using the coffee machine
seed 0.182 Fill a cup with water from the sink
seed 0.154 Prepare a coffee and place it on the dining table
```

For evaluation, `leave_one_out=True` removes any example whose task is the query or a near copy, so the planner cannot just read the answer. Leave it off on a robot.

## What gets recorded

`record_episode` writes only when you say the run succeeded and the plan passes the [action model](../reference/action-model.md) from its start belief. Details are on [SAGE, repair and editing](../planners/sage-repair.md). One line of the live file looks like this (real output, plan recorded through `record_episode`; `ts` is a time stamp):

```json
{"verified": true, "action_model": "1.0", "evidence": "goal_observation", "init_state": {"arrived": null, "found": null, "holding": null, "opened": [], "on": []}, "task": "put the apple on the table", "reasoning": "Recorded from a successful episode.", "plan_text": "{\"steps\": [{\"action\": \"MoveTo\", \"object\": \"Fridge\"}, ...]}", "ts": 1794981850.0456762}
```

(The `plan_text` is cut here with `...`; the real line has all five steps.) `evidence` came from the `meta` argument; `task`, `reasoning`, `plan_text` and `ts` are always written.

!!! warning "Seed plans can break SAGE's own rules"
    Only 12 of the 38 bundled reference plans pass `action_model.simulate`. The most common first error is a `Pick` with no `Find` before it (19 plans). So a retrieved seed example can show a pattern that the SAGE gate rejects. (Counts from a run of the checker over the bundled file; a test in `tests/test_action_model.py` pins them.)

## Cleaning a pool

Dropping duplicates, unverified lines or test tasks from a pool is done with `python -m embodiplan.memory_tools clean`. It is described in the repository's [`docs/memory.md`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/docs/memory.md), and is not covered here.
