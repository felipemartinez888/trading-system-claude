"""Preregistration schema, validation, freeze and selection records (M4-T1 deliverable 1).

A preregistration (``preregistrations/<prereg_id>.yaml``, schema ``tsc-prereg-v1``) fixes, before
any result, everything a hypothesis's research run depends on: the strategy and its search space,
the universe, the data segments and dataset pins, the walk-forward plan, the objective, the
selection rule, the trial budget, the rejection criteria and the max-loss policy.

**Owner policy stays pending.** Three fields encode owner decisions that are still open
(``trial_budget.counting``: D-C; ``holdout_procedure``: D-D; ``risk_compatibility``: D-E). While any
of them is ``PENDING``, `validate` reports it as an open decision and `freeze` refuses. This module
never chooses a policy.

**Freeze** (`freeze`) records, inside the file, the SHA-256 of its own content (without the
``frozen`` block), an implementation hash of the strategy code (the strategy's module, the
strategy base class, every feature module and the research signal adapter), the parameter hash of
every configuration and the code commit. A frozen file that was edited, or whose strategy code
changed, no longer validates. Freezing writes the file; committing it is the owner's act (a
hypothesis freeze is an owner decision).

**Selection record** (``kind: selection``, `make_selection_record`): after a walk-forward of a
frozen search preregistration has a positive verdict and a final-selection candidate, the record
names the one selected strategy hash (``selected.strategy_hash``), its parent's content hash and
the report's hash. It deliberately has **no top-level** ``strategy_hash``: that is the field the
holdout guard reads (`store.holdout.inspect_preregistration`), so a selection record cannot unlock
the holdout. Whether and how a selection record may be used for validation and the holdout is
decision D-D; adopting it needs an owner-approved change of the guard.

**Draft strategies.** A search preregistration may name a strategy that is only in
`strategies.DRAFT_REGISTRY` (implemented, awaiting an owner freeze): it is validated like any
other, and ``DRAFT_OPEN_ITEM`` is listed among the open decisions, so `freeze` refuses until the
owner promotes the strategy to `REGISTRY` (and no run can build it before that).

The audit trail is git: every preregistration and selection record is a committed file, and the
runner and holdout guard accept only committed, clean files (`research.prereg`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from tsclaude.contracts.events import canonical_json

if TYPE_CHECKING:
    from tsclaude.risk.config import RiskConfig

SCHEMA = "tsc-prereg-v1"
PENDING = "PENDING"
#: the objective is fixed by ADR-0012 and RESEARCH_PROTOCOL §5
OBJECTIVE = "certified_margin_lcb_then_p_lcb"
#: owner decisions a preregistration cannot settle by itself: field path -> decision id
POLICY_FIELDS: dict[str, str] = {
    "trial_budget.counting": "D-C",
    "holdout_procedure": "D-D",
    "risk_compatibility": "D-E",
}
#: the only values each policy field may take besides PENDING: the options of its decision in
#: DECISIONS_PENDING. Anything else (a typo, "TBD", an empty string) is a problem, never a choice.
POLICY_VALUES: dict[str, tuple[str, ...]] = {
    "trial_budget.counting": ("configurations", "registry_rows"),
    "holdout_procedure": ("two-stage-selection-record",),
    "risk_compatibility": (
        "live-floor-kept-geometries-dropped",
        "live-floor-replaced-for-validated",
    ),
}
#: data segments fixed by D-05 (RESEARCH_PROTOCOL §2); a preregistration may not move them
SEGMENTS = {
    "development": ["2022-01-01", "2025-01-01"],
    "validation": ["2025-01-01", "2026-01-01"],
    "holdout": ["2026-01-01", "2026-10-01"],
}
KINDS = ("search", "selection")
#: the only keys a file may have; an unknown key (a typo such as ``backtest_config_hsh``) would
#: otherwise silently disable what it meant to fix (re-review N5)
SEARCH_KEYS = frozenset({
    "schema", "prereg_id", "kind", "title", "rationale", "strategy", "search_space", "universe",
    "data", "walk_forward", "objective", "selection_rule", "trial_budget", "holdout_procedure",
    "risk_compatibility", "rejection_criteria", "max_loss_r_policy", "min_oos_coverage",
    "backtest_config_hash", "frozen",
})  # fmt: skip
SELECTION_KEYS = frozenset({
    "schema", "prereg_id", "kind", "parent", "selected", "walk_forward", "trial_budget",
    "holdout_procedure", "risk_compatibility",
})  # fmt: skip
WALK_FORWARD_KEYS = frozenset({
    "anchor", "end", "initial_train_months", "test_months", "embargo_minutes",
    "time_exit_required", "n_min", "min_oos_coverage",
})  # fmt: skip
#: closed key sets of the nested blocks (re-review R2: ``data.dataset_id`` would leave the pin null)
NESTED_KEYS = {
    "strategy": frozenset({"strategy_id", "version"}),
    "universe": frozenset({"market", "symbols"}),
    "data": frozenset({"development", "validation", "holdout", "dataset_ids"}),
    "trial_budget": frozenset({"max_configurations", "max_registry_trials", "counting"}),
}
_HEX64 = frozenset("0123456789abcdef")
SELECTION_RULES = ("full-development-train-v1",)
#: open item listed (not a problem) for a strategy that is only in `strategies.DRAFT_REGISTRY`
DRAFT_OPEN_ITEM = "strategy is a DRAFT (not runnable until an owner-approved promotion to REGISTRY)"
REQUIRED_REJECTION = (
    "certified margin lower bound <= 0",
    "expectancy lower bound <= 0",
    "fewer than 300 effective trades",
    "cost stress x1.5 not certified",
    "no edge over the random-entry control",
    "any integrity problem (grade X)",
)


class PreregistrationInvalid(ValueError):
    """The file does not satisfy the schema or its freeze (problems listed)."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass
