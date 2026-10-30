# SAGE's retrieval memory

SAGE puts the top-k most similar past plans into its prompts (`MemoryRetriever`,
`embodiplan/memory_retriever.py`). The examples come from two pools:

| pool | where | written by |
|---|---|---|
| seed | `gt_path`: `eval_dataset_gt.json` (AI2-THOR reference plans), or a robot's curated `.jsonl` seed file | a person, verified offline |
| live | `live_path`: one JSON line per episode | `SAGEPlanner.record_episode` after a verified run |

A retrieved example is copied almost word for word into the next plan, so a wrong
example costs more than no example. The rules below keep both pools clean.

## Episode record

```json
{"task": "Open the drawer in the kitchen.", "reasoning": "Recorded from a successful episode.",
 "plan_text": "{\"steps\": [{\"action\": \"MoveTo\", \"object\": \"drawer@kitchen\"}, {\"action\": \"Open\", \"object\": \"drawer@kitchen\"}]}",
 "ts": 1791520000.0, "verified": true, "init_state": {"arrived": null, "found": null, "holding": null, "opened": [], "on": []},
 "action_model": "1.0", "evidence": "goal_observation"}
```

`task`, `reasoning`, `plan_text` and `ts` are the fields every version wrote. Since 0.2.3
`record_episode` adds `verified`, `init_state` (the belief the plan started from), the
action-model version and the caller's `meta` (robot_agent adds how the goals were verified and
the run id). A seed `.jsonl` file uses the same fields; `steps` (a list) may replace
`plan_text`.

## What gets recorded

`SAGEPlanner.record_episode(task, plan, success=True, *, meta=None)` records only when
`success` is true and the plan passes the action model from its start belief (the belief of
the last `generate_plan` for that task). The caller decides what "success" means: robot_agent
passes only runs whose goals were looked at after the task (see robot_agent
`docs/memory.md`).

## Cleaning a pool

```
python -m embodiplan.memory_tools clean IN.jsonl --out OUT.jsonl \
    [--exclude-tasks FILE] [--capabilities JSON|MANIFEST] [--require-verified] \
    [--max-per-task N] [--near 0.92] [--report]
```

IN is only read (OUT must be another file). The summary lists the dropped episodes per
reason; `--report` lists each one with its line number:

| reason | dropped when |
|---|---|
| `unparseable` | the line is not JSON, or has no task or no plan |
| `excluded` | the task is in the exclude file (a test set): same task or a near match |
| `unverified` | `verified: false`; no `verified: true` with `--require-verified` (legacy records have no flag); or the plan fails the action model from `init_state` / `belief` (else from an empty state) |
| `duplicate` | an earlier kept episode has the same task and the same plan |
| `near_duplicate` | an earlier kept episode has a near-same task and the same plan once repeated steps collapse (`Find mug, Find mug` = `Find mug`) |
| `over_cap` | `--max-per-task N`: the task already has N kept episodes (the shortest plans are kept) |

The exclude file is plain text (one task per line, `#` comments), JSON (a list, a
`{"samples": [...]}` dataset with `task_desc`, or a mapping id -> task / row), or JSONL with
`task`. `--capabilities` is the robot body the action model assumes, as JSON
(`'{"hands": 1, "find_reaches": false}'`) or a manifest file (its `capabilities`).

Clean a deployed pool in three steps: stop the agent (it appends to the file), run `clean` into
a new file, check the summary, then swap the files and keep the old one as a backup.

## Same task, near-duplicate

`memory_tools.task_key` lower-cases the text, keeps words only and drops filler words
(`the`, `a`, `please`, ...). Two tasks are the same when their keys are equal or at least
`--near` similar (difflib ratio, default 0.92: a typo, a missing word). A different object or
place is a different task: "... to the table" and "... to the dining table" are not merged.

## Leave-one-out retrieval (evaluation)

An evaluation whose test tasks are also in memory measures recall, not planning: SAGE
retrieves the answer. With leave-one-out the retriever never returns an example whose task is
the query or a near-duplicate of it:

```python
SAGEPlanner(..., leave_one_out=True)
MemoryRetriever(MemoryRetrieverConfig(..., leave_one_out=True, loo_threshold=0.92))
memory.retrieve(task, k=3, leave_one_out=True)
```

`MEM_LEAVE_ONE_OUT=1` turns it on for every retriever in the process (the older switch; it
used to drop exact matches only). Leave it off on a robot: there a past plan of the same task
is exactly what memory is for.
