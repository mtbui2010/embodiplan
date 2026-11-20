---
sources: [README.md, docs/usage.md, CHANGELOG.md, embodiplan/base.py, embodiplan/__init__.py, embodiplan/action_model.py, embodiplan/robot_prompt.py, embodiplan/memory_retriever.py, embodiplan/data/eval_dataset_gt.json, embodiplan/direct.py, embodiplan/cot.py, embodiplan/few_shot.py, embodiplan/few_shot_examples.py, embodiplan/self_refine.py, embodiplan/react.py, embodiplan/hierarchical.py, embodiplan/my_planner.py, embodiplan/llm_router.py, embodiplan/saycan.py, embodiplan/llmp.py, embodiplan/sage.py]
verified_at: 76da520
---

# Known pitfalls

This page lists the problems of embodiplan that were read in the code and, where stated, reproduced offline. For each one you get the symptom, the cause with a link to the line, a way to avoid it, and the page that has the detail.

!!! note "Read this first"
    These entries describe the code at commit `76da520` (embodiplan 0.2.7). Links to lines that did not change since `a69064255c69` still point at that earlier commit, which is a valid permalink. The code may change, and then an entry may no longer be true. They are the behaviour today, not a list of intended features. Each entry was checked in the code; "reproduced" means it was run offline with a scripted LLM reply or a fake `requests.post` (no real model, no network).

!!! abstract "In this page"
    - Most failures do not raise. A planner usually catches the error and returns an empty plan with `parse_ok=False`. Always check `parse_ok` and `num_steps`.
    - The default host is a remote server, and `provider="anthropic"` does not work.
    - `parse_ok=True` does not mean the plan is valid. Only the [action model](reference/action-model.md) checks rules.

Links in the "Cause" lines use the pinned commit.

## Setup and backends

### `provider="anthropic"` gives an empty plan, not an error

