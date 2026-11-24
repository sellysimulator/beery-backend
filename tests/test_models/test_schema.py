"""The live schema, asserted against a real PostgreSQL 18.

Upgrade, downgrade, autogenerate drift, the DDL snapshot and the
models-match check are ``test_migration.py``'s.

Everything here reads the **database**: ``information_schema``, raw SQL,
real inserts and real deletes.  Nothing imports ``app/models/game.py`` --
a test that asserted the model classes agree with themselves would pass
against a migration that never ran.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DatabaseError, IntegrityError

from app.core.enums import DemandKind, Role, RoleAssignmentMode

from .conftest import (
    EXPECTED_TABLES,
    FORBIDDEN_COLUMN_NAMES,
    TIMESTAMP_TABLES,
    DockerUnavailableWarning,
    columns,
    docker_unavailable_reason,
    indexes,
    insert_game,
    insert_row,
    insert_user,
    is_autoincrement,
    is_reserved_word_error,
    require_docker,
    table_names,
    unique_key_columns,
)

pytestmark = pytest.mark.dbschema

#: Every money column, typed ``NUMERIC(12, 2)``.
MONEY_COLUMNS = (
    ("games", "chain_total_cost"),
    ("role_configs", "holding_cost_per_unit_week"),
    ("role_configs", "backlog_cost_per_unit_week"),
    ("role_configs", "fixed_order_cost"),
    ("role_configs", "unit_purchase_cost"),
    ("role_configs", "starting_capital"),
    ("weeks", "holding_cost"),
    ("weeks", "backlog_cost"),
    ("weeks", "fixed_order_cost"),
    ("weeks", "purchase_cost"),
    ("weeks", "total_week_cost"),
    ("weeks", "cumulative_cost"),
    ("user_stats", "total_cost"),
    ("user_stats", "avg_cost_per_week"),
)

#: The four ``BotConfig`` fields.
BOT_COLUMNS = ("theta", "alpha", "beta", "target_stock_multiplier")


def _parent_columns(db: Connection, table: str) -> dict[str, Any]:
    """Foreign keys a child row needs before it can exist at all."""
    if table in {"game_configs", "role_configs", "demand_configs", "participants"}:
        return {"game_id": insert_game(db)}
    if table == "user_stats":
        return {"user_id": insert_user(db)}
    return {}


# --- the tables ------------------------------------------------------------


def test_migration_creates_exactly_the_nine_tables(db: Connection) -> None:
    """The nine tables, ``alembic_version``, nothing else.  ``sessions``,
    ``events``, ``orders`` and ``pipeline_slots`` are deliberately absent."""
    found = table_names(db)
    assert found == EXPECTED_TABLES | {"alembic_version"}
    for absent in ("sessions", "events", "orders", "pipeline_slots"):
        assert absent not in found


# --- timestamp server defaults ---------------------------------------------


@pytest.mark.parametrize("table", TIMESTAMP_TABLES)
def test_timestamp_columns_have_a_server_default(db: Connection, table: str) -> None:
    """``created_at``/``updated_at`` carry a server-side default.

    End-of-game persistence writes with batched raw inserts that bypass ORM
    defaults, so a Python-side ``default=`` is not enough  ``[HARD-WON]``.
    """
    spec = columns(db, table)
    for name in ("created_at", "updated_at"):
        assert name in spec, f"{table}.{name} is missing"
        default = spec[name]["COLUMN_DEFAULT"]
        assert default is not None, f"{table}.{name} has no server_default"
        # PostgreSQL reports `server_default=func.now()` as `now()`, and a
        # `CURRENT_TIMESTAMP` literal as `CURRENT_TIMESTAMP`.
        rendered = str(default).upper()
        assert (
            "CURRENT_TIMESTAMP" in rendered or "NOW()" in rendered
        ), f"{table}.{name} defaults to {default!r}, which is not a clock"


@pytest.mark.parametrize("table", TIMESTAMP_TABLES)
def test_raw_insert_naming_neither_timestamp_succeeds(
    db: Connection, table: str
) -> None:
    """The insert end-of-game persistence actually performs.

    Without ``server_default`` this raises PostgreSQL's ``null value in column
    "created_at" ... violates not-null constraint``, persistence swallows it,
    and every completed game is silently dropped  ``[HARD-WON]``.
    """
    parents = _parent_columns(db, table)
    row_id = insert_row(db, table, **parents)
    # `user_stats` is keyed by its foreign key, not by an autoincrement id.
    if table == "user_stats":
        key, ident = "user_id", parents["user_id"]
    else:
        key, ident = "id", row_id
    stored = db.execute(
        text(f"SELECT created_at, updated_at FROM {table} WHERE {key} = :id"),
        {"id": ident},
    ).one()
    assert stored.created_at is not None
    assert stored.updated_at is not None


def test_no_fact_column_has_a_database_default(db: Connection) -> None:
    """Only ``created_at``/``updated_at`` (``now()``) and the serial ids carry
    a default in the migrated schema: every fact is written by the app."""
    offenders = [
        f"{table}.{name} DEFAULT {spec['COLUMN_DEFAULT']}"
        for table in sorted(EXPECTED_TABLES)
        for name, spec in columns(db, table).items()
        if spec["COLUMN_DEFAULT"] is not None
        and not is_autoincrement(spec)
        and name not in {"created_at", "updated_at"}
    ]
    assert offenders == []


def test_timestamp_free_tables_really_have_none(db: Connection) -> None:
    """The bulk tables carry no timestamp columns."""
    for table in ("demand_series", "weeks"):
        spec = columns(db, table)
        assert "created_at" not in spec
        assert "updated_at" not in spec


# --- reserved words --------------------------------------------------------


def test_no_column_is_named_after_a_reserved_word(db: Connection) -> None:
    """Not ``order``, ``user``, ``group``, ``limit``, ``end``, nor
    ``min``, ``max``, ``key``, ``rank``..."""
    offenders = [
        f"{table}.{name}"
        for table in sorted(EXPECTED_TABLES)
        for name in columns(db, table)
        if name.lower() in FORBIDDEN_COLUMN_NAMES
    ]
    assert offenders == []


def test_every_column_parses_unquoted(db: Connection) -> None:
    """The general case, asked of the server rather than a word list.

    A reserved identifier fails to parse without double quotes, which is a
    permanent tax on every raw query persistence and results write.  Each probe runs
    in a savepoint: a failed statement aborts a PostgreSQL transaction.
    """
    offenders = []
    for table in sorted(EXPECTED_TABLES):
        for name in columns(db, table):
            try:
                with db.begin_nested():
                    db.execute(text(f"SELECT {name} FROM {table} WHERE 1 = 0"))
            except DatabaseError as exc:
                if is_reserved_word_error(exc):
                    offenders.append(f"{table}.{name}")
                else:  # pragma: no cover - any other failure is a real bug
                    raise
    assert offenders == []


def test_renamed_columns_use_their_safe_names(db: Connection) -> None:
    """``order_qty``, ``min_value``,
    ``max_value``, and a raw unquoted ``SELECT`` over them parses."""
    assert "order_qty" in columns(db, "weeks")
    demand = columns(db, "demand_configs")
    assert "min_value" in demand
    assert "max_value" in demand
    db.execute(text("SELECT order_qty FROM weeks WHERE 1 = 0"))
    db.execute(text("SELECT min_value, max_value FROM demand_configs WHERE 1 = 0"))


# --- unique keys -----------------------------------------------------------


def test_weeks_is_unique_on_game_week_role(db: Connection) -> None:
    """Without it a retried persistence writes every week twice and every
    chart doubles."""
    game_id = insert_game(db)
    assert ("game_id", "week", "role") in unique_key_columns(db, "weeks")
    insert_row(db, "weeks", game_id=game_id, week=1, role=Role.RETAILER.value)
    insert_row(db, "weeks", game_id=game_id, week=2, role=Role.RETAILER.value)
    insert_row(db, "weeks", game_id=game_id, week=1, role=Role.FACTORY.value)
    with pytest.raises(IntegrityError):
        insert_row(db, "weeks", game_id=game_id, week=1, role=Role.RETAILER.value)


def test_role_configs_is_unique_on_game_and_role(db: Connection) -> None:
    """One row per role per game."""
    game_id = insert_game(db)
    assert ("game_id", "role") in unique_key_columns(db, "role_configs")
    for role in Role:
        insert_row(db, "role_configs", game_id=game_id, role=role.value)
    with pytest.raises(IntegrityError):
        insert_row(db, "role_configs", game_id=game_id, role=Role.RETAILER.value)


def test_demand_series_is_unique_on_game_and_week(db: Connection) -> None:
    """One demand row per week per game."""
    game_id = insert_game(db)
    assert ("game_id", "week") in unique_key_columns(db, "demand_series")
    insert_row(db, "demand_series", game_id=game_id, week=1, quantity=4)
    insert_row(db, "demand_series", game_id=game_id, week=2, quantity=4)
    with pytest.raises(IntegrityError):
        insert_row(db, "demand_series", game_id=game_id, week=1, quantity=9)


@pytest.mark.parametrize("table", ["game_configs", "demand_configs"])
def test_one_row_per_game(db: Connection, table: str) -> None:
    """``game_id`` is unique on both single-row config tables."""
    game_id = insert_game(db)
    assert ("game_id",) in unique_key_columns(db, table)
    insert_row(db, table, game_id=game_id)
    with pytest.raises(IntegrityError):
        insert_row(db, table, game_id=game_id)


def test_users_firebase_uid_is_unique_and_indexed(db: Connection) -> None:
    """Unique, indexed."""
    assert ("firebase_uid",) in unique_key_columns(db, "users")
    insert_user(db, firebase_uid="uid-duplicate")
    with pytest.raises(IntegrityError):
        insert_user(db, firebase_uid="uid-duplicate")


# --- deletes and attribution -----------------------------------------------


def test_deleting_a_game_cascades_to_every_child(db: Connection) -> None:
    """One statement deletes a whole game.

    144 ``weeks`` rows is a full 36-week four-role game.
    """
    game_id = insert_game(db)
    insert_row(db, "game_configs", game_id=game_id)
    insert_row(db, "demand_configs", game_id=game_id, kind=DemandKind.CONSTANT.value)
    for role in Role:
        insert_row(db, "role_configs", game_id=game_id, role=role.value)
    insert_row(db, "participants", game_id=game_id, alias="P1")
    for week in range(1, 37):
        insert_row(db, "demand_series", game_id=game_id, week=week, quantity=4)
        for role in Role:
            insert_row(db, "weeks", game_id=game_id, week=week, role=role.value)

    assert _count(db, "weeks", game_id) == 144

    db.execute(text("DELETE FROM games WHERE id = :id"), {"id": game_id})

    for table in (
        "game_configs",
        "role_configs",
        "demand_configs",
        "participants",
        "demand_series",
        "weeks",
    ):
        assert _count(db, table, game_id) == 0, f"{table} did not cascade"


def test_deleting_a_user_keeps_the_games_they_played(db: Connection) -> None:
    """``ON DELETE SET NULL`` from ``users``.

    Other players are entitled to their own records, so the rows survive with
    the attribution removed.
    """
    user_id = insert_user(db)
    game_ids = []
    for _ in range(3):
        game_id = insert_game(db, host_user_id=user_id)
        insert_row(db, "participants", game_id=game_id, alias="P1", user_id=user_id)
        for role in Role:
            insert_row(db, "weeks", game_id=game_id, week=1, role=role.value)
        game_ids.append(game_id)

    db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    for game_id in game_ids:
        assert _count(db, "weeks", game_id) == 4
        owners = db.execute(
            text("SELECT user_id FROM participants WHERE game_id = :id"),
            {"id": game_id},
        ).scalars()
        assert list(owners) == [None]
        host = db.execute(
            text("SELECT host_user_id FROM games WHERE id = :id"), {"id": game_id}
        ).scalar_one()
        assert host is None


def _count(db: Connection, table: str, game_id: int) -> int:
    return int(
        db.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE game_id = :id"), {"id": game_id}
        ).scalar_one()
    )


# --- guest hosts, guests and bots ------------------------------------------


def test_a_guest_hosted_game_inserts_with_no_account(db: Connection) -> None:
    """**D3** -- hosting is a capability, not an account. A game needs no
    host account: ``games.host_user_id`` is nullable and only a signed-in
    host fills it (D23). There is no host name column."""
    spec = columns(db, "games")
    assert spec["host_user_id"]["IS_NULLABLE"] == "YES"
    assert "host_display_name" not in spec
    game_id = insert_game(db)
    host = db.execute(
        text("SELECT host_user_id FROM games WHERE id = :id"), {"id": game_id}
    ).scalar_one()
    assert host is None


def test_games_host_user_id_is_indexed(db: Connection) -> None:
    """A queried foreign key gets an explicitly named index: PostgreSQL never
    creates one for a foreign key itself."""
    indexdef = db.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'games' AND indexname = 'ix_games_host_user_id'"
        )
    ).scalar_one()
    assert "(host_user_id)" in indexdef


def test_participants_accepts_a_guest_and_a_bot(db: Connection) -> None:
    """Exactly one of ``user_id`` / ``guest_identity`` for a human; both NULL
    for a bot."""
    game_id = insert_game(db)
    guest = insert_row(
        db,
        "participants",
        game_id=game_id,
        alias="P1",
        user_id=None,
        guest_identity="guest_2f0b1c46-1c3f-4f9a-9a1e-6b0a3a1d4f21",
        participant_type="PLAYER",
        is_bot=False,
    )
    bot = insert_row(
        db,
        "participants",
        game_id=game_id,
        alias="P2",
        user_id=None,
        guest_identity=None,
        participant_type="PLAYER",
        is_bot=True,
    )
    rows = db.execute(
        text(
            "SELECT id, user_id, guest_identity, is_bot FROM participants "
            "WHERE game_id = :id ORDER BY id"
        ),
        {"id": game_id},
    ).all()
    by_id = {row.id: row for row in rows}
    assert by_id[guest].user_id is None
    assert by_id[guest].guest_identity.startswith("guest_")
    assert by_id[bot].user_id is None
    assert by_id[bot].guest_identity is None
    assert bool(by_id[bot].is_bot) is True


# --- money and precision ---------------------------------------------------


@pytest.mark.parametrize(("table", "column"), MONEY_COLUMNS)
def test_money_columns_are_numeric_12_2(
    db: Connection, table: str, column: str
) -> None:
    """``NUMERIC(12, 2)`` throughout."""
    spec = columns(db, table)[column]
    assert spec["DATA_TYPE"] == "numeric"
    assert (spec["NUMERIC_PRECISION"], spec["NUMERIC_SCALE"]) == (12, 2)


def test_no_decimal_column_lost_its_scale(db: Connection) -> None:
    """Generalised -- a ``NUMERIC(12, 0)`` typo anywhere."""
    for table in sorted(EXPECTED_TABLES):
        for name, spec in columns(db, table).items():
            if spec["DATA_TYPE"] == "numeric":
                assert spec["NUMERIC_SCALE"] >= 2, f"{table}.{name} has scale 0"


def test_money_rounds_to_two_places_and_never_to_an_integer(
    db: Connection,
) -> None:
    """``0.50`` reads back as ``0.50``.

    A ``NUMERIC(12, 0)`` typo is invisible until a debrief shows every cost
    as a whole number.
    """
    game_id = insert_game(db)
    insert_row(
        db,
        "weeks",
        game_id=game_id,
        week=1,
        role=Role.RETAILER.value,
        total_week_cost=Decimal("0.50"),
        cumulative_cost=Decimal("12.345"),
    )
    stored = db.execute(
        text("SELECT total_week_cost, cumulative_cost FROM weeks WHERE game_id = :id"),
        {"id": game_id},
    ).one()
    assert stored.total_week_cost == Decimal("0.50")
    assert stored.cumulative_cost in {Decimal("12.34"), Decimal("12.35")}


def test_bot_and_bullwhip_precision(db: Connection) -> None:
    """``NUMERIC(6, 4)`` for bot parameters, ``NUMERIC(10, 4)`` for
    ``bullwhip_avg``, which is a ratio and needs precision, not range."""
    config_spec = columns(db, "game_configs")
    for name in BOT_COLUMNS:
        spec = config_spec[name]
        assert (spec["NUMERIC_PRECISION"], spec["NUMERIC_SCALE"]) == (6, 4), name
    bullwhip = columns(db, "user_stats")["bullwhip_avg"]
    assert (bullwhip["NUMERIC_PRECISION"], bullwhip["NUMERIC_SCALE"]) == (10, 4)
    assert bullwhip["IS_NULLABLE"] == "YES"  # D12's null bullwhip

    game_id = insert_game(db)
    insert_row(db, "game_configs", game_id=game_id, theta=Decimal("0.2500"))
    stored = db.execute(
        text("SELECT theta FROM game_configs WHERE game_id = :id"), {"id": game_id}
    ).scalar_one()
    assert stored == Decimal("0.2500")


# --- enum-valued columns ---------------------------------------------------


def test_enum_valued_columns_accept_every_member(db: Connection) -> None:
    """Asserted at the application level, because v1 stores these as
    ``VARCHAR`` rather than a DB ``ENUM``: every member of the corresponding
    enum fits its column and round-trips unchanged."""
    game_id = insert_game(db)

    for role in Role:
        insert_row(db, "role_configs", game_id=game_id, role=role.value)
        insert_row(db, "weeks", game_id=game_id, week=1, role=role.value)
    stored_roles = set(
        db.execute(
            text("SELECT role FROM role_configs WHERE game_id = :id"), {"id": game_id}
        ).scalars()
    )
    assert stored_roles == {role.value for role in Role}

    insert_row(
        db,
        "participants",
        game_id=game_id,
        alias="P1",
        role=Role.RETAILER.value,
        participant_type="PLAYER",
    )
    assert set(
        db.execute(
            text("SELECT participant_type FROM participants WHERE game_id = :id"),
            {"id": game_id},
        ).scalars()
    ) == {"PLAYER"}

    for mode in RoleAssignmentMode:
        other = insert_game(db)
        insert_row(db, "game_configs", game_id=other, role_assignment_mode=mode.value)
        assert (
            db.execute(
                text(
                    "SELECT role_assignment_mode FROM game_configs "
                    "WHERE game_id = :id"
                ),
                {"id": other},
            ).scalar_one()
            == mode.value
        )

    for kind in DemandKind:
        other = insert_game(db)
        insert_row(db, "demand_configs", game_id=other, kind=kind.value)
        assert (
            db.execute(
                text("SELECT kind FROM demand_configs WHERE game_id = :id"),
                {"id": other},
            ).scalar_one()
            == kind.value
        )


def test_enum_columns_are_varchar_wide_enough_for_every_member(
    db: Connection,
) -> None:
    """The column widths, checked against the enums themselves."""
    longest_role = max(len(role.value) for role in Role)
    for table in ("role_configs", "weeks", "participants"):
        spec = columns(db, table)["role"]
        assert spec["DATA_TYPE"] == "character varying"
        assert spec["CHARACTER_MAXIMUM_LENGTH"] == 16 >= longest_role

    participant_type = columns(db, "participants")["participant_type"]
    assert participant_type["CHARACTER_MAXIMUM_LENGTH"] == 12 >= len("PLAYER")

    mode = columns(db, "game_configs")["role_assignment_mode"]
    assert (
        mode["CHARACTER_MAXIMUM_LENGTH"]
        == 20
        >= max(len(member.value) for member in RoleAssignmentMode)
    )

    kind = columns(db, "demand_configs")["kind"]
    assert (
        kind["CHARACTER_MAXIMUM_LENGTH"]
        == 16
        >= max(len(member.value) for member in DemandKind)
    )


# --- Factory-only columns --------------------------------------------------


def test_factory_only_columns_are_null_for_the_other_three_roles(
    db: Connection,
) -> None:
    """Both Factory columns are nullable and a non-Factory role row carries
    NULL."""
    spec = columns(db, "role_configs")
    for name in ("production_delay_weeks", "production_capacity_per_week"):
        assert spec[name]["IS_NULLABLE"] == "YES", name

    game_id = insert_game(db)
    for role in Role:
        values: dict[str, Any] = {"game_id": game_id, "role": role.value}
        if role is Role.FACTORY:
            values["production_delay_weeks"] = 2
            values["production_capacity_per_week"] = 100
        insert_row(db, "role_configs", **values)

    rows = db.execute(
        text(
            "SELECT role, production_delay_weeks, production_capacity_per_week "
            "FROM role_configs WHERE game_id = :id"
        ),
        {"id": game_id},
    ).all()
    by_role = {row.role: row for row in rows}
    for role in Role:
        row = by_role[role.value]
        if role is Role.FACTORY:
            assert row.production_delay_weeks == 2
            assert row.production_capacity_per_week == 100
        else:
            assert row.production_delay_weeks is None
            assert row.production_capacity_per_week is None


# --- recycled room codes ---------------------------------------------------


def test_room_code_is_indexed_but_not_unique_on_its_own(db: Connection) -> None:
    """Room codes are recycled once a room expires, so two games may share
    one.

    "Not unique" means **not unique on its own**.
    ``UNIQUE (room_code, started_at)`` is the database-level backstop for
    ``persist_game``'s idempotency (spec §10.2), so ``room_code`` is a member
    column of a unique index -- while remaining non-unique by itself, which
    is the property this test exists to protect.  Asserting that *no* index
    touching ``room_code`` is unique would fail against any schema carrying
    that constraint, and would read as a reason not to have the backstop.
    """
    rows = indexes(db, "games")
    room_code_indexes = [row for row in rows if row["COLUMN_NAME"] == "room_code"]
    assert room_code_indexes, "games.room_code is not indexed"

    sizes: dict[str, int] = {}
    for row in rows:
        sizes[row["INDEX_NAME"]] = sizes.get(row["INDEX_NAME"], 0) + 1
    single_column_unique = [
        row["INDEX_NAME"]
        for row in room_code_indexes
        if row["NON_UNIQUE"] == 0 and sizes[row["INDEX_NAME"]] == 1
    ]
    assert (
        not single_column_unique
    ), f"games.room_code must not be unique on its own: {single_column_unique}"

    # Two games may share a room code, so long as they did not start in the
    # same second -- which two real games in one room never do.
    first = insert_game(
        db, room_code="BEER01", started_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    )
    second = insert_game(
        db, room_code="BEER01", started_at=datetime(2026, 1, 9, 3, 4, 5, tzinfo=UTC)
    )
    assert first != second
    assert (
        db.execute(
            text("SELECT COUNT(*) FROM games WHERE room_code = 'BEER01'")
        ).scalar_one()
        == 2
    )


# --- UTC timestamps --------------------------------------------------------


def test_timestamps_round_trip_as_utc(app_connection: Connection) -> None:
    """No timezone drift.

    Uses the app's own engine, because the ``options=-c timezone=UTC``
    connect argument ``app/config.py`` gives it is what makes the session
    UTC.  The columns are ``timestamptz``, so a value reads back
    timezone-aware, and its offset must be zero whatever the server's own
    ``TimeZone`` is.
    """
    assert app_connection.execute(text("SHOW TimeZone")).scalar_one() == "UTC"

    written = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    game_id = insert_game(app_connection, started_at=written, finished_at=written)
    read_back = app_connection.execute(
        text("SELECT started_at, created_at FROM games WHERE id = :id"),
        {"id": game_id},
    ).one()

    started = read_back.started_at
    assert started.tzinfo is not None, "timestamptz must read back aware"
    assert started.utcoffset() == UTC.utcoffset(None)
    assert started == written

    created = read_back.created_at
    assert created.tzinfo is not None
    assert created.utcoffset() == UTC.utcoffset(None)
    drift = abs((created - datetime.now(UTC)).total_seconds())
    assert drift < 300, f"server_default timestamp is {drift:.0f}s off UTC"


def test_timestamp_columns_are_timestamptz(db: Connection) -> None:
    """Every stored time is ``timestamp with time zone``: a naive
    ``timestamp`` would silently take the session's zone on the way in."""
    for table in sorted(EXPECTED_TABLES):
        for name, spec in columns(db, table).items():
            if "timestamp" in spec["DATA_TYPE"]:
                assert (
                    spec["DATA_TYPE"] == "timestamp with time zone"
                ), f"{table}.{name} is {spec['DATA_TYPE']}"


