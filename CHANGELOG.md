# Changelog

All interface changes of embodiplan are listed here. Versions follow SemVer.
The action rules carry their own version, `embodiplan.ACTION_MODEL_VERSION`.

## 0.2.7 (action model 1.0)

### Added
- `SAGEPlanner.generate_plan(subgoals=[...])`: a sub-goal may carry its own steps, `(text, steps)` or
  `{"name": text, "steps": steps}`. That block is fixed: no expand call, no refine, and `edit_block`
  returns its steps unchanged (`SubgoalBlock.fixed`). Strings behave as before. `SAGEPlanner.fixed_subgoals`
  tells a caller the planner has this.

## 0.2.5 (action model 1.0)

SAGE's decompose answer is parsed robustly. No interface change.

### Fixed
- `SAGEPlanner` decompose: a sub-goal list a 7-8B model split into step words
  (`["MoveTo", "counter@kitchen", "Find", "apple", ...]`), wrote as step-shaped dicts
  (`{"action", "object"}`) or word lists, or followed by a second JSON object, now gives one
  sub-goal per step (`"MoveTo counter@kitchen"`, `"Find apple"`); the no-JSON fallback keeps
  quoted values, never JSON keys. Before, every word (and key) became a sub-goal that was
  expanded to a whole plan (T2: 61 steps in 22 blocks for a two-object task). The action names
  are the planner's (`actions`, else `ROBOT_ACTIONS`).

## 0.2.4 (action model 1.0)

SAGE plans with the robot's own action list. Additions only; clients pinned
`embodiplan>=0.2.1,<0.3` need no change.

### Added
- `SAGEPlanner(actions=None, robot_description=None)`: when given, the robot's actions
  replace the fixed `ACTIONS_STR` vocabulary (and the `STEP_SCHEMA` rules / `JSON_EXAMPLE`
  of actions the robot does not have) in the decompose, expand, refine and repair system
  prompts, and the description replaces the generic "household robot" line. The decompose
  prompt then also names the actions. Default None: prompts unchanged (golden test
  `tests/data/sage_prompts_0_2_3.txt`). `SAGEPlanner.system_prompts` holds the four prompts.
- `embodiplan.robot_prompt`: `system_prompts`, `step_schema`, `json_example`,
  `actions_block`, `action_name(s)`.

## 0.2.3 (action model 1.0)

SAGE's retrieval memory (SAGE_v2 report P0.8, G4, G5). Additions only; clients pinned
`embodiplan>=0.2.1,<0.3` need no change.

