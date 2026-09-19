"""The fixed-length FIFO behind every delay in the game (`05-pipeline.md`).

Pure: this module imports only from the standard library. It performs no I/O,
reads no clock, holds no RNG and does no logging.

A `Pipeline` holds either goods in transit or orders in flight. One week does
exactly one `advance()` followed by exactly one `push()`, so a quantity pushed
in week `w` is returned by `advance()` in week `w + length`. Between the two
calls the pipeline is transiently one slot short, which is why `length` is a
stored configuration value rather than `len(self._slots)`.
"""

from __future__ import annotations


class Pipeline:
    """A FIFO of `length` non-negative integer slots, front first."""

    __slots__ = ("_length", "_slots")

    def __init__(self, length: int, fill: int = 0) -> None:
        """A FIFO with exactly `length` slots, every slot pre-loaded with `fill`.

        Raises ValueError if length < 1 or fill < 0.
        """
        if length < 1:
            raise ValueError(f"length must be at least 1, got {length}")
        if fill < 0:
            raise ValueError(f"fill must not be negative, got {fill}")
        self._length: int = length
        self._slots: list[int] = [fill] * length

    def advance(self) -> int:
        """Remove and return the front slot — what arrives this week.

        Leaves the pipeline one slot SHORT; a matching `push()` restores it.
        Raises IndexError when no slots remain.
        """
        return self._slots.pop(0)

    def push(self, qty: int) -> None:
        """Append `qty` at the back. Raises ValueError if qty < 0."""
        if qty < 0:
            raise ValueError(f"qty must not be negative, got {qty}")
        self._slots.append(qty)

    def total(self) -> int:
        """Sum of all slots currently held — the supply line."""
        return sum(self._slots)

    def slots(self) -> list[int]:
        """A COPY of the contents, front first.

        Mutating the returned list must not affect the pipeline.
        """
        return list(self._slots)

    def __len__(self) -> int:
        """The number of slots currently held, which dips by one mid-cycle."""
        return len(self._slots)

    def __eq__(self, other: object) -> bool:
        """Equal when both the configured length and the current slots match."""
        if not isinstance(other, Pipeline):
            return NotImplemented
        return self._length == other._length and self._slots == other._slots

    def __repr__(self) -> str:
        return f"Pipeline(length={self._length}, slots={self._slots!r})"

    @property
    def length(self) -> int:
        """The configured length.

        NOT the current slot count, which is one lower between an `advance()`
        and its `push()`.
        """
        return self._length

    def to_payload(self) -> dict:
        """A JSON-serialisable `{"length": int, "slots": list[int]}`."""
        return {"length": self._length, "slots": list(self._slots)}

    @classmethod
    def from_payload(cls, payload: dict) -> Pipeline:
        """Rebuild a pipeline from `to_payload()`, including mid-cycle."""
        pipeline = cls(length=int(payload["length"]))
        pipeline._slots = [int(slot) for slot in payload["slots"]]
        return pipeline