# --- the Docker guard ------------------------------------------------------


def test_a_missing_docker_daemon_skips_loudly() -> None:
    """The suite must not quietly evaporate.

    Without a daemon the suite skips so the rest of the backend suite still
    runs, but the skip is reported with a reason *and* a
    warning, so an all-skipped run does not read like a passing one.
    """

    def broken_probe() -> None:
        raise RuntimeError("Cannot connect to the Docker daemon at unix:///var/run")

    reason = docker_unavailable_reason(broken_probe)
    assert reason is not None
    assert "Docker" in reason
    assert "Cannot connect to the Docker daemon" in reason

    with (
        pytest.warns(DockerUnavailableWarning),
        pytest.raises(pytest.skip.Exception) as caught,
    ):
        require_docker(broken_probe)
    assert "Docker" in str(caught.value)

    # ...and a working daemon never skips.
    assert docker_unavailable_reason(lambda: None) is None
    require_docker(lambda: None)


def test_every_module_in_this_suite_is_marked() -> None:
    """Every test here carries ``dbschema``.

    That is what lets the gate run this suite as ``pytest -m dbschema``
    and the rest as ``pytest -m "not dbschema"``; an unmarked test here runs
    on a machine with no Docker and fails for a reason that has nothing to
    do with the schema.
    """
    from . import test_migration

    for module in (sys.modules[__name__], test_migration):
        assert module.pytestmark.name == "dbschema", module.__name__


# --- the server refuses a missing default ----------------------------------


def test_a_missing_server_default_is_rejected_not_filled_in(db: Connection) -> None:
    """The raw-insert test can only catch a missing ``server_default``
    because the server refuses the insert.

    PostgreSQL has no permissive mode that would quietly fill a ``NOT NULL``
    column with a zero value, so this proves the detector works on the same
    server the suite runs against: the same bug, in a throwaway temporary
    table, is a ``not_null_violation`` (SQLSTATE ``23502``).
    """
    db.execute(
        text(
            "CREATE TEMPORARY TABLE missing_default_probe ("
            "  id INTEGER NOT NULL PRIMARY KEY,"
            "  created_at TIMESTAMPTZ NOT NULL"  # the bug: no server_default
            ")"
        )
    )
    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(text("INSERT INTO missing_default_probe (id) VALUES (1)"))
    assert getattr(caught.value.orig, "sqlstate", None) == "23502"