### Added
- `embodiplan.memory_tools` and its CLI
  `python -m embodiplan.memory_tools clean IN.jsonl --out OUT.jsonl [--exclude-tasks FILE] [--report]`:
  writes the episodes worth retrieving to OUT (IN is never written) and prints what was
  dropped and why: unparseable, `excluded` (a test-set task, exact or near match),
  `unverified` (`verified: false`, no flag with `--require-verified`, or the plan fails the
  action model from the episode's start belief), `duplicate`, `near_duplicate`, and
  `over_cap` (`--max-per-task N`). `--capabilities` takes a JSON body or a manifest file.
  Helpers: `task_key`, `same_task`, `plan_signature`, `verify_episode`, `clean`, `clean_file`,
  `load_tasks`. See docs/memory.md.
- Leave-one-out retrieval: `MemoryRetriever.retrieve(query, k, leave_one_out=True)`,
  `MemoryRetrieverConfig(leave_one_out=True, loo_threshold=0.92)` and
  `SAGEPlanner(leave_one_out=True)` never return an example whose task is the query or a
  near-duplicate of it. `MEM_LEAVE_ONE_OUT=1` still works and now drops near-duplicates too.
- JSONL seed pools: `gt_path` (and `load_seed_examples`) also reads a `.jsonl` file of
  episode records (`{task, reasoning?, plan_text | steps}`), e.g. a robot's curated seed
  pool. `memory_retriever.SEED_FORMATS == ("json", "jsonl")` tells clients it is supported.
- `MemoryRetriever.add_episode(..., meta=None)` stores extra fields with the record.

### Changed
- `SAGEPlanner.record_episode(task, plan, success=True, *, meta=None)`: a recorded episode
  now carries `verified: true`, `init_state` (the plan's start belief), `action_model` and the
  caller's `meta`. The record is still one JSON line; older readers ignore the new keys.
  Non-ASCII tasks are written as UTF-8 instead of `\u` escapes.

## 0.2.2 (action model 1.0)

SAGE suffix-repair fixes (SAGE_v2 report R1-R4, G1, G4). No signature removed; clients pinned
`embodiplan>=0.2.1,<0.3` need no change.

### Fixed
- R1: `replan` located the failed sub-goal by walking the pre-dedupe block sizes with the
  post-dedupe step count, so after `flatten` dropped a step it repaired the previous sub-goal.
  Every plan step now carries the index of its block (book-kept through `flatten`, completed
  steps and repairs; recomputed when the blocks are edited from outside, e.g. robot_agent's
  `edit_block` loop).
- R2: the repair state was re-simulated from an empty state, losing the belief (an already open
  drawer, an object already in hand). The state at the failure is now: `replan(..., state=)`
  when given, else the `Robot state:` line of `obs`, else the plan's starting belief replayed
  through the completed steps.
- R3: after one global fallback `_last_task` was the long "remaining" prompt, so every later
  `replan(task)` was global too. The plan's task stays `task`; the fallback's plan is book-kept
  after the completed steps.
- R4: a local repair did not write its suffix back, so a second failure was located against
  the stale decomposition. `last_blocks` now holds the repaired plan (done steps in their
  blocks, the current block ends with the suffix, later blocks as they were flattened).

### Changed
- `generate_plan` starts the symbolic gate from the belief (`state=` or the `Robot state:`
  line of `obs`) instead of an empty state, so a plan that starts with an object in hand or a
  container open is no longer "refined" to fix violations that are not there.
- A refine output is re-verified before use (`generate_plan` and `replan`): when it has more
  violations than its input, the input is kept (`metrics.extra["refine_rejected"]`, per block
  in `generate_plan`; `SubgoalBlock.refine_rejected`).
- `record_episode` records only verified episodes: `success` and the plan passes the symbolic
  gate (from the plan's starting belief for the last planned task). Returns True when recorded.
- The 259 bundled reference-plan verdicts are unchanged (the action model is not touched).

### Added
- `embodiplan.sage.belief_from_obs(obs) -> SymbolicState | None` (parses robot_agent's
  `WorldState.as_text()` and `SymbolicState.as_text()`).
- Keyword-only `state=` on `SAGEPlanner.generate_plan` and `SAGEPlanner.replan`.

## 0.2.1 (action model 1.0)

First release with the public action model. 0.2.0 was the version label before it existed, so
clients pin `embodiplan>=0.2.1,<0.3` to be sure the API below is present.

### Added
- `embodiplan.action_model`: the one public source of the action rules.
  `check(step, world, *, capabilities, containers, openable, object_places, actions) -> list[Violation]`,
  `apply(step, world, *, inplace=False) -> world`, `simulate(plan, world, ...) -> VerifierReport`,
  `Capabilities(hands=1, find_reaches=True)` (also built from a manifest `capabilities` dict),
  `Violation(code, reason, severity)`, `ACTION_MODEL_VERSION = "1.0"`. `world` is duck-typed
  (`arrived`, `found`, `holding`, `opened`, `on`), so robot_agent's `WorldState` works as is.
- `VerifierReport.warnings` (warnings never make `ok` False).
- `simulate(..., apply_on_error=True)`: optimistic replay that still applies a rejected step.
- SAGE editing surface: `SAGEPlanner.edit_block(block, feedback, *, task, state, obs,
  visible_objects, examples_block) -> steps`, `SAGEPlanner.last_blocks`,
  `SAGEPlanner.retrieve_examples(task)`, `embodiplan.sage.flatten(blocks)`, `embodiplan.sage.SubgoalBlock`.
- `SAGEPlanner(capabilities=...)`: the robot body the gate assumes.

### Changed (rules; error codes in brackets)
- Open an open container [`already_open`], TurnOn an appliance that is on [`already_on`]: errors.
- Open / Close with an object in hand on a one-hand robot [`hand_full`]. Pass
  `capabilities={"hands": 2}` for a body that can (e.g. to model AI2-THOR's agent).
- Pick an object other than the last Find [`pick_not_found`]; Find the object already in hand [`find_held`].
- Open / Close / TurnOn / TurnOff / Wash need the robot at the target or the target found
  (any found object was enough before) [`interact_without_find`]; Wash also accepts the object in hand.
  With `Capabilities(find_reaches=False)` (Find only looks) the robot must be at it.
- Open / Close / TurnOn / TurnOff without an object [`empty_object`].
- PutIn needs the robot at the container (or the container found, when Find reaches)
  [`place_wrong_location`]. Place puts the object on the receptacle, so a closed container is not
  an error for Place (the top of a closed chest of drawers is a surface); PutIn needs it open.
- Containers also match robot place keys by their head word (`drawer@bedroom`, `kitchen drawer`).
- MoveTo where the robot already is [`already_there`] and Serve with an empty hand
  [`serve_without_holding`] are warnings.
- New optional checks, only when the caller passes the context: Find away from the believed place
  of the object (`object_places`) [`find_wrong_place`], Open / Close of a non-openable target
  (`openable`) [`not_openable`], a custom action vocabulary (`actions`) [`unknown_action`].
- The verdicts of the 259 bundled reference plans are unchanged (same violations, same steps).

### Kept (aliases)
- `embodiplan.verifier`: `SymbolicState`, `StepViolation`, `VerifierReport`, `verify_step`,
  `simulate`, `_apply`, `normalize_*`, `DEFAULT_CONTAINERS`, `format_violations_for_llm` keep their
  signatures; they are now thin wrappers over `action_model`.
- `embodiplan.sage._flatten`, `SAGEPlanner._refine`, `._last_blocks`, `._retrieve_block` still exist.
