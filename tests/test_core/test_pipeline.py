"""Black-box tests for section 05 -- ``app/core/pipeline.py``.

Covers every numbered item of ``05-pipeline.md §5`` (acceptance criteria 1-17,
including 14b and 15b) and every item of ``§6`` (failure modes 1-7), against the frozen
public surface in ``§2`` and the normative worked examples in ``§4``.

Nothing private is touched: the tests drive the pipeline only through
``advance``, ``push``, ``total``, ``slots``, ``length``, ``len``, ``==``,
``repr``, ``to_payload`` and ``from_payload``.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.core.pipeline import Pipeline

# --------------------------------------------------------------------------
# AC 1, AC 2 -- construction and pre-loading (05-pipeline.md §3.2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("length", [1, 2, 3, 8])
@pytest.mark.parametrize("fill", [0, 1, 4, 9_999])
def test_construction_preloads_every_slot_with_fill(length: int, fill: int) -> None:
    """AC 1: ``Pipeline(n, fill=f).slots() == [f] * n`` and ``.total() == n * f``."""
    p = Pipeline(length, fill=fill)

    assert p.slots() == [fill] * length
    assert p.total() == length * fill


@pytest.mark.parametrize("length", [1, 2, 3, 8])
def test_fill_defaults_to_zero(length: int) -> None:
    """AC 2: ``Pipeline(n).slots() == [0] * n``."""
    p = Pipeline(length)

    assert p.slots() == [0] * length
    assert p.total() == 0


def test_fill_is_per_slot_not_a_total() -> None:
    """FM 1: ``Pipeline(2, fill=4).total()`` is 8, not 4.

    Fails against an implementation that spreads ``fill`` across the pipeline
    instead of loading it into each slot.
    """
    p = Pipeline(2, fill=4)

    assert p.slots() == [4, 4]
    assert p.total() == 8


def test_fill_is_per_slot_for_the_classic_mit_supply_line() -> None:
    """FM 1, stated as §3.2 states it: a delay of 2 and 4 units per slot is a
    supply line of 8."""
    assert Pipeline(2, fill=4).total() == 8
    assert Pipeline(3, fill=4).total() == 12
    assert Pipeline(8, fill=4).total() == 32


# --------------------------------------------------------------------------
# AC 3, AC 4, AC 5 -- the advance/push cycle (§3.1)
# --------------------------------------------------------------------------


def test_advance_returns_and_removes_the_front_value() -> None:
    """AC 3."""
    p = Pipeline(3, fill=0)
    p.advance()
    p.push(1)
    p.advance()
    p.push(2)
    p.advance()
    p.push(3)
    # slots are now [1, 2, 3], front first.
    assert p.slots() == [1, 2, 3]

    assert p.advance() == 1
    assert p.slots() == [2, 3]
    assert p.advance() == 2
    assert p.slots() == [3]


def test_push_appends_at_the_back() -> None:
    """AC 4."""
    p = Pipeline(2, fill=4)
    p.advance()

    p.push(10)

    assert p.slots() == [4, 10]


def test_len_is_restored_by_the_push_that_follows_an_advance() -> None:
    """AC 5."""
    for length in (1, 2, 3, 8):
        p = Pipeline(length, fill=2)
        p.advance()
        assert len(p) == length - 1
        p.push(7)
        assert len(p) == p.length == length


def test_worked_example_length_two() -> None:
    """§4, first normative example."""
    p = Pipeline(length=2, fill=4)
    assert p.slots() == [4, 4]
    assert p.total() == 8
    assert p.length == 2
    assert len(p) == 2

    assert p.advance() == 4
    assert p.slots() == [4]
    assert p.total() == 4
    assert len(p) == 1
    assert p.length == 2

    p.push(10)
    assert p.slots() == [4, 10]
    assert p.total() == 14

    assert p.advance() == 4
    p.push(0)
    assert p.slots() == [10, 0]

    assert p.advance() == 10


def test_worked_example_length_one() -> None:
    """§4, the shortest legal pipeline."""
    p = Pipeline(length=1, fill=4)
    assert p.slots() == [4]
    assert p.advance() == 4
    p.push(7)
    assert p.advance() == 7


def test_worked_example_week_by_week_trace() -> None:
    """§3.1's trace, asserted slot by slot."""
    p = Pipeline(length=2, fill=4)
    assert p.slots() == [4, 4]

    assert p.advance() == 4
    assert p.slots() == [4]
    p.push(7)
    assert p.slots() == [4, 7]

    assert p.advance() == 4
    assert p.slots() == [7]
    p.push(9)
    assert p.slots() == [7, 9]

    assert p.advance() == 7
    assert p.slots() == [9]


