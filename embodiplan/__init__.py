# embodiplan/__init__.py
"""
embodiplan — pluggable LLM task planner for household robotics.

Usage
-----
    import embodiplan

    planner = embodiplan.direct(host="http://localhost:11434", model="llama3.2")
    steps, metrics = planner.generate_plan(
        task="make coffee",
        obs="Kitchen scene loaded.",
        visible_objects=["coffee_machine", "mug", "counter_top"],
    )

Or via registry:

    planner = embodiplan.get("CoT", host=..., model=...)

Available methods
-----------------
    embodiplan.direct()        — single-call baseline
    embodiplan.cot()           — Chain-of-Thought (reason then plan)
    embodiplan.few_shot()      — Few-Shot CoT with built-in examples
    embodiplan.self_refine()   — generate → critique → refine loop
    embodiplan.react()         — ReAct (one action per LLM call)
    embodiplan.hierarchical()  — sub-goals → action expansion
    embodiplan.llm_router()    — local generate + external API verify
    embodiplan.sage()          — SAGE: symbolic gate + local edit + hybrid memory
"""

from embodiplan.base import (
    BasePlanner,
    PlanMetrics,
    LLMBackend,
    parse_steps,
    ROBOT_ACTIONS,
    PROVIDER_MODELS,
    DEFAULT_HOST,
    DEFAULT_MODEL,
    DEFAULT_BACKEND,
)
from embodiplan.direct       import DirectPlanner
from embodiplan.cot          import CoTPlanner
from embodiplan.few_shot     import FewShotPlanner
from embodiplan.self_refine  import SelfRefinePlanner
from embodiplan.react        import ReActPlanner
from embodiplan.hierarchical import HierarchicalPlanner
from embodiplan.llm_router   import LLMRouterPlanner
from embodiplan.my_planner   import HierarchicalFewShotPlanner
from embodiplan.sage         import SAGEPlanner
from embodiplan.llmp         import LLMPPlanner
from embodiplan.saycan       import SayCanPlanner
from embodiplan              import action_model
from embodiplan.action_model import ACTION_MODEL_VERSION

__version__ = "0.2.2"

import os as _os

# Bundled data: the curated 38-task AI2-THOR benchmark with simulator-verified
# reference plans (also the default seed pool for SAGE's memory).
DATA_DIR   = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data")
GT_DATASET = _os.path.join(DATA_DIR, "eval_dataset_gt.json")
__all__ = [
    "BasePlanner", "PlanMetrics", "LLMBackend", "parse_steps",
    "ROBOT_ACTIONS", "PROVIDER_MODELS", "DEFAULT_HOST", "DEFAULT_MODEL", "DEFAULT_BACKEND",
    "DirectPlanner", "CoTPlanner", "FewShotPlanner",
    "SelfRefinePlanner", "ReActPlanner", "HierarchicalPlanner", "LLMRouterPlanner",
    "HierarchicalFewShotPlanner", "SAGEPlanner", "GRACEPlanner",
    "direct", "cot", "few_shot", "self_refine", "react", "hierarchical", "llm_router",
    "hierarchical_few_shot", "sage", "grace",
    "get", "list_methods",
    "REGISTRY", "ALIASES", "DATA_DIR", "GT_DATASET",
    "action_model", "ACTION_MODEL_VERSION",
]

# Deprecated names kept so old configs and result files still resolve.
GRACEPlanner = SAGEPlanner
ALIASES: dict[str, str] = {"GRACE": "SAGE"}


class _Registry(dict):
    """dict that also resolves deprecated method names (ALIASES) on lookup,
    without listing them when iterating."""

    def __getitem__(self, key):
        return super().__getitem__(ALIASES.get(key, key))

    def __contains__(self, key):
        return super().__contains__(ALIASES.get(key, key))

    def get(self, key, default=None):
        return super().get(ALIASES.get(key, key), default)


# ── Registry ──────────────────────────────────────────────────────────
REGISTRY: dict[str, type[BasePlanner]] = _Registry({
    "Direct":                DirectPlanner,
    "CoT":                   CoTPlanner,
    "Few-Shot CoT":          FewShotPlanner,
    "Self-Refine":           SelfRefinePlanner,
    "ReAct":                 ReActPlanner,
    "Hierarchical":          HierarchicalPlanner,
    "LLM Router":            LLMRouterPlanner,
    "Hierarchical Few-Shot": HierarchicalFewShotPlanner,
    "SAGE":                  SAGEPlanner,
    "LLM+P":                 LLMPPlanner,
    "SayCan":                SayCanPlanner,
})


