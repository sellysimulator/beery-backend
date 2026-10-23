"""``alembic upgrade head``, its downgrade, and the drift detector.

Covers ``13-db-models-and-migrations.md §5`` criteria 1 to 4 and §6 failure
mode 3.  Each test gets its own freshly created, completely empty database
inside the session's container, so an upgrade/downgrade cycle here cannot
disturb the migrated schema ``test_schema.py`` reads.

The ``alembic`` CLI is driven as a real subprocess whose ``DB_*``
environment names the container -- ``alembic/env.py`` builds its URL from
``settings.db_url``, and an environment variable beats the ``.env`` entry
that points at the owner's live managed MySQL.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from .conftest import ALEMBIC_INI, ALEMBIC_VERSIONS, BACKEND_ROOT, EXPECTED_TABLES

pytestmark = pytest.mark.dbschema

AlembicRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def _tables(url: str) -> set[str]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE()"
                )
            )
            return {row[0] for row in rows}
    finally:
        engine.dispose()


def _version_rows(url: str) -> list[str]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            return list(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars()
            )
    finally:
        engine.dispose()


# --- criterion 1 -----------------------------------------------------------


def test_upgrade_head_on_an_empty_database_creates_exactly_nine_tables(
    blank_database: str, alembic: AlembicRunner
) -> None:
    """AC 1 -- on an empty MySQL, ``alembic upgrade head`` creates the nine
    tables of §2.1 to §2.9 plus ``alembic_version``, and nothing else."""
    assert _tables(blank_database) == set()

    alembic(blank_database, "upgrade", "head")

    assert _tables(blank_database) == EXPECTED_TABLES | {"alembic_version"}
    assert len(_version_rows(blank_database)) == 1


def test_the_revision_chain_is_linear_and_starts_at_the_initial_schema() -> None:
    """§4.1 -- a single linear chain rooted at ``0001_initial_schema``.

    This deliberately does **not** assert the exact list of revision files.
    It used to (``scripts == ["0001_initial_schema.py"]``), which made it a
    criterion that expires the instant any later section changes the schema
    -- and it did: section 14 folded a column into ``0001`` rather than add
    ``0002`` purely to keep this green, which is the tail wagging the dog.
    A migration chain grows by design; what must stay true is that it is
    one chain with one head and no gaps, so that is what is asserted here.
    """
    scripts = sorted(
        path.name
        for path in ALEMBIC_VERSIONS.glob("*.py")
        if path.name != "__init__.py"
    )
    assert scripts, "there must be at least the initial revision"
    assert scripts[0] == "0001_initial_schema.py"

    numbers = [int(name[:4]) for name in scripts]
    assert numbers == list(
        range(1, len(numbers) + 1)
    ), f"revision numbers must be contiguous from 0001, got {scripts}"

    # Exactly one head: every `down_revision` but the first names its
    # predecessor, and no two revisions claim the same parent.
    parents = []
    for name in scripts:
        text_of = (ALEMBIC_VERSIONS / name).read_text()
        match = re.search(r"^down_revision[^=]*=\s*(.+)$", text_of, re.MULTILINE)
        assert match, f"{name} declares no down_revision"
        parents.append(match.group(1).strip().strip("\"'"))
    assert parents[0] in {"None", "none"}, "0001 must be the root"
    assert len(set(parents)) == len(parents), f"branched history: {parents}"


# --- criterion 2 -----------------------------------------------------------


def test_downgrade_base_removes_all_nine_tables(
    blank_database: str, alembic: AlembicRunner
) -> None:
    """AC 2 -- a real ``downgrade()``, dropping in reverse dependency order,
    with no foreign-key errors.  A revision whose downgrade is ``pass``
    cannot be tested (§4.1)."""
    alembic(blank_database, "upgrade", "head")
    completed = alembic(blank_database, "downgrade", "base")

    assert "foreign key" not in completed.stderr.lower()
    remaining = _tables(blank_database)
    assert remaining & EXPECTED_TABLES == set()
    # `alembic_version` survives an unwind; it must be empty.
    assert _version_rows(blank_database) == []


# --- criterion 3 -----------------------------------------------------------


def test_upgrade_downgrade_upgrade_round_trip(
    blank_database: str, alembic: AlembicRunner
) -> None:
    """AC 3 -- head, base, head again."""
    alembic(blank_database, "upgrade", "head")
    first = _tables(blank_database)
    alembic(blank_database, "downgrade", "base")
    alembic(blank_database, "upgrade", "head")

    assert _tables(blank_database) == first
    assert len(_version_rows(blank_database)) == 1


# --- criterion 4, failure mode 3 ------------------------------------------


def _ini_with_extra_version_path(destination: Path, extra: Path) -> Path:
    """A copy of ``alembic.ini`` that may also write revisions into ``extra``.

    Alembic refuses a ``--version-path`` that is not in ``version_locations``,
    and the drift check must not drop a file into the real
    ``alembic/versions/``.  ``script_location`` is made absolute because
    ``%(here)s`` would otherwise resolve to the copy's own directory.
    """
    source = ALEMBIC_INI.read_text()
    patched = re.sub(
        r"^script_location\s*=.*$",
        f"script_location = {BACKEND_ROOT / 'alembic'}\n"
        f"version_locations = {ALEMBIC_VERSIONS}{os.pathsep}{extra}",
        source,
        count=1,
        flags=re.MULTILINE,
    )
    assert "version_locations = " in patched
    ini = destination / "alembic.ini"
    ini.write_text(patched)
    return ini


def test_autogenerate_against_head_produces_an_empty_revision(
    blank_database: str, alembic: AlembicRunner, tmp_path: Path
) -> None:
    """AC 4 and failure mode 3 -- the models and the migration agree.

    This is the drift detector: anything the models declare and the migration
    does not (or the other way round) shows up here as an ``op.`` call in an
    otherwise empty revision.
    """
    alembic(blank_database, "upgrade", "head")

    output = tmp_path / "versions"
    output.mkdir()
    ini = _ini_with_extra_version_path(tmp_path, output)
    alembic(
        blank_database,
        "revision",
        "--autogenerate",
        "-m",
        "drift-check",
        "--version-path",
        str(output),
        ini=ini,
    )

    generated = list(output.glob("*.py"))
    assert len(generated) == 1, f"expected one revision, got {generated}"
    script = generated[0].read_text()
    operations = _operations(script, "upgrade") + _operations(script, "downgrade")
    assert operations == [], (
        "autogenerate is not empty -- 0001_initial_schema and the metadata "
        "`alembic/env.py` compares it against disagree.\n"
        "If every operation below drops one of the nine tables, the "
        "metadata was *empty*: `alembic/env.py` uses `app.db.base.Base"
        ".metadata`, which stays empty until something imports the model "
        "modules.\n" + "\n".join(operations)
    )


def _operations(script: str, function: str) -> list[str]:
    body = re.search(
        rf"^def {function}\(\).*?:\n(.*?)(?=^def |\Z)", script, re.DOTALL | re.MULTILINE
    )
    assert body is not None, f"the generated revision has no {function}()"
    return [
        line.strip()
        for line in body.group(1).splitlines()
        if line.strip().startswith("op.")
    ]


# --- 0004: the host is recorded on its participant row only ---------------


def test_0004_moves_the_host_onto_its_row_and_the_downgrade_restores_it(
    blank_database: str, alembic: AlembicRunner
) -> None:
    """A game stored before 0004 whose `HOST` row carries no identity keeps
    its registered host: the upgrade copies `games.host_user_id` onto the row
    before dropping the column, and the downgrade reads the column back."""
    alembic(blank_database, "upgrade", "0003")
    engine = create_engine(blank_database, future=True)
    try:
        with engine.begin() as connection:
            user_id = connection.execute(
                text("INSERT INTO users (firebase_uid) VALUES ('uid-0004-host')")
            ).lastrowid
            game_id = connection.execute(
                text(
                    "INSERT INTO games (room_code, host_user_id, host_display_name,"
                    " rng_seed, duration_weeks, weeks_played, started_at,"
                    " finished_at, chain_total_cost, public_id)"
                    " VALUES ('R0004', :user_id, 'Prof', 1, 8, 8,"
                    " '2026-01-01 08:00:00', '2026-01-01 10:00:00', 0, 'p0004')"
                ),
                {"user_id": user_id},
            ).lastrowid
            connection.execute(
                text(
                    "INSERT INTO participants"
                    " (game_id, alias, display_name, participant_type)"
                    " VALUES (:game_id, 'HOST', 'Prof', 'HOST')"
                ),
                {"game_id": game_id},
            )

        alembic(blank_database, "upgrade", "head")
        with engine.connect() as connection:
            host = connection.execute(
                text("SELECT user_id FROM participants WHERE game_id = :id"),
                {"id": game_id},
            ).scalar_one()
            games_columns = connection.execute(
                text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'games'"
                )
            ).scalars()
            assert "host_user_id" not in set(games_columns)
        assert host == user_id

        alembic(blank_database, "downgrade", "0003")
        with engine.connect() as connection:
            restored = connection.execute(
                text("SELECT host_user_id FROM games WHERE id = :id"),
                {"id": game_id},
            ).scalar_one()
        assert restored == user_id
    finally:
        engine.dispose()