# --------------------------------------------------------------------------
# AC 6 -- arrival timing, and FM 2 / FM 6
# --------------------------------------------------------------------------


@pytest.mark.parametrize("length", [1, 2, 3, 8])
def test_a_value_pushed_in_cycle_w_arrives_in_cycle_w_plus_length(
    length: int,
) -> None:
    """AC 6: the only timing rule in the game, for lengths 1, 2, 3 and 8."""
    fill = 4
    p = Pipeline(length, fill=fill)
    pushed: dict[int, int] = {}

    for cycle in range(1, 3 * length + 5):
        arrived = p.advance()
        if cycle <= length:
            assert arrived == fill, f"cycle {cycle} should still deliver the fill"
        else:
            assert arrived == pushed[cycle - length], (
                f"cycle {cycle} should deliver what was pushed in "
                f"cycle {cycle - length}"
            )
        value = 100 + cycle
        p.push(value)
        pushed[cycle] = value


def test_arrival_order_is_fifo_not_lifo() -> None:
    """FM 2: ``[4, 4]``, push 10, push 20 -- the next two advances are 4, 4."""
    p = Pipeline(2, fill=4)

    p.push(10)
    p.push(20)

    assert p.advance() == 4
    assert p.advance() == 4
    assert p.advance() == 10
    assert p.advance() == 20


def test_a_value_pushed_now_does_not_arrive_at_the_next_advance() -> None:
    """FM 6: off-by-one arrival with ``length=2``."""
    p = Pipeline(2, fill=4)

    assert p.advance() == 4
    p.push(99)

    assert p.advance() == 4, "the intervening advance must return the pre-loaded value"
    p.push(0)
    assert p.advance() == 99


# --------------------------------------------------------------------------
# AC 7 -- total() tracks slots() at every point in the cycle
# --------------------------------------------------------------------------


@pytest.mark.parametrize("length", [1, 2, 3, 8])
def test_total_equals_sum_of_slots_at_every_point_in_the_cycle(
    length: int,
) -> None:
    """AC 7."""
    p = Pipeline(length, fill=3)
    assert p.total() == sum(p.slots())

    for cycle in range(1, 20):
        p.advance()
        assert p.total() == sum(p.slots()), "mid-cycle, after advance()"
        p.push(cycle * 2)
        assert p.total() == sum(p.slots()), "at rest, after push()"


def test_total_after_advance_excludes_what_just_arrived() -> None:
    """§3.3: the supply line a deciding player is shown."""
    p = Pipeline(2, fill=4)

    p.advance()

    assert p.total() == 4


# --------------------------------------------------------------------------
# AC 8 and FM 4 / FM 5 -- slots() is a copy, instances are independent
# --------------------------------------------------------------------------


def test_slots_returns_a_copy_that_cannot_corrupt_the_pipeline() -> None:
    """AC 8 / FM 4."""
    p = Pipeline(2, fill=4)

    s = p.slots()
    s.append(99)
    s[0] = -1000

    assert p.total() == 8
    assert p.slots() == [4, 4]
    assert len(p) == 2


def test_slots_returns_a_fresh_list_each_call() -> None:
    """AC 8: two calls must not hand back the same object."""
    p = Pipeline(3, fill=1)

    assert p.slots() is not p.slots()


def test_two_pipelines_do_not_share_state() -> None:
    """FM 5: no shared default list between instances."""
    a = Pipeline(2)
    b = Pipeline(2)

    a.advance()
    a.push(42)

    assert b.slots() == [0, 0]
    assert b.total() == 0
    assert len(b) == 2
    assert a.slots() == [0, 42]