def get(
    name: str,
    host: str  = DEFAULT_HOST,
    model: str = DEFAULT_MODEL,
    **kwargs,
) -> BasePlanner:
    """
    Instantiate a planner by registry name.

    Args:
        name:   One of "Direct", "CoT", "Few-Shot CoT", "Self-Refine",
                        "ReAct", "Hierarchical", "LLM Router".
        host:   Ollama server URL.
        model:  Ollama model name.
        **kwargs: Method-specific options (e.g. max_steps for ReAct).

    Returns:
        A ready-to-use BasePlanner instance.

    Example:
        planner = embodiplan.get("ReAct", model="llama3.2", max_steps=12)
    """
    cls = REGISTRY.get(name)
    if cls is None:
        available = ", ".join(f'"{k}"' for k in REGISTRY)
        raise ValueError(f"Unknown method '{name}'. Available: {available}")
    # provider and api_key are passed via **kwargs if supplied
    return cls(host=host, model=model, **kwargs)


def list_methods() -> list[dict]:
    """Return info about all registered planning methods."""
    return [
        {"name": name, "class": cls.__name__, "description": cls.description}
        for name, cls in REGISTRY.items()
    ]


# ── Shorthand factory functions ────────────────────────────────────────
def direct(host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL,
           provider: str = DEFAULT_BACKEND, api_key: str = "") -> DirectPlanner:
    """Baseline: single LLM call → JSON plan."""
    return DirectPlanner(host=host, model=model, provider=provider, api_key=api_key)


def cot(host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL,
        provider: str = DEFAULT_BACKEND, api_key: str = "") -> CoTPlanner:
    """Chain-of-Thought: reason first, then plan."""
    return CoTPlanner(host=host, model=model, provider=provider, api_key=api_key)


def few_shot(host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL,
             provider: str = DEFAULT_BACKEND, api_key: str = "") -> FewShotPlanner:
    """Few-Shot CoT: built-in examples anchor the output style."""
    return FewShotPlanner(host=host, model=model, provider=provider, api_key=api_key)


def self_refine(
    host: str = DEFAULT_HOST,
    model: str = DEFAULT_MODEL,
    provider: str = DEFAULT_BACKEND,
    api_key: str = "",
    max_iterations: int = 2,
) -> SelfRefinePlanner:
    """Self-Refine: generate → critique → fix, repeated max_iterations times."""
    return SelfRefinePlanner(host=host, model=model, provider=provider, api_key=api_key, max_iterations=max_iterations)


def react(
    host: str = DEFAULT_HOST,
    model: str = DEFAULT_MODEL,
    provider: str = DEFAULT_BACKEND,
    api_key: str = "",
    max_steps: int = 15,
) -> ReActPlanner:
    """ReAct: one Thought+Action per LLM call, grounded planning."""
    return ReActPlanner(host=host, model=model, provider=provider, api_key=api_key, max_steps=max_steps)


def hierarchical(host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL,
                 provider: str = DEFAULT_BACKEND, api_key: str = "") -> HierarchicalPlanner:
    """Hierarchical: decompose to sub-goals, then expand each to actions."""
    return HierarchicalPlanner(host=host, model=model, provider=provider, api_key=api_key)


def hierarchical_few_shot(
    host: str  = DEFAULT_HOST,
    model: str = DEFAULT_MODEL,
    provider: str = DEFAULT_BACKEND,
    api_key: str  = "",
    top_k: int    = 3,
) -> HierarchicalFewShotPlanner:
    """Hierarchical + dynamic few-shot: retrieves top-k similar examples, then decomposes & expands."""
    return HierarchicalFewShotPlanner(host=host, model=model, provider=provider,
                                      api_key=api_key, top_k=top_k)


def llm_router(
    host: str = DEFAULT_HOST,
    model: str = DEFAULT_MODEL,
    verifier_backend: str = "openai",
    verifier_model: str = "",
    openai_api_key: str = "",
    anthropic_api_key: str = "",
) -> LLMRouterPlanner:
    """LLM Router: local model generates, external API (OpenAI/Claude) verifies."""
    return LLMRouterPlanner(
        host=host,
        model=model,
        verifier_backend=verifier_backend,
        verifier_model=verifier_model,
        openai_api_key=openai_api_key,
        anthropic_api_key=anthropic_api_key,
    )


def sage(
    host:           str  = DEFAULT_HOST,
    model:          str  = DEFAULT_MODEL,
    provider:       str  = DEFAULT_BACKEND,
    api_key:        str  = "",
    top_k:          int  = 3,
    max_refines:    int  = 1,
    gt_path:        str  = "",
    live_path:      str  = "",
    use_chroma:     bool = False,
    chroma_path:    str  = "",
) -> SAGEPlanner:
    """SAGE: hierarchical + symbolic precondition verifier + hybrid memory
    retrieval + local sub-goal repair. The most complete planner."""
    return SAGEPlanner(
        host=host, model=model, provider=provider, api_key=api_key,
        top_k=top_k, max_refines=max_refines,
        gt_path=gt_path, live_path=live_path,
        use_chroma=use_chroma, chroma_path=chroma_path,
    )


# Deprecated alias of sage().
grace = sage