class Validation:
    path: str
    kind: str | None
    frozen: bool
    problems: list[str] = field(default_factory=list)
    open_decisions: list[str] = field(default_factory=list)
    configurations: dict[str, str] = field(default_factory=dict)  # config id -> params hash
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "frozen": self.frozen,
            "ok": self.ok,
            "problems": list(self.problems),
            "open_decisions": list(self.open_decisions),
            "configurations": dict(self.configurations),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------- hashing
def content_sha256(doc: Mapping[str, Any]) -> str:
    """The file's identity: canonical JSON of every field except the ``frozen`` block."""
    body = {k: v for k, v in doc.items() if k != "frozen"}
    return hashlib.sha256(canonical_json(_jsonable(body)).encode("utf-8")).hexdigest()


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def implementation_files(strategy_id: str) -> list[Path]:
    """The code a strategy's behaviour depends on: its module, the strategy base, every feature
    module and the research signal adapter."""
    import inspect

    from tsclaude import features, research, strategies
    from tsclaude.strategies import REGISTRY, build_strategy

    if strategy_id not in REGISTRY:
        raise ValueError(f"unknown strategy {strategy_id!r}")
    module = Path(inspect.getfile(type(build_strategy(strategy_id, {}))))
    strat_dir = Path(next(iter(strategies.__path__)))
    feat_dir = Path(next(iter(features.__path__)))
    research_dir = Path(next(iter(research.__path__)))
    files = {module, strat_dir / "base.py", research_dir / "source.py"}
    files.update(feat_dir.glob("*.py"))
    return sorted(files)


