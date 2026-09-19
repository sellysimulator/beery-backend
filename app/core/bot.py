"""Sterman's anchor-and-adjust heuristic bot (`08-bot-agent.md`).

A `BotAgent` fills an empty role, or stands in for a player who has dropped
out (`00-decisions.md` D7). It plays a plausible human, not an optimal one:
the whole pedagogical point of the beer game is the bullwhip effect a real
person produces, and a bot that flattened it would destroy the lesson
(`beer-game-spec.md` §8.5).

Pure: this module imports only from `app.core` and the standard library. It
performs no I/O, reads no clock, holds no RNG (never `random`) and does no
logging. `decide()` mutates nothing; all state change happens in `observe()`.

A `BotAgent` is not a `RoleAgent` and does not subclass one: a `RoleAgent`
holds inventory, a `BotAgent` holds a decision rule. The engine owns the
`RoleAgent` for every role, bot-played or not; the caller asks the `BotAgent`
what to submit and passes that to `GameEngine.submit_order`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config_models import GameConfig
from .enums import Role

__all__ = ["BotAgent", "BotMemory", "bot_for"]


def _half_up_round(value: float) -> int:
    """Half-up rounding: `floor(x + 0.5)`, never Python's round-half-to-even."""
    return math.floor(value + 0.5)


@dataclass
class BotMemory:
    """The only state a bot carries between weeks.

    Serialised into the room document alongside the engine. Not frozen:
    `observe()` mutates it in place, which is the one place any mutation is
    allowed to happen (§3.6).
    """

    expected_demand: float
    last_observed_demand: int | None

    def to_payload(self) -> dict:
        """A JSON-safe snapshot."""
        return {
            "expected_demand": self.expected_demand,
            "last_observed_demand": self.last_observed_demand,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> BotMemory:
        """The inverse of `to_payload`."""
        last_observed = payload["last_observed_demand"]
        return cls(
            expected_demand=float(payload["expected_demand"]),
            last_observed_demand=(
                None if last_observed is None else int(last_observed)
            ),
        )


class BotAgent:
    """Sterman's anchor-and-adjust heuristic, bound to one role.

    ```
    expected_demand_t = theta * observed_demand_(t-1) + (1 - theta) * expected_demand_(t-1)

    order_t = max(0, expected_demand_t
                     + alpha * (target_stock - inventory_t + backlog_t - beta * supply_line_t))
    ```

    `beta` is the whole point (§3.1): at 1.0 the bot fully credits its own
    supply line and behaves stably; at 0.0 it ignores the supply line and
    panics the way a losing human does.
    """

    role: Role
    memory: BotMemory
    target_stock: float
    theta: float
    alpha: float
    beta: float

    def __init__(self, role: Role, config: GameConfig) -> None:
        """Build a bot for `role`, deriving its parameters from `config`.

        `target_stock` is per role: `initial_inventory * target_stock_multiplier`
        for *this* role's own configuration (§3.2). The initial anchor is
        `initial_order_in_pipeline`, the steady-state quantity the game was
        pre-loaded with -- including for the Retailer, whose own order
        pipeline does not exist but whose configured quantity is still the
        right opening belief (§3.3).
        """
        self.role = role
        role_config = config.role_config(role)
        bot_config = config.bot

        self.theta = bot_config.theta
        self.alpha = bot_config.alpha
        self.beta = bot_config.beta
        self.target_stock = (
            role_config.initial_inventory * bot_config.target_stock_multiplier
        )
        self.memory = BotMemory(
            expected_demand=float(role_config.initial_order_in_pipeline),
            last_observed_demand=None,
        )

    def observe(self, incoming_order: int) -> None:
        """Update the demand anchor with this week's observed demand.

        Called once per week, BEFORE `decide()`. The smoothing is
        deliberately lagged: `expected_demand` is updated against the
        *previous* observation, never the one just passed in, so the bot
        never gets to use this week's demand in this week's anchor (§3.4).
        """
        if self.memory.last_observed_demand is not None:
            self.memory.expected_demand = (
                self.theta * self.memory.last_observed_demand
                + (1.0 - self.theta) * self.memory.expected_demand
            )
        self.memory.last_observed_demand = incoming_order

    def decide(self, inventory: int, backlog: int, supply_line: int) -> int:
        """Return the order quantity for the open week.

        Pure: identical inputs and identical memory always give an identical
        answer, and nothing is mutated (§3.6). The floor is 0, even when
        `visibility.allow_negative_orders` is true -- a heuristic that hands
        stock back is not what the model describes. `max_order_quantity` is
        never applied here; `GameEngine.submit_order` clamps, and clamping
        twice would hide a clamping bug (§3.5).
        """
        raw = self.memory.expected_demand + self.alpha * (
            self.target_stock - inventory + backlog - self.beta * supply_line
        )
        return max(0, _half_up_round(raw))

    def to_payload(self) -> dict:
        """`{"role": str, "memory": {...}}` and nothing else.

        The four parameters and `target_stock` are derived from `config`,
        which is passed to `from_payload` separately, exactly as the engine's
        payload omits its config.
        """
        return {"role": self.role.value, "memory": self.memory.to_payload()}

    @classmethod
    def from_payload(cls, payload: dict, config: GameConfig) -> BotAgent:
        """Rebuild the bot a `to_payload()` described."""
        role = Role(payload["role"])
        bot = cls(role, config)
        bot.memory = BotMemory.from_payload(payload["memory"])
        return bot

    def __eq__(self, other: object) -> bool:
        """Value equality over `role` and `memory`.

        Declared because criterion 13 compares two bots and the default
        identity comparison would make it vacuous. `BotMemory` is a plain
        dataclass, so its own `==` is generated.
        """
        if not isinstance(other, BotAgent):
            return NotImplemented
        return self.role == other.role and self.memory == other.memory

    def __repr__(self) -> str:
        return (
            f"BotAgent(role={self.role.value}, "
            f"expected_demand={self.memory.expected_demand!r})"
        )


def bot_for(role: Role, config: GameConfig) -> BotAgent:
    """A `BotAgent` for `role`, built from `config`."""
    return BotAgent(role, config)