def test_two_pipelines_built_with_the_same_fill_do_not_share_state() -> None:
    """FM 5, with a non-default fill."""
    a = Pipeline(3, fill=5)
    b = Pipeline(3, fill=5)

    for value in (1, 2, 3):
        a.advance()
        a.push(value)

    assert b.slots() == [5, 5, 5]
    assert a.slots() == [1, 2, 3]


# --------------------------------------------------------------------------
# AC 9 and FM 3 -- length is configuration, len() is the live slot count
# --------------------------------------------------------------------------


@pytest.mark.parametrize("length", [1, 2, 3, 8])
def test_length_is_stable_while_len_dips_between_advance_and_push(
    length: int,
) -> None:
    """AC 9."""
    p = Pipeline(length, fill=4)
    assert p.length == length
    assert len(p) == length

    p.advance()
    assert p.length == length, "the configured length never changes"
    assert len(p) == length - 1

    p.push(1)
    assert p.length == length
    assert len(p) == length


def test_one_hundred_cycles_do_not_drift() -> None:
    """FM 3: length drift only shows up dozens of weeks in."""
    p = Pipeline(2, fill=4)

    for cycle in range(100):
        p.advance()
        p.push(cycle)
        assert len(p) == p.length

    assert p.length == 2
    assert len(p) == 2
    assert p.slots() == [98, 99]


# --------------------------------------------------------------------------
# AC 10, AC 11, AC 12 -- rejected values (§3.6)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("length", [0, -1, -8])
def test_a_length_below_one_raises_value_error(length: int) -> None:
    """AC 10."""
    with pytest.raises(ValueError):
        Pipeline(length)


@pytest.mark.parametrize("fill", [-1, -100])
def test_a_negative_fill_raises_value_error(fill: int) -> None:
    """AC 11."""
    with pytest.raises(ValueError):
        Pipeline(2, fill=fill)


@pytest.mark.parametrize("qty", [-1, -9_999])
def test_push_of_a_negative_quantity_raises_and_changes_nothing(qty: int) -> None:
    """AC 12 / §3.6."""
    p = Pipeline(2, fill=4)
    before_slots = p.slots()
    before_total = p.total()
    before_len = len(p)

    with pytest.raises(ValueError):
        p.push(qty)

    assert p.slots() == before_slots
    assert p.total() == before_total
    assert len(p) == before_len
    assert p.length == 2


def test_push_of_a_negative_quantity_mid_cycle_changes_nothing() -> None:
    """AC 12, while the pipeline is transiently one slot short."""
    p = Pipeline(2, fill=4)
    p.advance()

    with pytest.raises(ValueError):
        p.push(-1)

    assert p.slots() == [4]
    assert len(p) == 1
    assert p.length == 2

    p.push(7)
    assert p.slots() == [4, 7]


def test_push_of_zero_is_accepted() -> None:
    """§3.6 rejects negatives only; zero is an ordinary quantity."""
    p = Pipeline(2, fill=4)
    p.advance()

    p.push(0)

    assert p.slots() == [4, 0]


# --------------------------------------------------------------------------
# AC 13, AC 14 -- serialisation (§3.5)
# --------------------------------------------------------------------------


def test_to_payload_has_the_declared_shape() -> None:
    """§2: ``{"length": int, "slots": list[int]}``."""
    payload = Pipeline(2, fill=4).to_payload()

    assert payload == {"length": 2, "slots": [4, 4]}


@pytest.mark.parametrize("length", [1, 2, 3, 8])
@pytest.mark.parametrize("fill", [0, 4])
def test_round_trip_at_rest(length: int, fill: int) -> None:
    """AC 13, at rest."""
    p = Pipeline(length, fill=fill)

    restored = Pipeline.from_payload(p.to_payload())

    assert restored == p
    assert restored.slots() == p.slots()
    assert restored.length == p.length
    assert len(restored) == len(p)
    assert restored.total() == p.total()