- **Symptom.** The README ([line 120](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/README.md#L120)) and several docstrings name Anthropic as a backend. If you build a planner with `provider="anthropic"`, construction works and `generate_plan` returns `[]` with `parse_ok=False` and no exception.
- **Cause.** `LLMBackend.chat` supports only `ollama`, `openai` and `gemini` and raises `ValueError` for anything else ([`base.py:270`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/base.py#L270)). Each planner catches that error. Reproduced (`host` points to a closed port, no request is made):

```text
Direct       | [] False 1 notes=''                                        extra={}
CoT          | [] False 1 notes=''                                        extra['reasoning']="Unknown provider 'anthropic'. Use: ollama, openai, gemini"
Few-Shot CoT | [] False 1 notes=''                                        extra['reasoning']= same text
Self-Refine  | [] False 1 notes=''                                        extra['error']= same text
ReAct        | [] False 0 notes=''                                        extra={}
Hierarchical | [] False 0 notes=''                                        extra={}
SayCan       | [] False 0 notes='score_error:ValueError'
LLM+P        | [] False 0 notes='translate_call_error:ValueError|fallback_error:ValueError'  llmp_fallback=True
SAGE         | [] False 0 notes=''                                        extra={}
```

  Columns: plan, `parse_ok`, `llm_calls`. Where the text goes differs by planner: Direct, ReAct, Hierarchical and SAGE drop it; CoT and Few-Shot CoT keep it in `extra["reasoning"]`; Self-Refine in `extra["error"]`; SayCan and LLM+P in `notes`.
- **Avoid.** Use `ollama`, `openai` or `gemini`. Treat `parse_ok=False` with an empty plan as a failed call. To see the real error, call `planner._chat([...])` yourself. The same applies to a bad key or a closed server.
- **Detail.** [LLM calls and cost](concepts/llm-calls.md), [Interface](reference/interface.md), [Single-call planners](planners/single-call.md).

### The default host is remote

- **Symptom.** A planner built without `host=` sends its prompts to `http://ollama.aistations.org`. Reproduced: `CoTPlanner(model="m").host` and `embodiplan.cot(model="m").host` both print `http://ollama.aistations.org`.
- **Cause.** `DEFAULT_HOST` ([`base.py:23`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/base.py#L23)) is used by `BasePlanner` and by every factory in [`__init__.py`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/__init__.py#L152).
- **Avoid.** Always pass `host=`, for example `host="http://localhost:11434"`.
- **Detail.** [Getting started](getting-started.md), [LLM calls and cost](concepts/llm-calls.md).

### Short names give the factory function, not the module

- **Symptom.** `from embodiplan import cot` gives a function. The same holds for `direct, few_shot, self_refine, react, hierarchical, llm_router, sage`. `import embodiplan.cot as m` also returns the function, so a patch such as `m.something = ...` or `monkeypatch.setattr("embodiplan.cot.X", ...)` hits the wrong object. Reproduced:

```text
function function module
```

  (`type(embodiplan.cot)`, `type(m)` after `import embodiplan.cot as m`, `type(sys.modules["embodiplan.cot"])`.)
- **Cause.** The factories in [`__init__.py` L152-L229](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/__init__.py#L152-L229) have the same names as the modules and replace them as attributes of the package.
- **Avoid.** Use `from embodiplan.cot import CoTPlanner`, or `importlib.import_module("embodiplan.cot")`, or `sys.modules["embodiplan.cot"]` when you patch.
- **Detail.** [Interface](reference/interface.md).

### Planners cannot set `seed`

- **Symptom.** `CoTPlanner(..., seed=7)` runs, but the backend seed stays `None`. `embodiplan.cot(model="m", seed=7)` raises `TypeError: cot() got an unexpected keyword argument 'seed'`. Reproduced.
- **Cause.** `LLMBackend` has a `seed` option that goes to Ollama ([`base.py:206-215`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/base.py#L206-L215), [`L334-L335`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/base.py#L334-L335)). `BasePlanner.__init__` builds the backend without it and swallows extra keywords in `**kwargs` ([`base.py:543-548`](https://github.com/mtbui2010/embodiplan/blob/76da520/embodiplan/base.py#L543-L548)).
- **Avoid.** After construction set `planner._backend.seed = 7` (private attribute; this assignment was run and `planner._backend.seed` printed `7`; that the seed then reaches Ollama is read from `base.py:334-335`, not run). Runs are not reproducible by default.
- **Detail.** [LLM calls and cost](concepts/llm-calls.md).

### Token counts are estimated on the Ollama path

- **Symptom.** When the Ollama server returns no counts, `input_tokens` and `output_tokens` are guessed as `len(text) // 4`. Reproduced with a fake `requests.post` that returns only `{"message": {"content": "hello world"}}` for a 400-character prompt: `('hello world', 100, 2)`. The OpenAI path reads `usage` and gives `0` if it is missing ([`base.py:435`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/base.py#L435)). Gemini was not run.
- **Cause.** [`base.py:390-393`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/base.py#L390-L393) and [`base.py:471-472`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/base.py#L471-L472).
- **Avoid.** Do not treat token numbers as exact for local models.
- **Detail.** [LLM calls and cost](concepts/llm-calls.md).

### Other setup items

- **A long generation can time out.** The default Ollama timeout is 120 s; raise `OLLAMA_TIMEOUT` or set `OLLAMA_STREAM=1` (read from the code, not run). See [LLM calls](concepts/llm-calls.md).
- **`embodiplan.sage()` has only 10 parameters.** For `actions`, `capabilities`, `robot_description` or the `enable_*` switches build `SAGEPlanner(...)` directly. See [Interface](reference/interface.md).
- **Objects beyond 30 are cut** from prompts without a warning. See [Interface](reference/interface.md).

## Plan format and parsing

### `{"plan": [...]}` gives an empty plan

- **Symptom.** A reply shaped as `{"plan": [...]}` parses to `[]`; Direct, CoT and Few-Shot CoT then report `parse_ok=False`.
- **Cause.** `parse_steps` reads only the key `steps`.
- **Avoid.** Keep the prompt's JSON shape. Check `parse_ok`.
- **Detail.** [Plan format](concepts/plan-format.md), [Single-call](planners/single-call.md#direct).

### Direct loses the error text

- **Symptom.** After a failed call Direct returns `[]`, `notes=""`, `extra={}` and `llm_calls=1`. A failed call looks like a reply with no plan.
- **Cause.** The exception is kept in a local variable only ([`direct.py:45-46`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/direct.py#L45-L46)).
- **Avoid.** Call `planner._chat(...)` directly to see the error, or use CoT.
- **Detail.** [Single-call, Direct](planners/single-call.md#direct).

### The documented CoT call count is wrong

- **Symptom.** The repo's `docs/usage.md` says CoT takes "2 calls" ([line 28](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/docs/usage.md#L28)). The code makes 1. Reproduced with a fake `_chat`:

```text
1 1
```

  (`llm_calls`, `num_steps`.)
- **Cause.** CoT writes the reasoning and the plan in one reply and sets `llm_calls=1` ([`cot.py:90`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/cot.py#L90), [`cot.py:124`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/cot.py#L124)).
- **Avoid.** Trust `metrics.llm_calls`, not the table.
- **Detail.** [Single-call, CoT](planners/single-call.md#cot).

### Few-Shot CoT has 22 examples, not 3

- **Symptom.** The header comment of `few_shot.py` and the class description say "3 hand-crafted examples". The prompt holds 22 and is about 22,000 characters. Reproduced: `FEW_SHOT_EXAMPLES.count("=== EXAMPLE ")` prints `22`.
- **Cause.** Stale comments ([`few_shot.py:4`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/few_shot.py#L4), [`few_shot.py:168`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/few_shot.py#L168)); the examples live in [`few_shot_examples.py`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/few_shot_examples.py).
- **Avoid.** Expect a large prompt on small-context models. You cannot pass your own examples.
- **Detail.** [Single-call, Few-Shot CoT](planners/single-call.md#few-shot-cot).

### Few-Shot CoT's text fallback guesses

- **Symptom.** `Stop the coffee machine` becomes `TurnOff`, `Start ...` becomes `TurnOn`; lines with no verb hint are dropped; objects may be invented. `parse_ok=True` also holds for a fallback plan.
- **Cause.** First matching verb hint wins ([`few_shot.py:95-96`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/few_shot.py#L95-L96)).
- **Avoid.** Read `extra["fallback_used"]`; compare `num_steps` with the reply lines; check the plan with `simulate`.
- **Detail.** Shown with a run on [Single-call, Few-Shot CoT](planners/single-call.md#few-shot-cot).

## The symbolic gate

### The container example the prompts teach fails the default gate

- **Symptom.** `JSON_EXAMPLE` and the container sequence in `STEP_SCHEMA` do `Open Fridge`, `Pick Apple`, `Close Fridge`. With the default one hand, `simulate` gives `hand_full` (step 7 for the example). A model that copies the example is rejected by the gate.
- **Cause.** The prompt text ([`base.py:127-138`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/base.py#L127-L138)) and the rule `Capabilities.hands = 1` ([`action_model.py:65`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/action_model.py#L65), check at [`L359-L360`](https://github.com/mtbui2010/embodiplan/blob/76da520/embodiplan/action_model.py#L359-L360)).
- **Avoid.** Pass `capabilities={"hands": 2}` when you want this pattern. Both plans pass with two hands.
- **Detail.** The run is on [Action model, Failure modes](reference/action-model.md#failure-modes); the prompt side is on [Prompts](reference/prompts.md#failure-modes).

### Rules the model does not have

- `Pick` with a different object than the last `Find` gives `pick_not_found`, although `STEP_SCHEMA` says the field is ignored. There is no containment model (`Find Apple` after `MoveTo Fridge` with the door closed passes). `MoveTo <room>` clears `found`. Unlisted container names (such as `Basket`) are not checked. See [Action model](reference/action-model.md#failure-modes).
- The bundled reference plans often break the rules: only 12 of 38 pass `simulate`; the most common first error is `Pick` without `Find` (19 plans). See [Memory](concepts/memory.md).
- The SAGE gate does not pass `actions=` to the model ([`sage.py:458-460`](https://github.com/mtbui2010/embodiplan/blob/76da520/embodiplan/sage.py#L458-L460)), so a robot's own action names are reported as `unknown_action`. Use `enable_verifier=False` or `safe_refine=True`. See [Prompts](reference/prompts.md#failure-modes).

## Critique loops

### Self-Refine erases the plan on an unparseable refine reply

- **Symptom.** A good first plan is lost if the refine reply cannot be parsed. Reproduced on the page: replies plan, critique, `Sorry, I cannot.` gave `[]`, `llm_calls=3`, `parse_ok=False`.
- **Cause.** The refine result is assigned to `steps` with no empty check ([`self_refine.py:174-175`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/self_refine.py#L174-L175)).
- **Avoid.** Check `parse_ok`; keep `max_iterations` small; keep your own copy of a first plan if it matters.
- **Detail.** [Critique, Self-Refine](planners/critique.md#self-refine) (with the other items: `NO_ISSUES` matched anywhere, shallow equality).

### LLM Router: three defects

All three were reproduced offline; the runs are on [Critique, LLM Router](planners/critique.md#llm-router).

1. **"NOT APPROVED" counts as approval.** The test is `"APPROVED" in content.upper()` ([`llm_router.py:82`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/llm_router.py#L82), Gemini [`L111`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/llm_router.py#L111)), so a rejection that contains the word keeps the local plan and drops the correction. Avoid: do not rely on the router to detect a rejection.
2. **`anthropic_api_key` and `verifier_backend="anthropic"` do nothing useful.** The factory passes the key ([`__init__.py:225`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/__init__.py#L225)) but the class swallows it in `**kwargs`; any backend other than `"gemini"` takes the OpenAI branch ([`llm_router.py:148-162`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/llm_router.py#L148-L162)). The README example ([line 134](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/README.md#L134)) therefore uses OpenAI. Avoid: use `"openai"` or `"gemini"`.
3. **A skipped check is counted as a call, and the plan is unchecked.** With no key, nothing is sent but `llm_calls = local + 1` ([`llm_router.py:177`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/llm_router.py#L177)); HTTP errors only appear in `notes`. Avoid: set the key and read `extra["verifier_note"]` after each plan.

## Step-by-step planners

### ReAct stops on the word "done" anywhere

- **Symptom.** A thought such as `When that is done, find the mug.` ends the loop and the `Find` is lost, with no error. Reproduced on the page: `1 steps, 2 LLM calls`.
- **Cause.** `_is_done` searches the whole reply for `\bDone\b`, ignoring case ([`react.py:62-63`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/react.py#L62-L63)); the check runs before the action is parsed ([`react.py:127`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/react.py#L127)).
- **Avoid.** Compare `num_steps` with what you expect; run `simulate` on the result. One unparseable reply also ends the loop.
- **Detail.** [Step-by-step, ReAct](planners/stepwise.md#react).

### SayCan can loop and ignores `obs` and `capabilities`

- A bad scoring reply makes all scores equal and the same `MoveTo` repeats (`3 calls` with `max_steps=3`); `obs` is ignored; the gate is always one-handed; errors go to `notes`. See [Step-by-step, SayCan](planners/stepwise.md#saycan).

## Decomposition planners

### Hierarchical and Hierarchical Few-Shot raise `KeyError`

- **Symptom.** If an expanded step has no `action` key and it is not the first step, `generate_plan` raises `KeyError: 'action'`. Reproduced on the page with the reply `{"steps": [{"object": "Mug"}, {"object": "Cup"}]}`.
- **Cause.** The dedupe loop reads `s["action"]` outside any `try` ([`hierarchical.py:131-133`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/hierarchical.py#L131-L133); [`my_planner.py:247-255`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/my_planner.py#L247-L255)).
- **Avoid.** Wrap `generate_plan` in `try`.
- **Detail.** [Decomposition planners](planners/hierarchical.md#failure-modes). That page also covers one sub-goal per word, JSON keys kept by the fallback parser, and hidden errors.

## LLM+P

### A quiet fallback, and two different domains

- **Symptom 1.** Without `pyperplan` no error is raised: the planner makes the translate call, throws the reply away, and makes a Direct-style call. `parse_ok=True`. Reproduced here (scripted replies; `pyperplan` is not installed on this machine):

```text
[LLM+P] FALLBACK 1 steps in 0.0s (err=pyperplan_missing:pyperplan unavailable: No module named 'pyperplan')
[{'action': 'MoveTo', 'object': 'Kitchen'}] True 2 True pyperplan_missing:pyperplan unavailable: No module named 'pyperplan'
```

  (plan, `parse_ok`, `llm_calls`, `llmp_fallback`, `llmp_error`.) The import is lazy and the failure is caught ([`llmp.py:562-567`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/llmp.py#L562-L567); fallback at [`L606-L612`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/llmp.py#L606-L612)).
- **Symptom 2.** The code comment says the embedded domain is "byte-compatible" with `pddl/household_domain.pddl` ([`llmp.py:50-51`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/llmp.py#L50-L51)). It is not. Run: `file == embedded` is `False`; 5311 versus 1724 characters; the file has a long header and a typed `:types` block, the embedded string is shorter. The file wins when it exists next to the package (a source checkout); the embedded string is used on an install without it ([`llmp.py:105-125`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/llmp.py#L105-L125)). I did not compare the actions one by one.
- **Avoid.** Install `embodiplan[llmp]` and check `extra["llmp_fallback"]` is `False`. Set `EMBODIPLAN_PDDL_DOMAIN` or `domain_path` to know which domain runs.
- **Detail.** [LLM+P](planners/llm-p.md#failure-modes).

## SAGE and memory

### SAGE memory is empty unless you give `gt_path` or `live_path`

- **Symptom.** `retrieved_tasks == []` and the prompts hold no examples. Reproduced:

```text
MemoryRetriever {'seed': 0, 'live': 0}
0
3
```

  (a default `SAGEPlanner`: pool sizes, then hits for `"make coffee"`; with `gt_path="eval_dataset_gt.json"` it finds 3.)
- **Cause.** `gt_path` and `live_path` default to `""` ([`sage.py:371-372`](https://github.com/mtbui2010/embodiplan/blob/76da520/embodiplan/sage.py#L371-L372), [`memory_retriever.py:163-164`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/memory_retriever.py#L163-L164)) and the seed file loads only when `gt_path` is set ([`memory_retriever.py:190-193`](https://github.com/mtbui2010/embodiplan/blob/a69064255c69/embodiplan/memory_retriever.py#L190-L193)). The bundled file is not loaded on its own.
- **Avoid.** Pass `gt_path` (see [Memory](concepts/memory.md)). Seed examples can themselves break the gate's rules.
- **Detail.** [SAGE](planners/sage.md#failure-modes).

### A SAGE refine that parses to `[]` is accepted

- **Symptom.** The plan for a sub-goal becomes empty after a refine whose reply could not be read. Reproduced on [SAGE](planners/sage.md#failure-modes): `steps=0`, `llm_calls=3`, `refines=1`.
- **Cause.** An empty plan has no violations, so it passes the re-check. Only `safe_refine=True` keeps the old steps ([`sage.py:725`](https://github.com/mtbui2010/embodiplan/blob/76da520/embodiplan/sage.py#L725); in repair [`sage.py:906`](https://github.com/mtbui2010/embodiplan/blob/76da520/embodiplan/sage.py#L906)). The default is `False` ([`sage.py:379`](https://github.com/mtbui2010/embodiplan/blob/76da520/embodiplan/sage.py#L379)).
- **Avoid.** Pass `safe_refine=True`.
- **Detail.** [SAGE](planners/sage.md), [SAGE repair](planners/sage-repair.md#failure-modes) (task string must match for `replan`; a failed repair call looks like a skipped call; a rejected refine keeps an invalid suffix).

## Summary table

| Pitfall | Visible sign | Check |
|---|---|---|
| `anthropic` provider | `[]`, `parse_ok=False`, no exception | provider name; `_chat` by hand |
| Remote default host | prompts leave your machine | pass `host=` |
| Short names are functions | patch has no effect | `sys.modules` |
| No `seed` | different runs | set `_backend.seed` |
| Estimated tokens (Ollama) | round numbers | do not trust |
| Container example | `hand_full` | `hands: 2` |
| Self-Refine refine fail | `[]` after a good plan | `parse_ok` |
| LLM Router | `approved` for a rejection | `verifier_note` |
| ReAct "done" | short plan, no error | `num_steps` |
| Hierarchical `KeyError` | exception | `try` |
| LLM+P fallback | `llmp_fallback=True` | `extra` |
| SAGE no memory | `retrieved_tasks == []` | `gt_path` |
| SAGE empty refine | empty block | `safe_refine=True` |

Entries I could not run: whether the seed reaches a real Ollama server, Gemini token counts, the long-generation timeout, SayCan's 18-turn loop and the `SIGALRM` limit of LLM+P in a worker thread. They come from reading the code and are marked above.
