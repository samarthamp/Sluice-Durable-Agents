"""Guards for the package's structure, so the layering does not quietly erode.

* Each layer imports only from the layers below it (lazy imports included).
* Nothing references the pre-refactor entry points (``demo.py``, ``run_services.py``...).
* ``requirements.txt`` and the ``pyproject.toml`` extras list the same dependencies.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

import sluice
from sluice.dashboard import STATIC_DIR

REPO = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = REPO / "src" / "sluice"

# What each top-level part of the package may import from (besides itself).
ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "_spawn": set(),
    "world": {"core"},
    "ingest": {"core"},
    "observability": {"core"},
    "verification": {"core", "world"},
    "scenarios": {"core", "world", "ingest", "observability", "verification"},
    "dashboard": {"core", "world", "observability", "verification", "scenarios", "_spawn"},
    "cli": {"core", "world", "ingest", "observability", "verification", "scenarios",
            "dashboard", "_spawn", "<root>"},
}


def _part_of(path: pathlib.Path) -> str:
    rel = path.relative_to(PACKAGE)
    return rel.parts[0].removesuffix(".py") if len(rel.parts) > 1 or rel.stem != "__init__" \
        else "<root>"


def _imported_parts(path: pathlib.Path) -> set[str]:
    """Top-level package parts this module imports, resolving relative imports."""
    module_parts = list(path.relative_to(PACKAGE.parent).with_suffix("").parts)
    if module_parts[-1] == "__init__":
        module_parts.pop()
    package_parts = module_parts if path.name == "__init__.py" else module_parts[:-1]

    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - (node.level - 1)]
                target = base + (node.module.split(".") if node.module else [])
                if not node.module:
                    # `from .. import x`: x is a submodule, or an attribute of the package.
                    for alias in node.names:
                        is_module = (PACKAGE.parent.joinpath(*target, alias.name).is_dir()
                                     or PACKAGE.parent.joinpath(*target, f"{alias.name}.py")
                                     .exists())
                        found.add(_part(target + [alias.name] if is_module else target))
                    continue
            else:
                target = (node.module or "").split(".")
            found.add(_part(target))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(_part(alias.name.split(".")))
    found.discard(None)
    return found


def _part(dotted: list[str]) -> str | None:
    if not dotted or dotted[0] != "sluice":
        return None
    return dotted[1] if len(dotted) > 1 else "<root>"


MODULES = sorted(p for p in PACKAGE.rglob("*.py") if p.name != "__main__.py")


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(PACKAGE)))
def test_layers_only_import_downwards(path):
    part = _part_of(path)
    if part == "<root>":
        return  # the package root re-exports the public API from every layer
    illegal = _imported_parts(path) - ALLOWED[part] - {part}
    assert not illegal, f"{path.relative_to(REPO)} ({part}) imports from {sorted(illegal)}"


def test_the_kernel_is_standard_library_only():
    third_party = {"fastapi", "uvicorn", "pydantic", "httpx", "redis", "starlette"}
    for path in (PACKAGE / "core").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                 for a in n.names}
        names |= {(n.module or "").split(".")[0] for n in ast.walk(tree)
                  if isinstance(n, ast.ImportFrom) and not n.level}
        assert not names & third_party, f"{path.name} imports {names & third_party}"


# ------------------------------------------------------ stale references


OLD_ENTRY_POINTS = re.compile(
    r"\b(run_services|run_orchestrator|run_producer|run_dashboard)\.py\b"
    r"|(?<![\w/.-])(demo|smoke)\.py\b"
    r"|sluice/(engine|journal|types|tools|world|http_world|services|checker|sweep"
    r"|bench|view|audit|dashboard|scenarios|failover|ingest)\.py\b"
)


@pytest.mark.parametrize("folder", ["src", "scripts"])
def test_nothing_points_at_the_pre_refactor_entry_points(folder):
    hits = []
    for path in (REPO / folder).rglob("*"):
        if path.is_dir() or path.suffix not in (".py", ".html", ".sh", ".bat", ".toml"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if OLD_ENTRY_POINTS.search(line):
                hits.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not hits, "\n".join(hits)


OLD_NAME = "palim" + "psest"   # split, so this file does not match itself


def test_the_old_project_name_is_gone():
    """The project was renamed to Sluice; nothing in the repository uses the old name."""
    paths = [REPO / name for name in ("README.md", "pyproject.toml", "requirements.txt",
                                      "docker-compose.yml", ".gitignore", ".gitattributes")]
    for folder in ("src", "tests", "scripts", "docs"):
        paths += [p for p in (REPO / folder).rglob("*") if p.is_file()
                  and "__pycache__" not in p.parts]
    hits = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        if OLD_NAME in text.lower():
            hits.append(str(path.relative_to(REPO)))
    assert not hits, f"old project name still in: {hits}"


# ------------------------------------------------------------- packaging


def _toml():
    """tomllib on Python 3.11+, its backport tomli on 3.10 (a dev dependency there)."""
    try:
        import tomllib
    except ModuleNotFoundError:
        return pytest.importorskip("tomli")
    return tomllib


def _requirements() -> set[str]:
    lines = (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return {ln.split("#")[0].strip() for ln in lines if ln.split("#")[0].strip()}


def test_requirements_txt_matches_the_pyproject_extras():
    project = _toml().loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    declared = {
        req for name in ("http", "redis", "dev") for req in extras[name]
        if not req.startswith("sluice")
    }
    assert _requirements() == declared
    assert project["dependencies"] == [], "the core must stay standard library only"


def test_version_and_entry_point_agree():
    project = _toml().loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == sluice.__version__
    assert project["scripts"] == {"sluice": "sluice.cli:main"}


def test_the_dashboard_page_ships_inside_the_package():
    page = pathlib.Path(STATIC_DIR) / "dashboard.html"
    assert page.is_file()
    assert page.read_text(encoding="utf-8").lstrip().lower().startswith("<!doctype html")