@pytest.mark.parametrize("length", [1, 2, 3, 8])
def test_round_trip_mid_cycle(length: int) -> None:
    """AC 13, mid-cycle -- the §4 round-trip example generalised."""
    p = Pipeline(length, fill=2)
    p.advance()

    restored = Pipeline.from_payload(p.to_payload())

    assert restored == p
    assert restored.length == length
    assert len(restored) == length - 1
    assert restored.slots() == p.slots()

    restored.push(11)
    p.push(11)
    assert restored == p


def test_round_trip_after_a_long_run() -> None:
    """AC 13, with a pipeline whose slots all differ."""
    p = Pipeline(3, fill=1)
    for cycle in range(1, 10):
        p.advance()
        p.push(cycle * 3)

    assert Pipeline.from_payload(p.to_payload()) == p


def test_to_payload_is_json_serialisable() -> None:
    """AC 14."""
    p = Pipeline(3, fill=2)
    p.advance()
    p.push(7)

    text = json.dumps(p.to_payload())

    assert json.loads(text) == p.to_payload()
    assert Pipeline.from_payload(json.loads(text)) == p


def test_to_payload_is_json_serialisable_mid_cycle() -> None:
    """AC 14, while one slot short."""
    p = Pipeline(2, fill=4)
    p.advance()

    text = json.dumps(p.to_payload())

    assert Pipeline.from_payload(json.loads(text)) == p


def test_to_payload_slots_is_a_copy_at_rest() -> None:
    """AC 14b / §3.5b: a caller holds the payload across a week."""
    p = Pipeline(2, fill=4)

    payload = p.to_payload()
    payload["slots"].append(99)
    payload["slots"][0] = -1000
    payload["length"] = 77

    assert p.total() == 8
    assert p.slots() == [4, 4]
    assert len(p) == 2
    assert p.length == 2


def test_to_payload_slots_is_a_copy_mid_cycle() -> None:
    """AC 14b / §3.5b, while the pipeline is one slot short."""
    p = Pipeline(3, fill=2)
    p.advance()

    payload = p.to_payload()
    payload["slots"].clear()
    payload["slots"].extend([-5, -6, -7])

    assert p.total() == 4
    assert p.slots() == [2, 2]
    assert len(p) == 2
    assert p.length == 3

    p.push(6)
    assert p.slots() == [2, 2, 6]


def test_to_payload_returns_a_fresh_payload_each_call() -> None:
    """AC 14b / §3.5b: ``to_payload()`` is fresh, so two callers cannot alias
    each other's copy."""
    p = Pipeline(2, fill=4)

    first = p.to_payload()
    second = p.to_payload()

    assert first == second
    assert first is not second
    assert first["slots"] is not second["slots"]
    assert first["slots"] is not p.slots()


# --------------------------------------------------------------------------
# AC 15, AC 15b and FM 7 -- equality (§3.5)
# --------------------------------------------------------------------------


def test_equal_length_and_equal_slots_compare_equal() -> None:
    """AC 15."""
    assert Pipeline(2, fill=4) == Pipeline(2, fill=4)
    assert Pipeline(1) == Pipeline(1)

    a = Pipeline(3, fill=0)
    b = Pipeline(3, fill=0)
    for value in (5, 6, 7):
        a.advance()
        a.push(value)
        b.advance()
        b.push(value)
    assert a == b


def test_differing_slots_do_not_compare_equal() -> None:
    """AC 15."""
    a = Pipeline(2, fill=4)
    b = Pipeline(2, fill=5)

    assert a != b

    c = Pipeline(2, fill=4)
    c.advance()
    c.push(9)
    assert a != c


def test_differing_length_does_not_compare_equal() -> None:
    """AC 15."""
    assert Pipeline(2, fill=0) != Pipeline(3, fill=0)


def test_slot_order_matters_for_equality() -> None:
    """AC 15: equal multisets in a different order are different pipelines."""
    a = Pipeline(2, fill=0)
    a.advance()
    a.push(1)
    a.advance()
    a.push(2)

    b = Pipeline(2, fill=0)
    b.advance()
    b.push(2)
    b.advance()
    b.push(1)

    assert a.slots() == [1, 2]
    assert b.slots() == [2, 1]
    assert a != b


