"""
evaluate_sim.py
===============
Execute generated plans inside AI2-THOR via ThorClient and measure
ground-truth execution metrics.

Requires:
  - python thor_server.py  (running in a separate terminal)
  - python make_dataset.py  (to generate eval_dataset.json first)

Metrics added on top of evaluate.py's static metrics:
  Execution:
    exec_task_success   — 1 if all steps succeeded OR env signals done
    exec_step_success   — fraction of steps that returned success=True
    exec_total_reward   — cumulative reward from simulator
    exec_steps_done     — steps actually executed (may stop early on fail)
    exec_replans        — number of replan calls triggered during execution
    exec_latency_s      — wall-clock time for the full execute loop

  Combined (static + execution):
    combined_score      — 0.5 × quality_score + 0.5 × exec_task_success

Usage:
    # All methods, default dataset:
    python evaluate_sim.py

    # Select methods / model / output:
    python evaluate_sim.py --methods Direct CoT --model llama3.2 --out sim_results.csv

    # Limit samples (faster for debugging):
    python evaluate_sim.py --max-samples 5

    # Dry-run LLM (still executes in simulator):
    python evaluate_sim.py --dry-run-llm

    # Skip simulator execution, combine with offline metrics only:
    python evaluate_sim.py --no-sim
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Any

# ── Resolve paths ─────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    import embodiplan
except ModuleNotFoundError:
    sys.path.insert(0, os.path.join(_HERE, "..", "embodiplan"))
    import embodiplan

from embodiplan import REGISTRY, DEFAULT_HOST, DEFAULT_MODEL, DEFAULT_BACKEND

try:
    from thor_app.sim_client import ThorClient
except ImportError:
    sys.path.insert(0, _HERE)
    from thor_app.sim_client import ThorClient

try:
    from make_dataset import (
        build_dataset, validate_dataset,
        score_executability, score_precondition, score_redundancy,
        score_completeness, score_hallucination, compute_quality_score,
    )
    from thor_app.evaluate.evaluate import (
        check_connection, compute_efficiency_score,
        compute_robustness_score, compute_overall_score,
        _DryRunPlanner, CSV_COLUMNS as OFFLINE_COLUMNS,
        SampleResult as OfflineSampleResult,
    )
except ImportError:
    sys.path.insert(0, _HERE)
    from make_dataset import (
        build_dataset, validate_dataset,
        score_executability, score_precondition, score_redundancy,
        score_completeness, score_hallucination, compute_quality_score,
    )
    from thor_app.evaluate.evaluate import (
        check_connection, compute_efficiency_score,
        compute_robustness_score, compute_overall_score,
        _DryRunPlanner, CSV_COLUMNS as OFFLINE_COLUMNS,
        SampleResult as OfflineSampleResult,
    )


# ═════════════════════════════════════════════════════════════════════
# Result dataclass — extends offline result with execution columns
# ═════════════════════════════════════════════════════════════════════

SIM_EXTRA_COLUMNS = [
    "exec_task_success",
    "exec_step_success",
    "exec_total_reward",
    "exec_steps_done",
    "exec_replans",
    "exec_latency_s",
    "sim_error",
    "combined_score",
]

SIM_CSV_COLUMNS = list(OFFLINE_COLUMNS) + SIM_EXTRA_COLUMNS
# Strict verifier-grounded precondition (added for the verifier-predicts-execution
# analysis); insert next to the heuristic 'precondition' if not already present.
if "precondition_strict" not in SIM_CSV_COLUMNS:
    if "precondition" in SIM_CSV_COLUMNS:
        SIM_CSV_COLUMNS.insert(SIM_CSV_COLUMNS.index("precondition") + 1, "precondition_strict")
    else:
        SIM_CSV_COLUMNS.append("precondition_strict")
if "exec_goal_method" not in SIM_CSV_COLUMNS:
    if "exec_task_success" in SIM_CSV_COLUMNS:
        SIM_CSV_COLUMNS.insert(SIM_CSV_COLUMNS.index("exec_task_success") + 1, "exec_goal_method")
    else:
        SIM_CSV_COLUMNS.append("exec_goal_method")
# Runtime safety monitor (verify-before-execute gate) columns. Additive; mirror
# the insert pattern above. These are written for every row regardless of mode
# (gate_on=0 when the gate is OFF) so a single CSV is self-describing and
# gate-OFF runs remain byte-comparable to historical runs except for the three
# new trailing columns.
for _gcol in ("gate_on", "gate_prevented", "gate_repairs"):
    if _gcol not in SIM_CSV_COLUMNS:
        SIM_CSV_COLUMNS.append(_gcol)
# Failure-recovery (injected-failure) columns. Additive; populated only under
# --inject-fail, default 0 otherwise so historical runs stay comparable.
for _rcol in ("has_fail_injection", "replan_ok", "replan_latency_s",
              "replan_steps", "replan_llm_calls"):
    if _rcol not in SIM_CSV_COLUMNS:
        SIM_CSV_COLUMNS.append(_rcol)


@dataclass
class SimSampleResult:
    # ── Copy of offline fields ──
    method:     str   = ""
    model:      str   = ""
    backend:    str   = ""
    task_id:    str   = ""
    task_desc:  str   = ""
    room:       str   = ""
    difficulty: str   = ""

    parse_ok:        float = 0.0
    num_steps:       int   = 0
    ref_steps:       int   = 0
    step_ratio:      float = 0.0
    executability:   float = 0.0
    precondition:    float = 0.0
    precondition_strict: float = 0.0
    redundancy:      float = 0.0
    completeness:    float = 0.0
    hallucination:   float = 0.0
    quality_score:   float = 0.0

    latency_s:        float = 0.0
    llm_calls:        int   = 0
    input_tokens:     int   = 0
    output_tokens:    int   = 0
    total_tokens:     int   = 0
    tokens_per_step:  float = 0.0
    efficiency_score: float = 0.0

    has_fail_injection: int   = 0
    replan_ok:          float = 0.0
    replan_latency_s:   float = 0.0
    replan_steps:       int   = 0
    replan_llm_calls:   int   = 0
    step_overlap:       float = 0.0
    robustness_score:   float = 0.0

    overall_score: float = 0.0
    error:         str   = ""

    # ── Execution-specific fields ──
    exec_task_success: float = 0.0   # 1.0 if task completed successfully
    exec_goal_method:  str   = ""    # goal_condition | llm_judge | combined | no_check | err:*
    exec_step_success: float = 0.0   # fraction of steps that returned success
    exec_total_reward: float = 0.0   # cumulative reward from simulator
    exec_steps_done:   int   = 0     # how many steps were actually executed
    exec_replans:      int   = 0     # replan calls triggered
    exec_latency_s:    float = 0.0   # wall time for execution loop
    sim_error:         str   = ""    # simulator-level error
    combined_score:    float = 0.0   # 0.5×quality + 0.5×exec_task_success

    # ── Runtime safety monitor (verify-before-execute gate) fields ──
    # Populated only when --verify-gate is ON; default values keep gate-OFF rows
    # identical in meaning to historical runs (gate_on=0, no prevented actions).
    gate_on:        int = 0   # 1 if the pre-execution verifier gate was active
    gate_prevented: int = 0   # steps the gate BLOCKED before they reached the sim
    gate_repairs:   int = 0   # pre-emptive planner.replan calls the gate fired

    def to_row(self) -> dict:
        return asdict(self)


# ═════════════════════════════════════════════════════════════════════
# Simulator connection check
# ═════════════════════════════════════════════════════════════════════

def check_simulator(host: str, port: int) -> tuple[bool, str]:
    """Ping ThorServer and return (ok, message)."""
    try:
        client = ThorClient(host=host, port=port)
        if client.connected:
            return True, f"ThorServer OK at {host}:{port}"
        return False, (
            f"ThorServer not responding at {host}:{port}\n"
            f"  Fix: run  python thor_server.py  in a separate terminal"
        )
    except Exception as e:
        return False, (
            f"Cannot connect to ThorServer at {host}:{port}\n"
            f"  Error: {e}\n"
            f"  Fix: run  python thor_server.py  in a separate terminal"
        )


# ═════════════════════════════════════════════════════════════════════
# Core: execute plan in simulator
# ═════════════════════════════════════════════════════════════════════

def _dump_eval_bundle(bundle_dir, planner, sample, completed, object_map,
                      goal_success, goal_method, verdict, client):
    """Save one end-to-end human-evaluation record: final RGB frame, the final
    states of the task's expected objects, the executed plan, and the auto
    goal-checker verdict. Appended to <bundle_dir>/items.jsonl (+ frames/)."""
    import json as _json
    frames_dir = os.path.join(bundle_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    name = getattr(planner, "name", "method")
    tid = str(sample.get("task_id", "task"))
    fid = f"{name}__{tid}".replace("/", "_").replace(" ", "")
    frame_rel = ""
    try:
        img = client.get_frame()
        if img is not None:
            img.save(os.path.join(frames_dir, fid + ".jpg"), quality=85)
            frame_rel = "frames/" + fid + ".jpg"
    except Exception:
        pass
    KEYS = ("isOpen", "isToggled", "isPickedUp", "parentReceptacles",
            "receptacleObjectIds", "isSliced", "isCooked", "ObjectTemperature")
    states = {}
    for ot in sample.get("expected_objects", []):
        o = object_map.get(ot)
        if o:
            states[ot] = {k: o.get(k) for k in KEYS if k in o}
    rec = {
        "id": fid, "method": name, "task_id": tid,
        "task_desc": sample.get("task_desc", ""),
        "expected_objects": sample.get("expected_objects", []),
        "difficulty": sample.get("difficulty", ""), "room": sample.get("room", ""),
        "scene": sample.get("scene", ""),
        "plan": [f'{s.get("action","")} {s.get("object","")}'.strip() for s in (completed or [])],
        "auto_task_success": goal_success, "goal_method": goal_method,
        "goal_reason": getattr(verdict, "reason", "") if verdict is not None else "",
        "final_states": states, "frame": frame_rel,
    }
    with open(os.path.join(bundle_dir, "items.jsonl"), "a") as f:
        f.write(_json.dumps(rec) + "\n")


def execute_plan_in_sim(
    client:    ThorClient,
    planner,
    sample:    dict,
    plan:      list[dict],
    max_replan: int = 3,
    verbose:   bool = False,
    verify_gate: bool = False,
    inject_fail_at: int = -1,
    fail_reason: str = "",
) -> dict:
    """
    Execute a generated plan step-by-step in AI2-THOR.

    When ``inject_fail_at >= 0`` a single mid-execution failure is INJECTED: the
    step reached after ``inject_fail_at`` successful executions is forced to fail
    (without touching the robot), exactly as if the simulator had rejected it.
    This drives the planner's recovery path so the COST of recovery can be
    measured (replan latency / steps / llm-calls) — the failure-recovery
    benchmark. The recovery from THIS injected failure is the one whose cost is
    reported; ``replan_ok`` records whether the task still completed afterwards.

    When ``verify_gate`` is True the function becomes VERIFY-BEFORE-EXECUTE: a
    running symbolic state (embodiplan.verifier.SymbolicState) is replayed from
    the steps that have ALREADY executed successfully, and every upcoming step is
    checked with ``verify_step`` BEFORE it is sent to the simulator. A step the
    verifier predicts will violate its preconditions is never dispatched to the
    robot; instead the SAME pre-emptive ``planner.replan`` machinery is invoked to
    repair the suffix, and only verifier-passing steps reach ``client.step``.
    Default (verify_gate=False) is the unchanged execute-then-replan path.

    Returns a dict with execution metrics:
        task_success, step_success, total_reward,
        steps_done, replans, latency_s, error,
        gate_on, gate_prevented, gate_repairs
    """
    t0               = time.perf_counter()
    completed        = []
    current_plan     = list(plan)
    replan_count     = 0
    steps_attempted  = 0
    steps_succeeded  = 0
    total_reward     = 0.0
    task_done        = False
    sim_error        = ""
    obs              = ""
    goal_success     = None      # set by the grounded goal-checker (preferred over the reward proxy)
    goal_method      = ""

    # ── Runtime safety monitor state (verify-before-execute gate) ──
    # All imports are local and import-guarded so a minimal install without the
    # verifier still runs the unchanged execute-then-replan path.
    gate_prevented   = 0         # steps blocked by the gate before reaching the sim
    gate_repairs     = 0         # pre-emptive replan calls fired by the gate
    # ── Failure-recovery (injected-failure) capture state ──
    _injected        = False     # has the single injected failure fired yet?
    _capture_replan  = False     # is the NEXT replan the injected-recovery one?
    rec_latency      = 0.0       # cost of the injected-failure recovery
    rec_steps        = 0         # #steps in the repaired suffix
    rec_llm_calls    = 0
    rec_in_tok       = 0
    rec_out_tok      = 0
    rec_attempted    = 0         # 1 once an injected failure has been triggered
    _gate_ready      = False
    _vsim = _vverify = _vnstep = _vnplan = None
    if verify_gate:
        try:
            from embodiplan.verifier import (
                simulate as _vsim,
                verify_step as _vverify,
                normalize_step as _vnstep,
                normalize_plan as _vnplan,
            )
            _gate_ready = True
        except Exception as _ve:
            # Verifier unavailable → degrade safely to the baseline path.
            if verbose:
                print(f"    ⚠  verify-gate disabled (verifier import failed: {_ve})")
            _gate_ready = False
    # Guard against an infinite gate→replan→gate loop on a persistently-bad
    # step: cap consecutive prevents-without-progress, then drop-and-continue.
    _consecutive_prevented = 0

    try:
        # Reset simulator to the correct scene.
        # ProcTHOR samples (sim_type=="procthor", set by build_procthor_dataset)
        # are loaded by (split, house_index) instead of a FloorPlan string; the
        # default iTHOR path is unchanged. Backward-compatible additive branch.
        _meta = sample.get("_meta") or {}
        is_procthor = (sample.get("sim_type") == "procthor"
                       or _meta.get("simulator_type") == "procthor")
        if is_procthor:
            split = sample.get("split", _meta.get("split", "val"))
            house_index = sample.get("house_index", _meta.get("house_index"))
            resp = client.reset(simulator_type="procthor", split=split,
                                house_index=house_index)
        else:
            scene = sample.get("scene", "FloorPlan1")
            resp  = client.reset(scene)
        if resp.get("status") != "ok":
            return {
                "task_success": 0.0, "step_success": 0.0,
                "total_reward": 0.0, "steps_done": 0,
                "replans": 0, "latency_s": 0.0,
                "error": f"Scene reset failed: {resp.get('msg','')}",
            }

        obs             = resp.get("obs", "")
        visible_objects = resp.get("visible_objects", [])

        # Execute steps
        while current_plan:
            step   = current_plan.pop(0)
            action = step.get("action", "Wait")
            obj    = step.get("object", "")
            target = step.get("target", "")

            if verbose:
                print(f"    → {action} {obj}" + (f" → {target}" if target else ""))

            # ── VERIFY-BEFORE-EXECUTE gate ──────────────────────────────────
            # Replay the successfully-executed prefix symbolically to get the
            # state AT THE CURSOR, then check the upcoming step. A predicted
            # precondition violation is repaired pre-emptively (or dropped) and
            # the step is NEVER sent to the robot. This is zero-token / O(|π|).
            if _gate_ready:
                try:
                    _state = _vsim(
                        _vnplan(completed),
                        visible_objects=visible_objects,
                        stop_on_error=False,
                    ).final_state
                    _ok, _code, _reason = _vverify(_vnstep(step), _state)
                except Exception as _ge:
                    # Never let a gate error break execution — fall through to
                    # the normal execute-then-replan path for this step.
                    _ok, _code, _reason = True, None, None
                    if verbose:
                        print(f"    ⚠  gate eval error ({_ge}); executing normally")

                if not _ok:
                    gate_prevented += 1
                    _consecutive_prevented += 1
                    if verbose:
                        print(f"    ⛔  gate blocked: [{_code}] {_reason} "
                              f"(prevented #{gate_prevented})")
                    # Pre-emptive repair: ask the planner only for a corrected
                    # suffix, the SAME replan path used after a real failure —
                    # but here BEFORE any unsafe action touches the sim.
                    if (planner is not None
                            and replan_count < max_replan
                            and _consecutive_prevented <= max_replan):
                        replan_count += 1
                        gate_repairs  += 1
                        try:
                            new_steps, _ = planner.replan(
                                task            = sample["task_desc"],
                                completed       = completed,
                                failed_step     = step,
                                failure_reason  = f"[pre-exec verifier] {_reason}",
                                obs             = obs,
                                visible_objects = visible_objects,
                            )
                            current_plan = list(new_steps)
                        except Exception as _re:
                            if verbose:
                                print(f"    ✖  pre-emptive replan error: {_re}")
                            # Could not repair → drop the bad step (count as
                            # prevented-unsafe) and continue with the rest.
                        # Re-loop: gate the FIRST step of the repaired suffix.
                        continue
                    else:
                        # Repair budget exhausted / no planner / loop guard hit:
                        # drop the predicted-bad step, never execute it.
                        continue
                else:
                    # Gate passed for this step — progress made.
                    _consecutive_prevented = 0

            # ── INJECTED mid-execution failure (failure-recovery benchmark) ──
            # Force the step reached after `inject_fail_at` successful executions
            # to fail, without dispatching it to the robot. Mirrors a runtime
            # rejection the offline verifier could not have foreseen.
            if (not _injected) and inject_fail_at >= 0 and len(completed) >= inject_fail_at:
                _injected      = True
                _capture_replan = True
                rec_attempted   = 1
                result = {"success": False, "done": False, "reward": 0.0,
                          "obs": obs, "visible_objects": visible_objects,
                          "msg": fail_reason or "Injected mid-execution failure"}
            else:
                result = client.step(action, obj, target)
            steps_attempted += 1

            obs             = result.get("obs", obs)
            visible_objects = result.get("visible_objects", visible_objects)
            reward          = result.get("reward", 0.0)
            success         = result.get("success", False)
            total_reward   += reward

            if result.get("done"):
                task_done = True
                steps_succeeded += 1
                break

            if success:
                completed.append(step)
                steps_succeeded += 1
            else:
                # Step failed — try replan
                if replan_count < max_replan and planner is not None:
                    replan_count += 1
                    msg = result.get("msg", "step failed")
                    if verbose:
                        print(f"    ⚠  Failed: {msg} — replanning ({replan_count}/{max_replan})")
                    try:
                        new_steps, _rpm = planner.replan(
                            task            = sample["task_desc"],
                            completed       = completed,
                            failed_step     = step,
                            failure_reason  = msg,
                            obs             = obs,
                            visible_objects = visible_objects,
                        )
                        current_plan = new_steps
                        # Capture the cost of recovering from the INJECTED failure.
                        if _capture_replan:
                            _capture_replan = False
                            rec_latency   = float(getattr(_rpm, "latency_s", 0.0) or 0.0)
                            rec_steps     = len(new_steps)
                            rec_llm_calls = int(getattr(_rpm, "llm_calls", 0) or 0)
                            rec_in_tok    = int(getattr(_rpm, "input_tokens", 0) or 0)
                            rec_out_tok   = int(getattr(_rpm, "output_tokens", 0) or 0)
                    except Exception as e:
                        if verbose:
                            print(f"    ✖  Replan error: {e}")
                        break
                else:
                    break  # max replans reached or no planner

        # If we exhausted all steps without task_done, check if reward > 0
        if not task_done and total_reward > 0:
            task_done = True

        # ── GROUNDED goal check (replaces the reward>0 proxy for task_success) ──
        # Query the final object metadata and assert the task's goal conditions.
        # Deterministic where a GoalCondition is defined (the 38 curated tasks);
        # otherwise falls back inside check_goal. This makes exec_task_success
        # mean "goal achieved", not merely "some step executed".
        try:
            from goal_checker import check_goal
            _om = client.get_objects()
            object_map = {o["objectType"]: o for o in _om.get("objects", [])}
            _verdict = check_goal(
                task_id        = sample.get("task_id", ""),
                task_desc      = sample.get("task_desc", ""),
                executed_steps = completed,
                final_obs      = obs,
                object_map     = object_map,
                use_llm_judge  = False,
            )
            goal_success = 1.0 if _verdict.success else 0.0
            goal_method  = _verdict.method
        except Exception as _ge:
            goal_method = f"err:{type(_ge).__name__}"

        # ── Optional human-evaluation bundle: final frame + object states +
        #    plan + auto verdict, for publishable end-to-end human judging.
        #    Active only when SAGE_EVAL_BUNDLE is set; no effect otherwise.
        if os.environ.get("SAGE_EVAL_BUNDLE"):
            try:
                _dump_eval_bundle(os.environ["SAGE_EVAL_BUNDLE"], planner, sample,
                                  completed, object_map, goal_success, goal_method,
                                  locals().get("_verdict"), client)
            except Exception:
                pass

    except Exception as e:
        sim_error = str(e)[:150]
        if verbose:
            traceback.print_exc()

    elapsed = time.perf_counter() - t0
    step_success_rate = round(steps_succeeded / steps_attempted, 4) if steps_attempted else 0.0

    return {
        # Grounded goal success when the checker ran; else fall back to the
        # legacy reward>0 proxy (only if get_objects/check_goal errored).
        "task_success": goal_success if goal_success is not None else (1.0 if task_done else 0.0),
        "goal_method":  goal_method,
        "step_success": step_success_rate,
        "total_reward": round(total_reward, 4),
        "steps_done":   steps_attempted,
        "replans":      replan_count,
        "latency_s":    round(elapsed, 3),
        "error":        sim_error,
        # Runtime safety monitor metrics (gate_on reflects whether the gate was
        # actually active = requested AND verifier import succeeded).
        "gate_on":        1 if (verify_gate and _gate_ready) else 0,
        "gate_prevented": gate_prevented,
        "gate_repairs":   gate_repairs,
        # ── Failure-recovery (injected-failure) metrics ──
        "rec_attempted":     rec_attempted,
        "rec_ok":            (1.0 if rec_attempted and
                              ((goal_success == 1.0) or task_done) else 0.0),
        "rec_latency_s":     round(rec_latency, 3),
        "rec_steps":         rec_steps,
        "rec_llm_calls":     rec_llm_calls,
        "rec_tokens":        rec_in_tok + rec_out_tok,
    }


# ═════════════════════════════════════════════════════════════════════
# Evaluate one sample: plan + (optionally) execute
# ═════════════════════════════════════════════════════════════════════

def evaluate_sample_sim(
    client:     ThorClient | None,
    planner,
    sample:     dict,
    max_replan: int  = 3,
    run_sim:    bool = True,
    verbose:    bool = False,
    verify_gate: bool = False,
    inject_fail: bool = False,
) -> SimSampleResult:

    res = SimSampleResult(
        method     = planner.name,
        model      = getattr(planner, "model", ""),
        backend    = getattr(planner, "provider", ""),
        task_id    = sample["task_id"],
        task_desc  = sample["task_desc"][:60],
        room       = sample["room"],
        difficulty = sample["difficulty"],
        ref_steps  = len(sample["reference_steps"]),
        has_fail_injection = 1 if sample.get("fail_injection") else 0,
    )

    plan = []
    try:
        # ── Phase 1: Generate plan ──────────────────────────────────────
        steps, metrics = planner.generate_plan(
            task            = sample["task_desc"],
            obs             = sample["obs"],
            visible_objects = sample["visible_objects"],
        )

        if metrics.notes and not metrics.parse_ok:
            raise RuntimeError(f"LLM error: {metrics.notes}")

        plan = steps

        res.parse_ok        = 1.0 if (steps and metrics.parse_ok) else 0.0
        res.num_steps       = len(steps)
        res.step_ratio      = round(res.num_steps / res.ref_steps, 4) if res.ref_steps else 0.0
        res.latency_s       = round(metrics.latency_s, 4)
        res.llm_calls       = metrics.llm_calls
        res.input_tokens    = metrics.input_tokens
        res.output_tokens   = metrics.output_tokens
        res.total_tokens    = metrics.total_tokens
        res.tokens_per_step = round(metrics.tokens_per_step, 2)

        res.executability = score_executability(steps)
        res.precondition  = score_precondition(steps)
        try:  # strict, verifier-grounded precondition (mirrors the plan-quality grid)
            from embodiplan.verifier import normalize_plan as _nplan, simulate as _vsim
            _rep = _vsim(_nplan(steps), visible_objects=sample["visible_objects"],
                         stop_on_error=False)
            res.precondition_strict = round(max(0.0, 1.0 - len(_rep.violations) / len(steps)), 4) if steps else 0.0
        except Exception:
            res.precondition_strict = 0.0
        res.redundancy    = score_redundancy(steps)
        res.completeness  = score_completeness(steps, sample["expected_objects"])
        res.hallucination = score_hallucination(steps, sample["visible_objects"])
        res.quality_score = compute_quality_score({
            "executability": res.executability,
            "precondition":  res.precondition,
            "completeness":  res.completeness,
            "redundancy":    res.redundancy,
            "hallucination": res.hallucination,
        })
        res.efficiency_score = compute_efficiency_score(res.latency_s, res.llm_calls)
        res.overall_score    = compute_overall_score(
            res.quality_score, res.efficiency_score, 0.0, False
        )

    except Exception as e:
        res.error = str(e)[:200]
        print(f"\n  ⚠  [{res.task_id}] {res.method} plan error: {str(e)[:80]}")
        return res  # skip execution if planning failed

    if verbose:
        print(f"  📋 [{res.task_id}] {len(plan)} steps generated "
              f"(Q={res.quality_score:.2f}  {res.latency_s:.1f}s)")

    # ── Phase 2: Execute in simulator ──────────────────────────────────
    if run_sim and client is not None and plan:
        # Failure-recovery: resolve the injection point. Prefer the dataset's
        # fail_injection.at_step; else (when --inject-fail forces it) inject at
        # the midpoint of the executed plan so a recovery is always exercised.
        _inj_at = -1
        if inject_fail:
            _fi = sample.get("fail_injection") or {}
            _inj_at = int(_fi.get("at_step", max(1, len(plan) // 2)))
            _fail_reason = _fi.get("failure_reason", "Injected mid-execution failure")
        else:
            _fail_reason = ""
        exec_result = execute_plan_in_sim(
            client    = client,
            planner   = planner,
            sample    = sample,
            plan      = plan,
            max_replan= max_replan,
            verbose   = verbose,
            verify_gate = verify_gate,
            inject_fail_at = _inj_at,
            fail_reason    = _fail_reason,
        )
        res.has_fail_injection = 1 if inject_fail else res.has_fail_injection
        res.replan_ok        = exec_result.get("rec_ok", 0.0)
        res.replan_latency_s = exec_result.get("rec_latency_s", 0.0)
        res.replan_steps     = exec_result.get("rec_steps", 0)
        res.replan_llm_calls = exec_result.get("rec_llm_calls", 0)
        res.exec_task_success = exec_result["task_success"]
        res.exec_goal_method  = exec_result.get("goal_method", "")
        res.exec_step_success = exec_result["step_success"]
        res.exec_total_reward = exec_result["total_reward"]
        res.exec_steps_done   = exec_result["steps_done"]
        res.exec_replans      = exec_result["replans"]
        res.exec_latency_s    = exec_result["latency_s"]
        res.sim_error         = exec_result["error"]
        res.gate_on           = exec_result.get("gate_on", 0)
        res.gate_prevented    = exec_result.get("gate_prevented", 0)
        res.gate_repairs      = exec_result.get("gate_repairs", 0)

        if res.sim_error:
            print(f"\n  ⚠  [{res.task_id}] sim error: {res.sim_error[:80]}")

        if verbose:
            status = "✅" if res.exec_task_success else "❌"
            print(f"  {status} [{res.task_id}] exec: "
                  f"success={res.exec_task_success}  "
                  f"steps={res.exec_steps_done}/{res.num_steps}  "
                  f"reward={res.exec_total_reward:.1f}  "
                  f"{res.exec_latency_s:.1f}s")

    # Combined score: blend plan quality with execution success
    if run_sim:
        res.combined_score = round(
            0.5 * res.quality_score + 0.5 * res.exec_task_success, 4
        )
    else:
        res.combined_score = res.quality_score

    return res


# ═════════════════════════════════════════════════════════════════════
# Summary printer
# ═════════════════════════════════════════════════════════════════════

def _print_summary(results: list[SimSampleResult], run_sim: bool):
    from collections import defaultdict
    by_method: dict[str, list[SimSampleResult]] = defaultdict(list)
    for r in results:
        by_method[r.method].append(r)

    def avg(rows, attr):
        vals = [getattr(r, attr) for r in rows]
        return round(sum(vals) / len(vals), 3) if vals else 0.0

    if run_sim:
        header = (
            f"\n{'─'*130}\n"
            f"  {'Method':<16} {'Model':<14} "
            f" {'Quality':>7} {'Effic':>6} {'Overall':>8}"
            f" {'TaskSucc':>8} {'StepSucc':>8} {'Reward':>7} {'ExecLat':>8}"
            f" {'Combined':>9} {'Errors':>7}\n"
            f"{'─'*130}"
        )
        print(header)
        for method, rows in by_method.items():
            n   = len(rows)
            err = sum(1 for r in rows if r.error or r.sim_error)
            print(
                f"  {method:<16} {rows[0].model:<14}"
                f" {avg(rows,'quality_score'):>7.3f}"
                f" {avg(rows,'efficiency_score'):>6.3f}"
                f" {avg(rows,'overall_score'):>8.3f}"
                f" {avg(rows,'exec_task_success'):>8.3f}"
                f" {avg(rows,'exec_step_success'):>8.3f}"
                f" {avg(rows,'exec_total_reward'):>7.2f}"
                f" {avg(rows,'exec_latency_s'):>7.1f}s"
                f" {avg(rows,'combined_score'):>9.3f}"
                f" {err:>5}/{n}"
            )
    else:
        header = (
            f"\n{'─'*110}\n"
            f"  {'Method':<16} {'Model':<14}"
            f" {'Quality':>7} {'Effic':>6} {'Overall':>8}"
            f" {'Latency':>8} {'Calls':>5} {'Tok/step':>8}"
            f" {'Parse%':>7} {'Errors':>7}\n"
            f"{'─'*110}"
        )
        print(header)
        for method, rows in by_method.items():
            n   = len(rows)
            err = sum(1 for r in rows if r.error)
            print(
                f"  {method:<16} {rows[0].model:<14}"
                f" {avg(rows,'quality_score'):>7.3f}"
                f" {avg(rows,'efficiency_score'):>6.3f}"
                f" {avg(rows,'overall_score'):>8.3f}"
                f" {avg(rows,'latency_s'):>8.2f}s"
                f" {avg(rows,'llm_calls'):>5.1f}"
                f" {avg(rows,'tokens_per_step'):>8.0f}"
                f" {avg(rows,'parse_ok')*100:>7.1f}%"
                f" {err:>5}/{n}"
            )

    print(f"{'─'*110 if not run_sim else '─'*130}")


# ═════════════════════════════════════════════════════════════════════
# Main evaluation runner
# ═════════════════════════════════════════════════════════════════════

def run_sim_evaluation(
    dataset_path:  str,
    methods:       list[str],
    host:          str,
    model:         str,
    provider:      str,
    api_key:       str,
    sim_host:      str,
    sim_port:      int,
    out_path:      str,
    run_sim:       bool,
    dry_run_llm:   bool,
    max_replan:    int,
    max_samples:   int | None,
    verbose:       bool,
    method_kwargs: dict[str, dict],
    sim_type:      str = "thor",
    verify_gate:   bool = False,
    inject_fail:   bool = False,
) -> str:

    # ── Load dataset ──────────────────────────────────────────────────
    with open(dataset_path, encoding="utf-8") as f:
        data = json.load(f)
    samples = data["samples"] if "samples" in data else data
    if max_samples:
        samples = samples[:max_samples]
    print(f"\n📂  Loaded {len(samples)} samples from {dataset_path}")

    # ── LLM connection check ──────────────────────────────────────────
    if not dry_run_llm:
        ok, msg = check_connection(provider, host, model, api_key)
        if ok:
            print(f"  ✅  LLM OK — {msg}")
        else:
            print(f"\n{'═'*60}\n  ❌  LLM CONNECTION FAILED\n{'─'*60}")
            for line in msg.splitlines():
                print(f"  {line}")
            print(f"{'═'*60}\n  Tip: use --dry-run-llm to skip LLM calls\n")
            sys.exit(1)

    # ── Simulator connection check ─────────────────────────────────────
    client = None
    if run_sim:
        ok, msg = check_simulator(sim_host, sim_port)
        if ok:
            print(f"  ✅  Simulator OK — {msg}")
            # ProcTHOR runs share the same ZMQ server/protocol; only the
            # per-reset simulator_type differs (set by execute_plan_in_sim from
            # the sample). The client's default simulator_type only affects
            # resets that don't pass simulator_type explicitly, so for_procthor
            # is the safe choice when the dataset is ProcTHOR. iTHOR path
            # (default) is unchanged.
            if (sim_type or "thor").lower() == "procthor":
                from thor_app.sim_client import SimClient
                client = SimClient.for_procthor(host=sim_host, port=sim_port)
            else:
                client = ThorClient(host=sim_host, port=sim_port)
        else:
            print(f"\n{'═'*60}\n  ❌  SIMULATOR NOT RUNNING\n{'─'*60}")
            for line in msg.splitlines():
                print(f"  {line}")
            print(f"{'═'*60}")
            print("  Continuing with --no-sim (offline metrics only)...")
            run_sim = False

    # ── Build planners ────────────────────────────────────────────────
    planners = []
    if dry_run_llm:
        stub      = _DryRunPlanner()
        stub.name = "DryRun"
        planners  = [stub]
        print("🔧  Dry-run LLM mode")
    else:
        # SAGE ablation/variant flags — mirrors make_sage() in
        # task_planning_manuscripts/scripts/run_benchmark.py so that sim-mode results are
        # directly comparable to the plan-quality grid. SAGE-Fixed is the
        # safe_refine variant that is the paper's headline method.
        _SAGE_FLAGS = {
            "SAGE":            dict(enable_verifier=True,  enable_local_repair=True,  enable_memory=True,  safe_refine=False),
            "SAGE-Fixed":      dict(enable_verifier=True,  enable_local_repair=True,  enable_memory=True,  safe_refine=True),
            "SAGE-NoVerifier": dict(enable_verifier=False, enable_local_repair=True,  enable_memory=True,  safe_refine=False),
            "SAGE-NoRepair":   dict(enable_verifier=True,  enable_local_repair=False, enable_memory=True,  safe_refine=False),
            "SAGE-NoMemory":   dict(enable_verifier=True,  enable_local_repair=True,  enable_memory=False, safe_refine=False),
        }
        # GT file used to seed SAGE memory (same 38-task curated set).
        _gt = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "eval_dataset_gt.json")
        for name in methods:
            try:
                if name.startswith("SAGE"):
                    from embodiplan.sage import SAGEPlanner
                    variant = name
                    flags = _SAGE_FLAGS.get(variant)
                    if flags is None:
                        raise ValueError(f"unknown SAGE variant '{name}'")
                    p = SAGEPlanner(
                        host=host, model=model, provider=provider, api_key=api_key,
                        gt_path=_gt if flags["enable_memory"] else "",
                        live_path="",
                        **flags,
                    )
                else:
                    kwargs = {**method_kwargs.get(name, {}), "provider": provider, "api_key": api_key}
                    p = embodiplan.get(name, host=host, model=model, **kwargs)
                planners.append(p)
                print(f"  ✅  {name:15}  ({provider}/{model})")
            except Exception as e:
                print(f"  ❌  {name}: {e}")

    if not planners:
        print("No planners initialised — aborting.")
        return ""

    mode_label = "plan+execute" if run_sim else "plan only"
    _gate_label = "  |  verify-gate=ON (verify-before-execute)" if verify_gate else ""
    print(f"\n  Mode: {mode_label}  |  max_replan={max_replan}{_gate_label}")

    # ── Run evaluation ────────────────────────────────────────────────
    all_results: list[SimSampleResult] = []
    total = len(planners) * len(samples)
    done  = 0

    for planner in planners:
        print(f"\n▶  [{planner.name}]  {len(samples)} samples  ({mode_label})")
        consecutive_errors = 0

        for sample in samples:
            result = evaluate_sample_sim(
                client     = client,
                planner    = planner,
                sample     = sample,
                max_replan = max_replan,
                run_sim    = run_sim,
                verbose    = verbose,
                verify_gate= verify_gate,
                inject_fail= inject_fail,
            )
            all_results.append(result)
            done += 1

            if result.error or result.sim_error:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    err = result.error or result.sim_error
                    print(f"\n  ❌  3 consecutive errors — aborting [{planner.name}]")
                    print(f"     Last: {err[:80]}")
                    break
            else:
                consecutive_errors = 0

            if not verbose:
                pct = done / total * 100
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                print(f"\r  [{bar}] {pct:5.1f}%  {done}/{total}", end="", flush=True)

        if not verbose:
            print()

    # ── Write CSV ─────────────────────────────────────────────────────
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SIM_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in all_results:
            writer.writerow(r.to_row())

    _print_summary(all_results, run_sim)
    print(f"\n✅  Results saved → {out_path}")
    print(f"    Rows: {len(all_results)}  ·  Columns: {len(SIM_CSV_COLUMNS)}\n")
    return out_path


# ═════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════

def main():
    all_methods = list(REGISTRY.keys())

    parser = argparse.ArgumentParser(
        description="Evaluate embodiplan methods by executing plans in AI2-THOR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dataset",   default="eval_dataset.json")
    parser.add_argument("--methods",   nargs="+", default=all_methods)
    parser.add_argument("--host",      default=DEFAULT_HOST)
    parser.add_argument("--model",     default=DEFAULT_MODEL)
    parser.add_argument("--provider",  default=DEFAULT_BACKEND,
                        choices=["ollama","openai","gemini"])
    parser.add_argument("--api-key",   default="")
    parser.add_argument("--sim-host",  default="localhost",
                        help="ThorServer host (default: localhost)")
    parser.add_argument("--sim-port",  type=int, default=5555,
                        help="ThorServer ZMQ port (default: 5555)")
    parser.add_argument("--sim-type",  default="thor", choices=["thor", "procthor"],
                        help="Simulator family. 'procthor' resets houses by "
                             "(split, house_index) read from each sample; "
                             "default 'thor' is the unchanged iTHOR path.")
    parser.add_argument("--out",       default="sim_results.csv")
    parser.add_argument("--no-sim",    action="store_true",
                        help="Skip simulator execution (offline metrics only)")
    parser.add_argument("--dry-run-llm", action="store_true",
                        help="Stub LLM calls (still executes in simulator if available)")
    parser.add_argument("--max-samples", type=int, default=None,
                        help="Limit number of samples (useful for quick tests)")
    parser.add_argument("--max-replan",  type=int, default=3)
    parser.add_argument("--verify-gate", action="store_true",
                        help="Runtime safety monitor: verify each step with the "
                             "symbolic verifier BEFORE sending it to the robot. "
                             "Predicted precondition violations are repaired "
                             "pre-emptively (planner.replan) and never executed. "
                             "Default OFF reproduces the execute-then-replan path "
                             "exactly. Adds gate_on/gate_prevented/gate_repairs.")
    parser.add_argument("--inject-fail", action="store_true",
                        help="Failure-recovery benchmark: inject ONE mid-execution "
                             "failure per task (at fail_injection.at_step, else the "
                             "plan midpoint) and measure the COST of recovery "
                             "(replan_ok/replan_latency_s/replan_steps/replan_llm_calls). "
                             "Isolates sub-goal-local repair vs whole-plan replanning.")
    parser.add_argument("--verbose",   action="store_true")
    parser.add_argument("--react-max-steps",   type=int, default=15)
    parser.add_argument("--refine-iterations", type=int, default=2)
    args = parser.parse_args()

    methods = all_methods if "all" in args.methods else args.methods
    method_kwargs = {
        "ReAct":       {"max_steps":      args.react_max_steps},
        "Self-Refine": {"max_iterations": args.refine_iterations},
    }

    # Auto-generate dataset if missing
    if not os.path.exists(args.dataset):
        print(f"⚠  Dataset not found: {args.dataset} — generating...")
        samples  = build_dataset()
        warnings = validate_dataset(samples)
        for w in warnings:
            print(f"   ⚠  {w}")
        with open(args.dataset, "w", encoding="utf-8") as f:
            json.dump({"version": "1.0", "samples": samples}, f, indent=2, ensure_ascii=False)
        print(f"   ✅  Generated {len(samples)} samples → {args.dataset}")

    print(f"\n{'═'*65}")
    print(f"  embodiplan Simulation Evaluation")
    print(f"{'═'*65}")
    print(f"  Methods    : {methods}")
    print(f"  Provider   : {args.provider}  |  Model: {args.model}")
    print(f"  LLM host   : {args.host}")
    print(f"  Sim host   : {args.sim_host}:{args.sim_port}")
    print(f"  Run sim    : {'no (offline only)' if args.no_sim else 'yes'}")
    print(f"  Verify-gate: {'ON (verify-before-execute)' if args.verify_gate else 'off'}")
    print(f"  Output     : {args.out}")
    if args.max_samples:
        print(f"  Max samples: {args.max_samples}")
    print(f"{'═'*65}")

    t_start = time.perf_counter()
    out = run_sim_evaluation(
        dataset_path  = args.dataset,
        methods       = methods,
        host          = args.host,
        model         = args.model,
        provider      = args.provider,
        api_key       = args.api_key,
        sim_host      = args.sim_host,
        sim_port      = args.sim_port,
        out_path      = args.out,
        run_sim       = not args.no_sim,
        dry_run_llm   = args.dry_run_llm,
        max_replan    = args.max_replan,
        max_samples   = args.max_samples,
        verbose       = args.verbose,
        method_kwargs = method_kwargs,
        sim_type      = args.sim_type,
        verify_gate   = args.verify_gate,
        inject_fail   = args.inject_fail,
    )
    if out:
        print(f"  Total time: {time.perf_counter() - t_start:.1f}s\n")


if __name__ == "__main__":
    main()