# embodiplan/memory_tools.py
# ─────────────────────────────────────────────────────────────────────
# Tools for SAGE's retrieval memory (the live JSONL pool and seed pools).
#
#   python -m embodiplan.memory_tools clean IN.jsonl --out OUT.jsonl \
#          [--exclude-tasks FILE] [--capabilities JSON|FILE] [--require-verified]
#          [--max-per-task N] [--report]
#
# `clean` writes the episodes of IN worth retrieving to OUT and prints what it
# dropped and why. IN is never written. An episode is dropped when
#   * it does not parse (no task / no plan),
#   * its task is in the exclude file (test-set leakage; exact or near match),
#   * it is unverified: recorded with `verified: false`, recorded without a goal
#     verification flag (only with --require-verified), or its plan fails the
#     action model from its start belief (`init_state` / `belief`, else empty),
#   * it duplicates an episode kept before it (same task, same plan), or is a
#     near-duplicate (near-same task, same plan once repeated steps collapse),
#   * its task already has --max-per-task kept episodes (the shortest plans win).
#
# The same task matcher drives leave-one-out retrieval (MemoryRetriever's
# `leave_one_out`): an example whose task is the query or a near-duplicate of it
# is never returned, so an evaluation never retrieves its own answer.
# ─────────────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from embodiplan.action_model import Capabilities, SymbolicState, normalize_plan, simulate

# Two task texts with keys at least this similar (difflib ratio) are the same task.
NEAR_THRESHOLD = 0.92

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
# words that do not change what a task asks for
_FILLER = {"the", "a", "an", "please", "kindly", "can", "could", "you", "for", "me", "now"}


# ─────────────────────────────────────────────────────────────────────
# task matching
# ─────────────────────────────────────────────────────────────────────
def task_key(text: str) -> str:
    """Normalized task text: lower case, words only, filler words dropped."""
    words = _WORD_RE.findall((text or "").lower())
    kept = [w for w in words if w not in _FILLER]
    return " ".join(kept or words)


def same_task(a: str, b: str, threshold: float = NEAR_THRESHOLD) -> bool:
    """True when `a` and `b` ask for the same thing (equal keys, or keys at least
    `threshold` similar: a typo, a trailing word). A different object or place is
    a different task ("... to the table" vs "... to the dining table")."""
    ka, kb = task_key(a), task_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    return threshold < 1.0 and difflib.SequenceMatcher(None, ka, kb).ratio() >= threshold


# ─────────────────────────────────────────────────────────────────────
# episodes
# ─────────────────────────────────────────────────────────────────────
def plan_steps(rec: Mapping) -> list[dict]:
    """The steps of an episode record (`plan_text` JSON, or `steps` / `plan`)."""
    raw = rec.get("steps") or rec.get("plan")
    if raw is None:
        txt = rec.get("plan_text") or ""
        try:
            raw = json.loads(txt) if isinstance(txt, str) else txt
        except json.JSONDecodeError:
            return []
    if isinstance(raw, dict):
        raw = raw.get("steps") or []
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict) and s.get("action")]


def plan_signature(steps: Iterable[dict]) -> tuple:
    """What a plan does, for near-duplicate detection: normalized (action, object)
    pairs with consecutive repeats collapsed (Find mug, Find mug -> Find mug)."""
    out: list[tuple] = []
    for s in normalize_plan(list(steps)):
        p = (s.get("action", ""), str(s.get("object") or s.get("target") or "").strip().lower())
        if not out or out[-1] != p:
            out.append(p)
    return tuple(out)


def start_state(rec: Mapping) -> SymbolicState:
    """The episode's start belief: `init_state` ({arrived, found, holding, opened, on}),
    else a `belief` text (`Robot state: ...` / SymbolicState.as_text()), else empty."""
    st = rec.get("init_state")
    if isinstance(st, Mapping):
        return SymbolicState(arrived=st.get("arrived") or None, found=st.get("found") or None,
                             holding=st.get("holding") or None, opened=set(st.get("opened") or ()),
                             on=set(st.get("on") or ()))
    text = rec.get("belief")
    if isinstance(text, str) and text.strip():
        from embodiplan.sage import belief_from_obs          # lazy: sage imports this package's memory
        line = text if "Robot state:" in text else "Robot state: " + text
        got = belief_from_obs(line)
        if got is not None:
            return got
    return SymbolicState()