def test_an_advanced_pipeline_is_not_equal_to_a_fresh_one_that_matches() -> None:
    """FM 7 / §3.5: mid-cycle equality must account for the configured length."""
    advanced = Pipeline(3, fill=2)
    advanced.advance()
    fresh = Pipeline(2, fill=2)

    assert advanced.slots() == fresh.slots() == [2, 2]
    assert advanced != fresh
    assert fresh != advanced


def test_an_advanced_pipeline_is_not_equal_to_the_same_pipeline_at_rest() -> None:
    """§3.5, restated for two pipelines of identical length."""
    a = Pipeline(2, fill=4)
    b = Pipeline(2, fill=4)

    a.advance()

    assert a != b

    a.push(4)
    assert a == b


@pytest.mark.parametrize(
    "other",
    [[0, 0], "anything", 0, None, (0, 0), {"length": 2, "slots": [0, 0]}],
)
def test_comparison_against_a_non_pipeline_is_false_not_an_error(
    other: object,
) -> None:
    """AC 15b: ``__eq__`` returns ``NotImplemented``, so Python falls back to
    identity and the comparison is simply ``False``."""
    p = Pipeline(2)

    assert (p == other) is False
    assert (p != other) is True
    assert (other == p) is False


# --------------------------------------------------------------------------
# AC 16 -- advancing past a push
# --------------------------------------------------------------------------


def test_advancing_twice_without_pushing_works_while_slots_remain() -> None:
    """AC 16."""
    p = Pipeline(3, fill=0)
    p.advance()
    p.push(1)
    p.advance()
    p.push(2)
    p.advance()
    p.push(3)
    assert p.slots() == [1, 2, 3]

    assert p.advance() == 1
    assert p.advance() == 2
    assert len(p) == 1
    assert p.length == 3
    assert p.total() == 3
    assert p.advance() == 3
    assert len(p) == 0
    assert p.total() == 0
    assert p.slots() == []


def test_advancing_an_empty_pipeline_raises_index_error() -> None:
    """AC 16: ``length == 1``, advanced twice with no push."""
    p = Pipeline(1, fill=4)

    assert p.advance() == 4

    with pytest.raises(IndexError):
        p.advance()


def test_an_emptied_pipeline_can_be_refilled_by_pushes() -> None:
    """AC 16: emptying is not a terminal state, only advancing past it raises."""
    p = Pipeline(2, fill=4)
    p.advance()
    p.advance()
    assert len(p) == 0

    with pytest.raises(IndexError):
        p.advance()

    p.push(5)
    p.push(6)

    assert p.slots() == [5, 6]
    assert p.length == 2
    assert p.advance() == 5


# --------------------------------------------------------------------------
# AC 17 -- purity of the module (00-conventions.md §4)
# --------------------------------------------------------------------------


def _pipeline_source_path() -> Path:
    spec = importlib.util.find_spec("app.core.pipeline")
    assert spec is not None and spec.origin is not None
    return Path(spec.origin)


def test_module_imports_only_from_the_standard_library() -> None:
    """AC 17."""
    tree = ast.parse(_pipeline_source_path().read_text(encoding="utf-8"))
    imported: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import is not a standard-library import"
            assert node.module is not None
            imported.append(node.module.split(".")[0])

    non_stdlib = [name for name in imported if name not in sys.stdlib_module_names]
    assert non_stdlib == [], f"non-standard-library imports: {non_stdlib}"


# --------------------------------------------------------------------------
# §2 -- the remaining declared members exist and behave
# --------------------------------------------------------------------------


def test_repr_is_a_non_empty_string() -> None:
    """§3.5b: ``__repr__`` has no fixed format and no test may assert its exact
    text, so only that it produces a non-empty string is asserted."""
    assert isinstance(repr(Pipeline(2, fill=4)), str)
    assert repr(Pipeline(2, fill=4)) != ""