def implementation_sha256(strategy_id: str) -> str:
    root = Path(next(iter(__import__("tsclaude").__path__))).parent
    parts = []
    for p in implementation_files(strategy_id):
        parts.append(f"{p.resolve().relative_to(root.resolve()).as_posix()}:{_sha_file(p)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def engine_sha256() -> str:
    """Every module of the packages a research run executes (contracts, features, strategies,
    sim, risk, quality, research) and the backtest, risk and interruption configs. Recorded at
    freeze and reported by `validate` when it changed; not enforced, because every engine bug fix
    would otherwise void every freeze. The enforced `implementation_sha256` covers the
    strategy's own code; the walk-forward plan id covers the exact code commit of every run."""
    import tsclaude

    pkg = Path(next(iter(tsclaude.__path__)))
    repo = pkg.parents[1]
    files = []
    for sub in ("contracts", "features", "strategies", "sim", "risk", "quality", "research"):
        files += sorted((pkg / sub).rglob("*.py"))
    files += [repo / "configs" / n for n in ("backtest.yaml", "risk.yaml",
                                              "exchange_interruptions.yaml")]  # fmt: skip
    parts = []
    for f in files:
        if f.is_file():
            parts.append(f"{f.resolve().relative_to(repo.resolve()).as_posix()}:{_sha_file(f)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- helpers
def _get(doc: Mapping[str, Any], dotted: str) -> Any:
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def load(path: Path | str) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PreregistrationInvalid([f"{path} must be a YAML mapping"])
    # YAML reads unquoted dates as date objects: compare and hash them as ISO strings
    return dict(_jsonable(raw))


def _month_start(text: Any) -> bool:
    try:
        d = datetime.strptime(str(text), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return False
    return d.day == 1


KEEP_LIVE_FLOOR = "live-floor-kept-geometries-dropped"
#: the risk config the repo's backtest config applies (configs/backtest.yaml ``risk_config``)
REPO_RISK_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "risk.yaml"


def live_floor_problems(
    doc: Mapping[str, Any], *, risk_config: Path | None = None, risk: RiskConfig | None = None
) -> list[str]:
    """With ``risk_compatibility: live-floor-kept-geometries-dropped`` (D-E: keep the live floor),
    every configuration must be COMPATIBLE with the live baseline book for every symbol of the
    universe (`research.live_compat`, the paper engine's own reward/risk function): a
    configuration that the live floor rejects is never searched. Fail closed: no risk config, no
    bracket geometry or any error is a problem."""
    from tsclaude.contracts.market import Market
    from tsclaude.research.live_compat import COMPATIBLE, assess
    from tsclaude.risk.config import RiskConfigError, load_risk_config
    from tsclaude.sim.costs import CostModel

    if risk is None:
        try:
            risk = load_risk_config(risk_config or REPO_RISK_CONFIG)
        except RiskConfigError as exc:
            return [f"risk_compatibility {KEEP_LIVE_FLOOR}: cannot load the risk config ({exc})"]
    sid = str(_get(doc, "strategy.strategy_id"))
    build = _builder(sid)
    market = Market(str(_get(doc, "universe.market")))
    out: list[str] = []
    for i, params in enumerate(doc.get("search_space") or []):
        try:
            strategy = build(sid, params)
            for symbol in _get(doc, "universe.symbols") or []:
                a = assess(strategy, risk=risk, cost_model=CostModel(), market=market,
                           symbol=str(symbol))  # fmt: skip
                if a.status != COMPATIBLE:
                    out.append(
                        f"search_space[{i}] is {a.status} with the live floor min_rr_net "
                        f"{a.min_rr_net} for {symbol} ({a.reason}); D-E keeps the floor, so "
                        "the configuration must be dropped"
                    )
                    break
        except Exception as exc:  # fail closed
            out.append(f"search_space[{i}]: live compatibility not assessable ({exc})")
    return out


def _builder(strategy_id: str) -> Any:
    """`build_strategy` for runnable ids, `build_draft_strategy` for draft-only ids."""
    from tsclaude.strategies import DRAFT_REGISTRY, REGISTRY, build_draft_strategy, build_strategy

    if strategy_id not in REGISTRY and strategy_id in DRAFT_REGISTRY:
        return build_draft_strategy
    return build_strategy


def configurations_of(doc: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    """config id (content hash of the parameters, as the walk-forward uses) -> params hash."""
    from tsclaude.research.walkforward import config_id_of

    problems: list[str] = []
    out: dict[str, str] = {}
    sid = _get(doc, "strategy.strategy_id")
    build_strategy = _builder(str(sid))
    space = doc.get("search_space")
    if not isinstance(space, list) or not space or not all(isinstance(p, dict) for p in space):
        return {}, ["search_space must be a non-empty list of parameter objects"]
    for i, params in enumerate(space):
        try:
            strategy = build_strategy(str(sid), params)
        except (ValueError, TypeError) as exc:
            problems.append(f"search_space[{i}]: {exc}")
            continue
        cid = config_id_of(params)
        if cid in out:
            problems.append(f"search_space[{i}] repeats a configuration")
        out[cid] = strategy.params_hash
    return out, problems


def max_holding_ms_of(strategy: Any) -> int | None:
    """The longest scheduled holding period of a configuration (decision close to its time
    exit), or None when it has no time exit (an unbounded holding period)."""
    fn = getattr(strategy, "max_holding_ms", None)
    if callable(fn):
        return int(fn())
    bars = getattr(getattr(strategy, "params", None), "time_exit_bars", None)
    if isinstance(bars, int) and not isinstance(bars, bool):
        return (bars + 1) * int(strategy.interval.ms)
    return None


def holding_problems(doc: Mapping[str, Any]) -> list[str]:
    """RESEARCH_PROTOCOL §3: the embargo must cover one maximum holding period of every
    configuration (overnight causality review, H1 major 1)."""
    sid = _get(doc, "strategy.strategy_id")
    embargo = _get(doc, "walk_forward.embargo_minutes")
    space = doc.get("search_space")
    if not isinstance(embargo, int) or not isinstance(space, list):
        return []
    build = _builder(str(sid))
    out: list[str] = []
    for i, params in enumerate(space):
        try:
            strategy = build(str(sid), dict(params))
        except (ValueError, TypeError, AttributeError):
            continue  # reported by configurations_of
        held = max_holding_ms_of(strategy)
        if held is None:
            out.append(f"search_space[{i}] has no time exit: its holding period is unbounded")
        elif embargo * 60_000 < held:
            out.append(
                f"search_space[{i}]: embargo {embargo} min is shorter than its maximum holding "
                f"period {held // 60_000} min"
            )
    return out


# ---------------------------------------------------------------- validation
def validate_document(doc: Mapping[str, Any], *, path: str = "<memory>") -> Validation:
    from tsclaude.strategies import DRAFT_REGISTRY, REGISTRY

    kind = doc.get("kind") if doc.get("kind") in KINDS else None
    frozen = isinstance(doc.get("frozen"), Mapping)
    v = Validation(path=path, kind=kind, frozen=frozen)
    p = v.problems
    if doc.get("schema") != SCHEMA:
        p.append(f"schema must be {SCHEMA!r}")
    pid = doc.get("prereg_id")
    if not isinstance(pid, str) or not pid:
        p.append("prereg_id is required")
    elif path not in ("<memory>",) and Path(path).stem != pid:
        p.append(f"prereg_id {pid!r} does not match the file name {Path(path).name!r}")
    if kind is None:
        p.append(f"kind must be one of {list(KINDS)}")
    else:
        unknown = sorted(set(doc) - (SEARCH_KEYS if kind == "search" else SELECTION_KEYS))
        if unknown:
            p.append(f"unknown top-level keys {unknown} (a typo is never ignored)")
    for dotted, decision in POLICY_FIELDS.items():
        value = _get(doc, dotted)
        if value is None:
            p.append(f"{dotted} is required (owner decision {decision}; write {PENDING!r} if open)")
        elif value == PENDING:
            v.open_decisions.append(f"{dotted}: owner decision {decision} is open")
        elif value not in POLICY_VALUES[dotted]:
            p.append(
                f"{dotted} must be {PENDING!r} or one of {list(POLICY_VALUES[dotted])} "
                f"(owner decision {decision}); {value!r} is not a decision"
            )
    if kind == "selection":
        _validate_selection(doc, v)
        return v
    # ------------------------------------------------ search preregistration
    for name in ("title", "rationale"):
        if not isinstance(doc.get(name), str) or not str(doc.get(name)).strip():
            p.append(f"{name} is required")
    for block, allowed in NESTED_KEYS.items():
        value = doc.get(block)
        if isinstance(value, Mapping) and set(value) - allowed:
            p.append(f"unknown {block} keys {sorted(set(value) - allowed)}")
    sid = _get(doc, "strategy.strategy_id")
    if sid not in REGISTRY and sid not in DRAFT_REGISTRY:
        p.append(f"strategy.strategy_id {sid!r} is not an implemented strategy")
    else:
        if sid not in REGISTRY:
            v.open_decisions.append(DRAFT_OPEN_ITEM)
        version = _builder(str(sid))(str(sid), {}).version
        if _get(doc, "strategy.version") != version:
            p.append(f"strategy.version must be the implemented version {version!r}")
        v.configurations, more = configurations_of(doc)
        p.extend(more)
    market = _get(doc, "universe.market")
    symbols = _get(doc, "universe.symbols")
    if market not in ("spot", "usdm"):
        p.append("universe.market must be 'spot' or 'usdm'")
    if not isinstance(symbols, list) or not symbols or not all(isinstance(s, str) for s in symbols):
        p.append("universe.symbols must be a non-empty list")
    elif len(set(symbols)) != len(symbols):
        p.append("universe.symbols lists a symbol twice")
    if (
        _get(doc, "risk_compatibility") == KEEP_LIVE_FLOOR
        and market in ("spot", "usdm")
        and isinstance(symbols, list)
        and sid in {**REGISTRY, **DRAFT_REGISTRY}
    ):
        p.extend(live_floor_problems(doc))
    for seg, dates in SEGMENTS.items():
        if _get(doc, f"data.{seg}") != dates:
            p.append(f"data.{seg} must be {dates} (D-05; a preregistration cannot move it)")
    pins = _get(doc, "data.dataset_ids")
    if pins is not None and (
        not isinstance(pins, list) or not pins or not all(isinstance(x, str) for x in pins)
    ):
        p.append("data.dataset_ids must be null or a non-empty list of dataset ids")
    wf: Mapping[str, Any] = (
        doc["walk_forward"] if isinstance(doc.get("walk_forward"), Mapping) else {}
    )
    if not wf:
        p.append("walk_forward is required")
    else:
        unknown_wf = sorted(set(wf) - WALK_FORWARD_KEYS)
        if unknown_wf:
            p.append(f"unknown walk_forward keys {unknown_wf}")
        anchor = wf.get("anchor")
        if anchor != "auto" and not _month_start(anchor):
            p.append("walk_forward.anchor must be 'auto' or a UTC month start YYYY-MM-01")
        end = wf.get("end")
        if not _month_start(end) or not "2022-01-01" < str(end) <= "2025-01-01":
            p.append("walk_forward.end must be a UTC month start inside the development segment")
        for key, lo in (("initial_train_months", 1), ("test_months", 1), ("embargo_minutes", 1)):
            val = wf.get(key)
            if not isinstance(val, int) or isinstance(val, bool) or val < lo:
                p.append(f"walk_forward.{key} must be an integer >= {lo}")
        n_min = wf.get("n_min")
        if not isinstance(n_min, int) or isinstance(n_min, bool) or n_min < 300:
            p.append("walk_forward.n_min must be an integer >= 300 (the certification floor)")
        cov = wf.get("min_oos_coverage")
        if not isinstance(cov, int | float) or isinstance(cov, bool) or not 0.0 < cov <= 1.0:
            p.append("walk_forward.min_oos_coverage must be in (0, 1]")
        if wf.get("time_exit_required") is not True:
            p.append(
                "walk_forward.time_exit_required must be true: the embargo must cover one "
                "maximum holding period (RESEARCH_PROTOCOL §3)"
            )
        p.extend(holding_problems(doc))
    if doc.get("min_oos_coverage") is not None and doc.get("min_oos_coverage") != wf.get(
        "min_oos_coverage"
    ):
        p.append("min_oos_coverage, if given at the top level, must equal walk_forward's")
    fixed_cfg = doc.get("backtest_config_hash")
    if fixed_cfg is not None and not _is_hex64(fixed_cfg):
        p.append("backtest_config_hash must be null or a sha256 hex digest")
    if doc.get("objective") != OBJECTIVE:
        p.append(f"objective must be {OBJECTIVE!r} (ADR-0012)")
    if doc.get("selection_rule") not in SELECTION_RULES:
        p.append(f"selection_rule must be one of {list(SELECTION_RULES)}")
    budget: Mapping[str, Any] = (
        doc["trial_budget"] if isinstance(doc.get("trial_budget"), Mapping) else {}
    )
    maxc = budget.get("max_configurations")
    if not isinstance(maxc, int) or isinstance(maxc, bool) or maxc < 1:
        p.append("trial_budget.max_configurations must be a positive integer")
    elif v.configurations and len(v.configurations) > maxc:
        p.append(
            f"search_space has {len(v.configurations)} configurations, above "
            f"trial_budget.max_configurations {maxc}"
        )
    maxr = budget.get("max_registry_trials")
    if maxr is not None and (not isinstance(maxr, int) or isinstance(maxr, bool) or maxr < 1):
        p.append("trial_budget.max_registry_trials must be null or a positive integer")
    crit = doc.get("rejection_criteria")
    if not isinstance(crit, list) or not all(isinstance(c, str) for c in crit):
        p.append("rejection_criteria must be a list of strings")
    else:
        missing = [c for c in REQUIRED_REJECTION if c not in crit]
        if missing:
            p.append(f"rejection_criteria must include {missing}")
    mlp = doc.get("max_loss_r_policy")
    if not (
        mlp == "L-10 default"
        or (isinstance(mlp, Mapping) and isinstance(mlp.get("override_r"), int | float))
    ):
        p.append("max_loss_r_policy must be 'L-10 default' or {override_r: <number>}")
    if frozen:
        _check_freeze(doc, v)
    return v


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def _block_sha256(block: Mapping[str, Any]) -> str:
    body = {k: v for k, v in block.items() if k != "block_sha256"}
    return hashlib.sha256(canonical_json(_jsonable(body)).encode("utf-8")).hexdigest()


def _check_freeze(doc: Mapping[str, Any], v: Validation) -> None:
    fz = doc["frozen"]
    if fz.get("block_sha256") != _block_sha256(fz):
        v.problems.append("the frozen block was edited (its own hash differs)")
    if fz.get("content_sha256") != content_sha256(doc):
        v.problems.append("the file was edited after it was frozen (content hash differs)")
    sid = _get(doc, "strategy.strategy_id")
    try:
        impl = implementation_sha256(str(sid))
    except ValueError:
        impl = None
    if fz.get("implementation_sha256") != impl:
        v.problems.append("the strategy implementation changed since the freeze")
    if fz.get("configurations") != v.configurations:
        v.problems.append("the frozen configurations differ from the search space")
    if _get(doc, "walk_forward.anchor") == "auto":
        v.problems.append(
            "a frozen file must fix walk_forward.anchor (with 'auto' the folds are not pinned)"
        )
    if not _is_hex64(fz.get("backtest_config_hash")):
        v.problems.append("the frozen block must pin the backtest config hash")
    elif doc.get("backtest_config_hash") not in (None, fz["backtest_config_hash"]):
        v.problems.append("backtest_config_hash differs from the frozen block's")
    if fz.get("engine_sha256") not in (None, engine_sha256()):
        v.notes.append(
            "the research engine changed since the freeze (informational: review the commits "
            "between the freeze and this run)"
        )
    if v.open_decisions:
        v.problems.append("a frozen file still has open owner decisions")


def _validate_selection(doc: Mapping[str, Any], v: Validation) -> None:
    p = v.problems
    parent: Mapping[str, Any] = doc["parent"] if isinstance(doc.get("parent"), Mapping) else {}
    if not parent.get("prereg_id") or not parent.get("content_sha256"):
        p.append("parent.prereg_id and parent.content_sha256 are required")
    sel: Mapping[str, Any] = doc["selected"] if isinstance(doc.get("selected"), Mapping) else {}
    if not sel.get("config_id") or not isinstance(sel.get("params"), Mapping):
        p.append("selected.config_id and selected.params are required")
    if "strategy_hash" in doc:
        p.append(
            "a selection record must not have a top-level strategy_hash: that field lets the "
            "holdout guard unlock (decision D-D); use selected.strategy_hash"
        )
    sh = sel.get("strategy_hash")
    if not isinstance(sh, str) or len(sh) < 8:
        p.append("selected.strategy_hash is required")
    wf: Mapping[str, Any] = (
        doc["walk_forward"] if isinstance(doc.get("walk_forward"), Mapping) else {}
    )
    for key in ("plan_id", "report_sha256", "verdict"):
        if not wf.get(key):
            p.append(f"walk_forward.{key} is required")


def validate_file(path: Path | str, *, parent_dir: Path | None = None) -> Validation:
    try:
        doc = load(path)
    except (OSError, yaml.YAMLError, PreregistrationInvalid) as exc:
        return Validation(path=str(path), kind=None, frozen=False, problems=[str(exc)])
    v = validate_document(doc, path=str(path))
    if v.kind == "selection" and v.ok:
        _check_selection_parent(doc, v, parent_dir or Path(path).parent)
    return v


def _check_selection_parent(doc: Mapping[str, Any], v: Validation, folder: Path) -> None:
    from tsclaude.strategies import build_strategy

    parent = doc["parent"]
    ppath = folder / f"{parent['prereg_id']}.yaml"
    if not ppath.is_file():
        v.problems.append(f"the parent preregistration {ppath.name} does not exist")
        return
    pdoc = load(ppath)
    pv = validate_document(pdoc, path=str(ppath))
    if not pv.ok or not pv.frozen:
        v.problems.append("the parent preregistration is not a valid frozen search file")
    if content_sha256(pdoc) != parent["content_sha256"]:
        v.problems.append("the parent preregistration changed since the selection")
    sel = doc["selected"]
    if sel["config_id"] not in pv.configurations:
        v.problems.append("the selected configuration is not in the parent's search space")
        return
    sid = _get(pdoc, "strategy.strategy_id")
    if build_strategy(str(sid), dict(sel["params"])).params_hash != sel["strategy_hash"]:
        v.problems.append("selected.strategy_hash is not the selected parameters' hash")
    elif pv.configurations[sel["config_id"]] != sel["strategy_hash"]:
        v.problems.append("selected.strategy_hash differs from the parent's frozen configuration")


# ---------------------------------------------------------------- freeze
def freeze(
    path: Path | str,
    *,
    now_ms: int,
    code: Mapping[str, Any],
    backtest_config_hash: str,
    backtest_risk: RiskConfig | None = None,
) -> dict[str, Any]:
    """Validate and freeze a search preregistration in place; refused with any problem or open
    owner decision. Returns the frozen block. Committing the file is the owner's act.

    The frozen block pins the walk-forward completely: the anchor must be a concrete month start
    (``auto`` would let a run pick its own first fold) and ``backtest_config_hash`` (costs,
    instruments, statistics settings of ``configs/backtest.yaml``) is recorded, so every run must
    use that exact config (re-review N2).

    ``backtest_risk`` is the risk config the pinned backtest config applies (``risk_policy: live``)
    or None (``allow_all``). With ``risk_compatibility: live-floor-kept-geometries-dropped`` the
    runs must apply the live policy (identical risk rules in backtests and Paper, D-E), and every
    configuration must be COMPATIBLE with **that** risk config."""
    doc = load(path)
    v = validate_document(doc, path=str(path))
    problems = list(v.problems)
    if _get(doc, "risk_compatibility") == KEEP_LIVE_FLOOR:
        if backtest_risk is None:
            problems.append(
                f"risk_compatibility {KEEP_LIVE_FLOOR} needs a backtest config with "
                "risk_policy live (the runs must apply the paper risk rules)"
            )
        else:
            problems += live_floor_problems(doc, risk=backtest_risk)
    if _get(doc, "walk_forward.anchor") == "auto":
        problems.append(
            "cannot freeze with walk_forward.anchor 'auto': fix the first train month "
            "(YYYY-MM-01) so the folds are pinned"
        )
    if not _is_hex64(backtest_config_hash):
        problems.append("cannot freeze without the backtest config hash")
    elif doc.get("backtest_config_hash") not in (None, backtest_config_hash):
        problems.append(
            f"the file fixes backtest_config_hash {doc['backtest_config_hash']}, the current "
            f"config is {backtest_config_hash}"
        )
    if v.kind != "search":
        problems.append("only a search preregistration is frozen (a selection record is made)")
    if v.frozen:
        problems.append("the file is already frozen")
    if v.open_decisions:
        problems += [f"cannot freeze: {d}" for d in v.open_decisions]
    if code.get("dirty") is not False or not code.get("commit") or code["commit"] == "unknown":
        problems.append("cannot freeze from unidentified code (unknown commit or a dirty tree)")
    if problems:
        raise PreregistrationInvalid(problems)
    block = {
        "at": datetime.fromtimestamp(now_ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "content_sha256": content_sha256(doc),
        "implementation_sha256": implementation_sha256(str(_get(doc, "strategy.strategy_id"))),
        "configurations": v.configurations,
        "code_commit": code["commit"],
        "backtest_config_hash": backtest_config_hash,
        # informational: every module the research engine runs (not enforced, see the docstring)
        "engine_sha256": engine_sha256(),
    }
    block["block_sha256"] = _block_sha256(block)
    doc["frozen"] = block
    Path(path).write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return block


# ---------------------------------------------------------------- walk-forward conformance
def walkforward_conformance(
    doc: Mapping[str, Any],
    *,
    strategy_id: str,
    grid: Mapping[str, Mapping[str, Any]],
    symbols: Sequence[str],
    initial_train_months: int,
    test_months: int,
    embargo_minutes: int,
    n_min: int,
    min_oos_coverage: float,
    anchor: str,
    dataset_ids: Sequence[str] | None,
    final_selection: bool,
    folds: Sequence[Mapping[str, Any]] = (),
    config_hash: str | None = None,
    path: str = "<memory>",
) -> list[str]:
    """Why a walk-forward does not run exactly what a frozen search preregistration fixed.
    ``folds`` are the run's folds as ``Fold.to_dict()``; they must equal the folds the
    preregistration's walk-forward plan produces (anchor, end, lengths, embargo)."""
    v = validate_document(doc, path=path)
    problems = [f"preregistration: {x}" for x in v.problems]
    if v.kind != "search" or not v.frozen:
        problems.append("the preregistration is not a frozen search preregistration")
        return problems
    wf = doc["walk_forward"]
    market = _get(doc, "universe.market")
    expected_symbols = [f"{market}:{s}" for s in _get(doc, "universe.symbols")]
    pins = _get(doc, "data.dataset_ids")
    checks = [
        ("strategy", strategy_id, _get(doc, "strategy.strategy_id")),
        ("grid", sorted(grid), sorted(v.configurations)),
        ("symbols", list(symbols), expected_symbols),
        ("initial_train_months", initial_train_months, wf["initial_train_months"]),
        ("test_months", test_months, wf["test_months"]),
        ("embargo_minutes", embargo_minutes, wf["embargo_minutes"]),
        ("n_min", n_min, wf["n_min"]),
        ("min_oos_coverage", float(min_oos_coverage), float(wf["min_oos_coverage"])),
        ("dataset_ids", None if dataset_ids is None else sorted(dataset_ids),
         None if pins is None else sorted(pins)),
        ("final_selection", final_selection, True),
    ]  # fmt: skip
    # a frozen file has a concrete anchor (validation refuses 'auto' once frozen)
    checks.append(("anchor", anchor, f"{wf['anchor']}T00:00:00Z"))
    for name, got, want in checks:
        if got != want:
            problems.append(f"{name} is {got!r}, the preregistration fixes {want!r}")
    # the expected folds come from the FILE's anchor, never from the run (re-review R1)
    if not folds:
        problems.append("no folds given: the run's folds cannot be checked against the plan")
    elif _month_start(wf["anchor"]):  # otherwise validation already reported the anchor
        from tsclaude.research.walkforward import plan_folds

        first_train = folds[0]["train"][0]
        start = datetime.strptime(str(wf["anchor"]), "%Y-%m-%d").replace(tzinfo=UTC)
        end = datetime.strptime(str(wf["end"]), "%Y-%m-%d").replace(tzinfo=UTC)
        expected = [
            f.to_dict()
            for f in plan_folds(
                int(start.timestamp() * 1000), int(end.timestamp() * 1000),
                initial_train_months=int(wf["initial_train_months"]),
                test_months=int(wf["test_months"]),
                embargo_ms=int(wf["embargo_minutes"]) * 60_000,
            )
        ]  # fmt: skip
        if list(folds) != expected:
            problems.append(
                f"the folds differ from the preregistered plan (anchor {first_train}, end "
                f"{wf['end']}): {len(folds)} run folds vs {len(expected)} planned"
            )
    if isinstance(doc.get("max_loss_r_policy"), Mapping):
        problems.append("a max_loss_r override is not supported by the walk-forward yet")
    fixed_cfg = doc["frozen"].get("backtest_config_hash")
    if config_hash is None or config_hash != fixed_cfg:
        problems.append(f"backtest config hash {config_hash} differs from the frozen {fixed_cfg}")
    budget = doc["trial_budget"]
    if len(grid) > int(budget["max_configurations"]):
        problems.append("the grid exceeds trial_budget.max_configurations")
    return problems


# ---------------------------------------------------------------- selection record
def make_selection_record(
    search_path: Path | str, report_path: Path | str, *, prereg_id: str
) -> dict[str, Any]:
    """A selection record for the candidate of a walk-forward report of a frozen search
    preregistration (refused unless every link verifies). ``holdout_procedure`` stays PENDING
    (D-D); the caller writes and the owner commits the file."""
    sdoc = load(search_path)
    sv = validate_document(sdoc, path=str(search_path))
    problems = list(sv.problems)
    if sv.kind != "search" or not sv.frozen:
        problems.append("the search preregistration is not frozen")
    raw = Path(report_path).read_bytes()
    report = json.loads(raw)
    final = report.get("final_selection") or {}
    if report.get("prereg_id") != sdoc.get("prereg_id"):
        problems.append("the report is not a run of this preregistration")
    if (report.get("oos") or {}).get("verdict") != "OOS_MARGIN_CERTIFIED_DEVELOPMENT_ONLY":
        problems.append("the report's stitched verdict is not positive")
    if final.get("status") != "CANDIDATE_PENDING_GOVERNANCE":
        problems.append("the report has no final-selection candidate")
    if final.get("rule") != sdoc.get("selection_rule"):
        problems.append("the report's selection rule is not the preregistered one")
    checks = report.get("checks")
    if not isinstance(checks, dict) or not checks or not all(checks.values()):
        problems.append("the report's validity checks are missing or did not all pass")
    if problems:
        raise PreregistrationInvalid(problems)
    record = {
        "schema": SCHEMA,
        "prereg_id": prereg_id,
        "kind": "selection",
        "parent": {"prereg_id": sdoc["prereg_id"], "content_sha256": content_sha256(sdoc)},
        "selected": {
            "config_id": final["selected"],
            "params": dict(final["params"]),
            "strategy_hash": final["strategy_hash"],
        },
        "walk_forward": {
            "plan_id": (report.get("plan") or {}).get("plan_id"),
            "report_sha256": hashlib.sha256(raw).hexdigest(),
            "verdict": report["oos"]["verdict"],
        },
        "trial_budget": {"counting": _get(sdoc, "trial_budget.counting")},
        "holdout_procedure": PENDING,
        "risk_compatibility": sdoc.get("risk_compatibility"),
    }
    v = validate_document(record)
    if v.ok:  # the same links a stored record is checked against
        _check_selection_parent(record, v, Path(search_path).parent)
    if not v.ok:
        raise PreregistrationInvalid(v.problems)
    return record


__all__ = [
    "DRAFT_OPEN_ITEM",
    "KEEP_LIVE_FLOOR",
    "OBJECTIVE",
    "PENDING",
    "POLICY_FIELDS",
    "POLICY_VALUES",
    "REQUIRED_REJECTION",
    "SCHEMA",
    "SEARCH_KEYS",
    "SEGMENTS",
    "SELECTION_KEYS",
    "PreregistrationInvalid",
    "Validation",
    "configurations_of",
    "content_sha256",
    "engine_sha256",
    "freeze",
    "holding_problems",
    "implementation_files",
    "implementation_sha256",
    "live_floor_problems",
    "load",
    "make_selection_record",
    "max_holding_ms_of",
    "validate_document",
    "validate_file",
    "walkforward_conformance",
]