def state_dict(state) -> dict:
    """A JSON-friendly `init_state` of a SymbolicState-like world (None -> {})."""
    if state is None:
        return {}
    get = state.get if isinstance(state, Mapping) else (lambda k, d=None: getattr(state, k, d))
    return {"arrived": get("arrived"), "found": get("found"), "holding": get("holding"),
            "opened": sorted(get("opened") or ()), "on": sorted(get("on") or ())}


def verify_episode(rec: Mapping, capabilities=None, *, require_verified: bool = False) -> tuple[bool, str]:
    """(ok, why not): the episode is goal-verified and its plan passes the action
    model from its start belief."""
    if rec.get("verified") is False:
        return False, "unverified: recorded without goal verification (verified=false)"
    if require_verified and rec.get("verified") is not True:
        return False, "unverified: no goal verification recorded"
    steps = plan_steps(rec)
    if not steps:
        return False, "unparseable: no plan"
    rep = simulate(steps, start_state(rec), capabilities=Capabilities.from_any(capabilities))
    if not rep.ok:
        v = rep.violations[0]
        return False, f"unverified: plan fails the action model at step {v.index} ({v.code}: {v.reason})"
    return True, ""


def load_tasks(path: str) -> list[str]:
    """Task texts from an exclude file: plain text (one per line, '#' comments), a
    JSON list of strings / of {task|task_desc}, a JSON {"samples": [...]} dataset,
    a JSON mapping id -> task, or JSONL with a `task` per line."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if data is None:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append(line)
        data = rows
    return [t for t in (_task_of(x) for x in _items(data)) if t]


def _items(data) -> list:
    if isinstance(data, dict):
        if isinstance(data.get("samples"), list):
            return data["samples"]
        return list(data.values())
    return list(data) if isinstance(data, list) else []


def _task_of(x) -> str:
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, dict):
        return str(x.get("task") or x.get("task_desc") or x.get("instruction") or "").strip()
    if isinstance(x, (list, tuple)):          # (tier, task, ...) rows, e.g. a CASES table
        return next((str(v).strip() for v in x if isinstance(v, str) and len(v.split()) > 2), "")
    return ""


# ─────────────────────────────────────────────────────────────────────
# clean
# ─────────────────────────────────────────────────────────────────────
@dataclass
class Dropped:
    line: int            # 1-based line number in IN
    task: str
    reason: str          # "<kind>: detail"

    @property
    def kind(self) -> str:
        return self.reason.split(":", 1)[0]


@dataclass
class CleanReport:
    n_in: int = 0
    kept: list = field(default_factory=list)       # records written to OUT
    dropped: list = field(default_factory=list)    # list[Dropped]

    def counts(self) -> dict:
        out: dict = {}
        for d in self.dropped:
            out[d.kind] = out.get(d.kind, 0) + 1
        return out

    def summary(self, details: bool = False) -> str:
        lines = [f"{self.n_in} episodes in, {len(self.kept)} kept, {len(self.dropped)} dropped"]
        lines += [f"  {k:<15} {n}" for k, n in sorted(self.counts().items())]
        if details:
            lines += [f"  line {d.line}: {d.task[:70]!r}: {d.reason}" for d in self.dropped]
        return "\n".join(lines)


def clean(records: Iterable, *, exclude_tasks: Iterable[str] = (), capabilities=None,
          near_threshold: float = NEAR_THRESHOLD, require_verified: bool = False,
          max_per_task: int = 0) -> CleanReport:
    """Filter episode records (dicts; a non-dict is unparseable). Order is kept."""
    rep, excl = CleanReport(), [t for t in exclude_tasks if t]
    kept: list[tuple[int, dict, str, tuple]] = []           # (line, rec, task, signature)
    for line, rec in enumerate(records, 1):
        rep.n_in += 1
        task = str(rec.get("task") or "").strip() if isinstance(rec, dict) else ""
        why = _drop_reason(rec, task, kept, excl, capabilities, near_threshold, require_verified)
        if why:
            rep.dropped.append(Dropped(line, task, why))
        else:
            kept.append((line, rec, task, plan_signature(plan_steps(rec))))
    if max_per_task > 0:
        kept = _cap_per_task(kept, max_per_task, near_threshold, rep)
    rep.kept = [rec for _l, rec, _t, _s in kept]
    return rep


def _drop_reason(rec, task, kept, excl, caps, near, require_verified) -> str:
    if not isinstance(rec, dict) or not task:
        return "unparseable: no task"
    if not plan_steps(rec):
        return "unparseable: no plan"
    hit = next((t for t in excl if same_task(task, t, near)), None)
    if hit is not None:
        return f"excluded: test task {hit!r}"
    ok, why = verify_episode(rec, caps, require_verified=require_verified)
    if not ok:
        return why
    sig, steps = plan_signature(plan_steps(rec)), normalize_plan(plan_steps(rec))
    for line, prev, ptask, psig in kept:
        if task_key(ptask) == task_key(task) and normalize_plan(plan_steps(prev)) == steps:
            return f"duplicate: of line {line}"
        if psig == sig and same_task(ptask, task, near):
            return f"near_duplicate: of line {line}"
    return ""


def _cap_per_task(kept, n, near, rep) -> list:
    groups: list[list] = []
    for item in kept:
        g = next((g for g in groups if same_task(g[0][2], item[2], near)), None)
        (g.append(item) if g is not None else groups.append([item]))
    keep = set()
    for g in groups:
        for item in sorted(g, key=lambda it: (len(it[3]), it[0]))[:n]:
            keep.add(item[0])
    for line, _rec, task, _sig in kept:
        if line not in keep:
            rep.dropped.append(Dropped(line, task, f"over_cap: more than {n} episodes of this task"))
    rep.dropped.sort(key=lambda d: d.line)
    return [it for it in kept if it[0] in keep]


def read_jsonl(path: str) -> list:
    """Records of a JSONL file; a line that is not JSON becomes None (unparseable)."""
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append(None)
    return out


def clean_file(in_path: str, out_path: str, **kw) -> CleanReport:
    """`clean` IN into OUT (a new file; IN is only read). Raises ValueError when
    OUT is IN."""
    if os.path.exists(out_path) and os.path.samefile(in_path, out_path):
        raise ValueError("--out must not be the input file (the input is never edited)")
    rep = clean(read_jsonl(in_path), **kw)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in rep.kept:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rep


def load_capabilities(arg: str | None):
    """--capabilities: a JSON object, or a JSON / YAML file (a manifest's
    `capabilities` section is used when present)."""
    if not arg:
        return None
    if os.path.isfile(arg):
        with open(arg, "r", encoding="utf-8") as f:
            text = f.read()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            import yaml                               # only for a YAML file (a manifest)
            data = yaml.safe_load(text)
    else:
        data = json.loads(arg)
    if isinstance(data, dict) and isinstance(data.get("capabilities"), dict):
        data = data["capabilities"]
    return data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m embodiplan.memory_tools",
                                 description="Tools for SAGE's retrieval memory (JSONL episodes).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("clean", help="write the verified, de-duplicated, leak-free episodes of IN to OUT")
    c.add_argument("input", metavar="IN.jsonl")
    c.add_argument("--out", required=True, metavar="OUT.jsonl")
    c.add_argument("--exclude-tasks", metavar="FILE", help="tasks to drop (a test set): text / JSON / JSONL")
    c.add_argument("--capabilities", metavar="JSON|FILE", help="robot body for the action model (or a manifest)")
    c.add_argument("--require-verified", action="store_true",
                   help="drop episodes recorded without `verified: true` (legacy records have none)")
    c.add_argument("--max-per-task", type=int, default=0, metavar="N", help="keep at most N episodes per task")
    c.add_argument("--near", type=float, default=NEAR_THRESHOLD, help="task similarity of a near-duplicate")
    c.add_argument("--report", action="store_true", help="list every dropped episode")
    a = ap.parse_args(argv)
    excl = load_tasks(a.exclude_tasks) if a.exclude_tasks else []
    try:
        rep = clean_file(a.input, a.out, exclude_tasks=excl, capabilities=load_capabilities(a.capabilities),
                         near_threshold=a.near, require_verified=a.require_verified,
                         max_per_task=a.max_per_task)
    except (OSError, ValueError) as e:
        print(f"[memory_tools] {e}", file=sys.stderr)
        return 2
    print(f"{a.input} -> {a.out}")
    print(rep.summary(details=a.report))
    return 0


__all__ = [
    "NEAR_THRESHOLD", "task_key", "same_task", "plan_steps", "plan_signature", "start_state",
    "state_dict", "verify_episode", "load_tasks", "Dropped", "CleanReport", "clean", "clean_file",
    "read_jsonl", "load_capabilities", "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
