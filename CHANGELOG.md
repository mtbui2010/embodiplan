# Changelog

All interface changes of embodiplan are listed here. Versions follow SemVer.
The action rules carry their own version, `embodiplan.ACTION_MODEL_VERSION`.

## 0.2.0 (action model 1.0)

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
