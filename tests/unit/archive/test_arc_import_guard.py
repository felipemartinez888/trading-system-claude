"""CI guard (L-07 b, review R4): an ALLOWLIST of what code outside `src/tsclaude/store/` may use.

Scanned: every Python file under `src/` and `scripts/` (tests are excluded on purpose: they
exercise the internals). Outside `store/`:

- `tsclaude.store.eventstore` / `livebars`: anywhere;
- `tsclaude.store.lake_reader`: only `LakeReader`, `ReadResult`;
- `tsclaude.store.holdout`: only the public read API (`PUBLIC_HOLDOUT`);
- `tsclaude.store.lake_writer` / `lake_admin`: only `marketdata/archive/` and
  `marketdata/datacli.py`;
- `tsclaude.store.holdout_cli`: only `src/tsclaude/cli.py` (registers the owner command);
- everything else in `tsclaude.store` (lake, parquet_codec, ...), `pyarrow`, `duckdb`: never;
- plain `import tsclaude.store.<x>` (module objects) only for eventstore/livebars;
- unlock internals (`FORBIDDEN_NAMES`) never appear as names, attributes or string constants;
- `importlib.import_module` / `__import__` with a non-constant argument are refused.

What this guard cannot see -- deliberate raw file access to `data/lake_sealed/`, reflection
through computed attribute names, editing `state.db` -- is a **review-only control**.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ROOTS = [REPO / "src", REPO / "scripts"]
EXCLUDED = [REPO / "tests"]
STORE = REPO / "src" / "tsclaude" / "store"

ANYWHERE = {"tsclaude.store.eventstore", "tsclaude.store.livebars"}
NAMES_ONLY = {
    "tsclaude.store.lake_reader": {"LakeReader", "ReadResult"},
    "tsclaude.store.holdout": {
        "HoldoutGuard",
        "EventStoreUnlocks",
        "SealedUnlocks",
        "Purpose",
        "HoldoutError",
        "HoldoutLocked",
        "HoldoutAlreadyUnlocked",
        "PreregistrationError",
        "HOLDOUT_START_MS",
        "HOLDOUT_END_MS",
        "touches_holdout",
    },
}
WRITER_MODULES = {"tsclaude.store.lake_writer", "tsclaude.store.lake_admin"}
WRITER_PLACES = ("src/tsclaude/marketdata/archive/", "src/tsclaude/marketdata/datacli.py")
CLI_ONLY = {"tsclaude.store.holdout_cli": ("src/tsclaude/cli.py",)}
NEVER = ("pyarrow", "duckdb")
FORBIDDEN_NAMES = {
    "HOLDOUT_UNLOCKED",
    "holdout_unlocked",
    "UnlockAuthorization",
    "PreregEvidence",
    "holdout_cli",
    "holdout_unlock_claims",
    "read_sealed_part",
    "_read_rows",
    "_lake",
    "ISSUER",
    "_APPROVAL_ISSUER",
}
NAME_EXCEPTIONS = {  # the enum member's own definition in the Lead-owned contracts
    ("src/tsclaude/contracts/events.py", "HOLDOUT_UNLOCKED"),
    ("src/tsclaude/contracts/events.py", "holdout_unlocked"),
}
IMPORT_FUNCS = {"import_module", "__import__"}


def _module_rule(module: str, rel: str, names: list[str] | None) -> str | None:
    """Why importing `module` (with `names`, if a from-import) at `rel` is refused."""
    root = module.split(".")[0]
    if root in NEVER:
        return f"{module} is only for store/"
    if module == "tsclaude.store" and names is not None:
        for n in names:
            why = _module_rule(f"tsclaude.store.{n}", rel, None)
            if why:
                return why
        return None
    if not (module == "tsclaude.store" or module.startswith("tsclaude.store.")):
        return None
    if module in ANYWHERE:
        return None
    if module in NAMES_ONLY:
        if names is None:
            return f"import names from {module}, not the module"
        bad = [n for n in names if n not in NAMES_ONLY[module]]
        return f"{module}: {bad} are not public" if bad else None
    if module in WRITER_MODULES:
        return None if rel.startswith(WRITER_PLACES) else f"{module} only for importers"
    if module in CLI_ONLY:
        return None if rel in CLI_ONLY[module] else f"{module} only for the CLI entry point"
    return f"{module} is internal to store/"


def violations(path: Path, rel: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []

    def flag(node: ast.AST, why: str) -> None:
        out.append(f"{rel}:{getattr(node, 'lineno', '?')}: {why}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("tsclaude.store.") and a.name not in ANYWHERE:
                    flag(node, f"plain import of {a.name}")
                elif why := _module_rule(a.name, rel, None):
                    flag(node, why)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [a.name for a in node.names]
            if why := _module_rule(node.module, rel, names):
                flag(node, why)
            for a in node.names:
                if a.name in FORBIDDEN_NAMES or (a.asname or "") in FORBIDDEN_NAMES:
                    flag(node, f"imports {a.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if fname in IMPORT_FUNCS:
                arg = node.args[0] if node.args else None
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    flag(node, f"{fname}() with a non-constant module name")
                elif why := _module_rule(arg.value, rel, None):
                    flag(node, why)
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            if (rel, node.id) not in NAME_EXCEPTIONS:
                flag(node, f"uses {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            flag(node, f"uses .{node.attr}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in FORBIDDEN_NAMES
            and (rel, node.value) not in NAME_EXCEPTIONS
        ):
            flag(node, f"string {node.value!r}")
    return out


def _files() -> list[tuple[Path, str]]:
    out = []
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")):
            if STORE in path.parents or any(e in path.parents for e in EXCLUDED):
                continue
            out.append((path, path.relative_to(REPO).as_posix()))
    return out


def test_allowlist_holds_for_all_python_outside_store() -> None:
    files = _files()
    assert any(rel.startswith("scripts/") for _, rel in files)  # scripts are scanned too
    offenders = [v for path, rel in files for v in violations(path, rel)]
    assert offenders == [], "\n".join(offenders)


BYPASSES = [
    "from tsclaude.store.parquet_codec import ParquetCodec",
    "from tsclaude.store.lake_reader import Lake",
    "from tsclaude.store.lake_writer import LakeWriter\nLakeWriter('d')._lake",
    "from tsclaude.store import holdout_cli",
    "from tsclaude.contracts.events import EventType\nEventType.HOLDOUT_UNLOCKED",
    "import importlib\nimportlib.import_module('duck' + 'db')",
    "__import__(''.join(['py', 'arrow']))",
    "import pyarrow.parquet",
    "from tsclaude.store.lake import Lake",
    "import tsclaude.store.holdout",
    "from tsclaude.store.holdout import UnlockAuthorization",
    "from tsclaude.store.holdout import PreregEvidence as P",
    "x.read_sealed_part(p)",
    "getattr(m, 'holdout_unlocked')",
    "import importlib\nimportlib.import_module('tsclaude.store.lake')",
]


def test_guard_catches_every_reviewed_bypass(tmp_path: Path) -> None:
    for i, code in enumerate(BYPASSES):
        p = tmp_path / f"b{i}.py"
        p.write_text(code + "\n", encoding="utf-8")
        assert violations(p, "src/tsclaude/research/x.py"), code


def test_guard_allows_the_public_read_api(tmp_path: Path) -> None:
    p = tmp_path / "ok.py"
    p.write_text(
        "from tsclaude.store.lake_reader import LakeReader\n"
        "from tsclaude.store.holdout import HoldoutGuard, EventStoreUnlocks, Purpose\n"
        "from tsclaude.store.eventstore import EventStore\n",
        encoding="utf-8",
    )
    assert violations(p, "src/tsclaude/research/x.py") == []
    w = tmp_path / "w.py"
    w.write_text("from tsclaude.store.lake_writer import LakeWriter\n", encoding="utf-8")
    assert violations(w, "src/tsclaude/marketdata/archive/importer.py") == []
    assert violations(w, "src/tsclaude/research/x.py")
