"""No module under ``app/core`` draws from the global ``random`` module.

``00-conventions.md §4`` and **D11**: every random draw comes from a seeded
``random.Random`` derived from the game's seed, so the same config, seed and
order history replay byte-identically. A single ``random.randint`` anywhere
in the core reads the process-wide generator instead, and the replay
guarantee is gone without any test that compares two runs noticing, because
both runs in one process share that generator. ``test_demand.py`` and
``test_bot.py`` guard their own modules; this one scans the whole package,
so a new module cannot slip past.

Constructing ``random.Random(...)`` is the one permitted use.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2] / "app" / "core"


def global_random_uses(source: str) -> list[str]:
    """Every call on the global ``random`` module, and every name imported
    from it, other than ``Random`` itself."""
    tree = ast.parse(source)
    module_names = {"random"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.update(
                alias.asname
                for alias in node.names
                if alias.name == "random" and alias.asname
            )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "random":
            offenders.extend(
                f"from random import {alias.name}"
                for alias in node.names
                if alias.name != "Random"
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in module_names
            and node.func.attr != "Random"
        ):
            offenders.append(ast.unparse(node))
    return offenders


CORE_MODULES = sorted(CORE.rglob("*.py"))


@pytest.mark.parametrize(
    "path", CORE_MODULES, ids=[str(p.relative_to(CORE)) for p in CORE_MODULES]
)
def test_core_module_never_uses_the_global_random_module(path: Path) -> None:
    offenders = global_random_uses(path.read_text(encoding="utf-8"))
    assert offenders == [], f"{path.relative_to(CORE)} uses global random: {offenders}"


def test_the_scan_covers_the_modules_that_draw_random_numbers() -> None:
    names = {path.name for path in CORE_MODULES}
    assert {"demand.py", "game_engine.py", "bot.py"} <= names


def test_the_scan_would_catch_a_violation() -> None:
    """Guard the guard: a green scan has to mean something."""
    source = (
        "import random\n"
        "import random as rnd\n"
        "from random import choice, Random\n"
        "rng = random.Random('7:demand')\n"
        "a = random.randint(1, 6)\n"
        "random.shuffle([1, 2])\n"
        "b = rnd.random()\n"
    )
    assert global_random_uses(source) == [
        "from random import choice",
        "random.randint(1, 6)",
        "random.shuffle([1, 2])",
        "rnd.random()",
    ]
