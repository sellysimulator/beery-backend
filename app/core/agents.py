"""One object per link in the supply chain (`06-role-agents.md`).

A `RoleAgent` holds one role's inventory, backlog, pipelines and accumulated
cost, and knows how to settle a week *for itself*.

An agent does **not** know about its neighbours, and never holds a reference to
another agent. Everything that crosses the chain — a shipment moving
downstream, an order moving upstream — is routed by the `GameEngine` in section
07, which reads a settlement's `shipped` and calls `receive_shipment` on the
neighbour itself. That decoupling is what makes these objects testable in
isolation and what makes the two-pass settlement of `00-decisions.md` D9
expressible: `advance()` and `settle()` are separate steps precisely so that
**every** role can advance before **any** role delivers.

Pure: this module imports only from `app.core` and the standard library. It
performs no I/O, reads no clock, holds no RNG and does no logging.

Money accumulates as `float` and is never rounded during accumulation
(`00-conventions.md` §4); quantities are always `int`.
"""

from __future__ import annotations

from .config_models import FactoryConfig, GameConfig, RoleConfig
from .enums import Role
from .pipeline import Pipeline
from .records import OrderCharge, ProductionOutcome, WeekSettlement

__all__ = [
    "DistributorAgent",
    "FactoryAgent",
    "RetailerAgent",
    "RoleAgent",
    "WholesalerAgent",
    "agent_for",
]


class RoleAgent:
    """One link in the chain: its stock, its pipelines and its cost."""

    role: Role
    inventory: int
    backlog: int
    shipments: Pipeline
    orders: Pipeline | None  # None for RETAILER
    accumulated_cost: float
    last_order: int | None
    is_bot: bool

    def __init__(self, role: Role, config: GameConfig) -> None:
        """Build initial state from `config` (`06-role-agents.md` §3.1).

        Takes no neighbour argument, and never will: routing belongs to the
        engine.

        The Retailer has **no** order pipeline. Customer demand reaches it
        immediately (`beer-game-spec.md` §7 A2), so its
        `information_delay_weeks` is carried in `RoleConfig` for structural
        uniformity and left unused here. A pipeline that is advanced but whose
        value is discarded is exactly the kind of thing that later gets read by
        accident.
        """
        role_config = config.role_config(role)
        self._role_config: RoleConfig = role_config
        self.role = role
        self.inventory = role_config.initial_inventory
        self.backlog = role_config.initial_backlog
        # For the FACTORY this is the production line, of length
        # `production_delay_weeks`, already running when the game starts.
        self.shipments = Pipeline(
            config.inbound_delay_weeks(role),
            fill=role_config.initial_pipeline_quantity,
        )
        self.orders = (
            None
            if role is Role.RETAILER
            else Pipeline(
                config.order_delay_weeks(role),
                fill=role_config.initial_order_in_pipeline,
            )
        )
        self.accumulated_cost = 0.0
        self.last_order = None
        self.is_bot = False

    # --- Phase A, pass 1 --------------------------------------------------- #

    def advance(self) -> tuple[int, int | None]:
        """Pop the front of BOTH pipelines.

        Returns `(arriving, incoming_order)`. `incoming_order` is `None` for
        the Retailer, whose demand is supplied externally. Mutates nothing
        except the pipelines: inventory, backlog and cost are untouched.

        This is a separate step purely so that every role can advance before
        any role delivers (`00-decisions.md` D9).
        """
        arriving = self.shipments.advance()
        incoming_order = None if self.orders is None else self.orders.advance()
        return arriving, incoming_order

    # --- Phase A, pass 2 --------------------------------------------------- #

    def settle(self, week: int, arriving: int, incoming_order: int) -> WeekSettlement:
        """Receive, ship against the obligation, charge carrying cost.

        The order is exactly the one in `06-role-agents.md` §3.3: costs are
        charged on the **closing** inventory and backlog, after shipping.

        Does NOT move the shipped units anywhere — the caller reads
        `.shipped` and routes it to the downstream neighbour, or drops it for
        the Retailer, whose goods have left the system to the customer.

        Raises `ValueError` if `arriving` or `incoming_order` is negative.
        Neither is reachable through the engine: the Retailer's order comes
        from the demand series and the others' from pipelines, and both are
        guaranteed non-negative by sections 04 and 05.
        """
        if arriving < 0:
            raise ValueError(f"arriving must not be negative, got {arriving}")
        if incoming_order < 0:
            raise ValueError(
                f"incoming_order must not be negative, got {incoming_order}"
            )

        opening_inventory = self.inventory
        opening_backlog = self.backlog

        self.inventory += arriving  # 1. receive
        obligation = incoming_order + opening_backlog  # 2. what is owed
        shipped = min(self.inventory, obligation)  # 3. ship what exists
        self.inventory -= shipped
        self.backlog = obligation - shipped  # 4. what remains owed

        # 5. charge on CLOSING values
        holding_cost = self._role_config.holding_cost_per_unit_week * self.inventory
        backlog_cost = self._role_config.backlog_cost_per_unit_week * self.backlog
        carrying_cost = holding_cost + backlog_cost
        self.accumulated_cost += carrying_cost

        self._check_invariants()
        return WeekSettlement(
            role=self.role,
            week=week,
            opening_inventory=opening_inventory,
            opening_backlog=opening_backlog,
            arrived=arriving,
            incoming_order=incoming_order,
            obligation=obligation,
            shipped=shipped,
            unfulfilled=obligation - shipped,
            closing_inventory=self.inventory,
            closing_backlog=self.backlog,
            holding_cost=holding_cost,
            backlog_cost=backlog_cost,
            carrying_cost=carrying_cost,
        )

    # --- Phase C ----------------------------------------------------------- #

    def record_order(self, week: int, qty: int) -> OrderCharge:
        """Record the decision and charge order-related cost (D10).

        Does NOT push the order anywhere — the caller routes it upstream.

        A negative `qty`, reachable only when `visibility.allow_negative_orders`
        is true, charges a `fixed_order_cost` of 0 — a cancellation is not an
        order — and a correspondingly negative `purchase_cost`. Whether a
        negative order is legal at all is the engine's decision, not this
        method's.
        """
        self.last_order = qty
        fixed_order_cost = self._role_config.fixed_order_cost if qty > 0 else 0.0
        purchase_cost = self._role_config.unit_purchase_cost * qty
        order_cost = fixed_order_cost + purchase_cost
        self.accumulated_cost += order_cost

        self._check_invariants()
        return OrderCharge(
            role=self.role,
            week=week,
            order=qty,
            fixed_order_cost=fixed_order_cost,
            purchase_cost=purchase_cost,
            order_cost=order_cost,
        )

    def receive_order(self, qty: int) -> None:
        """Push a downstream neighbour's order into this role's order pipeline.

        Raises `TypeError` on a RETAILER, which has no order pipeline. Nothing
        in the system should ever call it there, and a silent no-op would hide
        a routing bug for the whole game.
        """
        if self.orders is None:
            raise TypeError(
                f"{type(self).__name__} has no order pipeline: "
                f"{self.role.value} takes customer demand directly."
            )
        self.orders.push(qty)

    def receive_shipment(self, qty: int) -> None:
        """Push an upstream neighbour's shipment into the shipment pipeline."""
        self.shipments.push(qty)

    # --- views ------------------------------------------------------------- #

    def supply_line(self) -> int:
        """Everything in transit towards this role."""
        return self.shipments.total()

    def orders_in_flight(self) -> int:
        """Everything owed to this role but not yet demanded; 0 for RETAILER."""
        return 0 if self.orders is None else self.orders.total()

    def __eq__(self, other: object) -> bool:
        """Equal on role, stock, cost, decision, bot flag, queue and pipelines.

        Declaring `__eq__` makes the class unhashable, which is correct for a
        mutable object: nothing may key a dict on an agent.
        """
        if not isinstance(other, RoleAgent):
            return NotImplemented
        return (
            self.role == other.role
            and self.inventory == other.inventory
            and self.backlog == other.backlog
            and self.accumulated_cost == other.accumulated_cost
            and self.last_order == other.last_order
            and self.is_bot == other.is_bot
            and self._queued_production() == other._queued_production()
            and self.shipments == other.shipments
            and self.orders == other.orders
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(role={self.role.value}, "
            f"inventory={self.inventory}, backlog={self.backlog}, "
            f"accumulated_cost={self.accumulated_cost!r})"
        )

    def state_payload(self) -> dict:
        """A JSON-safe snapshot holding no `Pipeline` and no nested agent.

        `production_queue` is **always present** and is 0 for the three
        non-Factory roles, so one payload shape round-trips for every role.

        This payload is **unredacted**. It is never sent to a client as-is;
        section 07 builds the per-recipient views.
        """
        return {
            "role": self.role.value,
            "inventory": self.inventory,
            "backlog": self.backlog,
            "accumulated_cost": self.accumulated_cost,
            "last_order": self.last_order,
            "is_bot": self.is_bot,
            "production_queue": self._queued_production(),
            "shipments": self.shipments.to_payload(),
            "orders": None if self.orders is None else self.orders.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: dict, config: GameConfig) -> RoleAgent:
        """Rebuild the agent a `state_payload()` describes, subclass included.

        `from_payload(a.state_payload(), config) == a` must hold: this is how
        an agent survives a round trip through Redis between weeks, including
        mid-cycle, between an `advance()` and its matching push.
        """
        role = Role(payload["role"])
        agent = agent_for(role, config)
        agent.inventory = int(payload["inventory"])
        agent.backlog = int(payload["backlog"])
        agent.accumulated_cost = float(payload["accumulated_cost"])
        last_order = payload["last_order"]
        agent.last_order = None if last_order is None else int(last_order)
        agent.is_bot = bool(payload["is_bot"])
        agent.shipments = Pipeline.from_payload(payload["shipments"])

        orders_payload = payload["orders"]
        if role is Role.RETAILER:
            if orders_payload is not None:
                raise ValueError("RETAILER has no order pipeline to restore.")
            agent.orders = None
        else:
            if orders_payload is None:
                raise ValueError(f"{role.value} is missing its order pipeline.")
            agent.orders = Pipeline.from_payload(orders_payload)

        agent._restore_production_queue(int(payload["production_queue"]))
        agent._check_invariants()
        return agent

    # --- internals --------------------------------------------------------- #

    def _queued_production(self) -> int:
        """Production waiting on capacity. Always 0 away from the Factory."""
        return 0

    def _restore_production_queue(self, qty: int) -> None:
        """Absorb a payload's queue. A non-Factory role has nowhere to put it."""

    def _check_invariants(self) -> None:
        """The invariants of `06-role-agents.md` §3.7 that hold at any moment.

        Invariant 5 (`len(shipments) == shipments.length`) holds only outside a
        settle cycle, and invariant 6 (monotonic cost) is a property over time;
        neither can be checked from a single state, so both are left to the
        engine and the tests.
        """
        assert self.inventory >= 0, "inventory went negative"
        assert self.backlog >= 0, "backlog went negative"
        assert self.inventory * self.backlog == 0, "stock held while units owed"
        assert self._queued_production() >= 0, "production queue went negative"


class RetailerAgent(RoleAgent):
    """Ships to the customer and takes demand directly, with no order pipeline."""


class WholesalerAgent(RoleAgent):
    """Ships to the Retailer, orders from the Distributor."""


class DistributorAgent(RoleAgent):
    """Ships to the Wholesaler, orders from the Factory."""


class FactoryAgent(RoleAgent):
    """Ships to the Distributor and has no supplier: it produces.

    Its `shipments` pipeline *is* the production line, of length
    `production_delay_weeks`, so an unfinished batch and a shipment in transit
    are the same thing to the rest of the game.
    """

    production_queue: int

    def __init__(self, role: Role, config: GameConfig) -> None:
        super().__init__(role, config)
        self._factory_config: FactoryConfig = config.factory_config()
        self.production_queue = 0

    def start_production(self, qty: int) -> ProductionOutcome:
        """Phase C for the Factory: apply capacity, start what fits, queue the rest.

        Excess is **queued, not lost**, and the queue is drained
        first-in-first-out, which adding it to `requested` achieves implicitly.
        A capacity of `None` means unlimited; a capacity of 0 cannot occur
        because section 03 clamps it to 1, and a 0 would queue forever.

        A negative `qty` is legal — `visibility.allow_negative_orders` lets the
        Factory player cancel, and the cancellation is drawn against what is
        already queued, so cancelling 3 with 4 queued is an ordinary week.
        Only a cancellation larger than the queue plus this week's order, which
        would leave `requested` below zero, raises `ValueError`. It is raised
        here, before anything is mutated, so a caller that catches it is not
        left holding a Factory with a negative `production_queue`.
        """
        capacity = self._factory_config.production_capacity_per_week
        requested = qty + self.production_queue
        if requested < 0:
            raise ValueError(
                f"cannot cancel more than is queued: requested {requested} "
                f"from qty {qty} and a queue of {self.production_queue}"
            )
        started = requested if capacity is None else min(requested, capacity)
        queued = requested - started
        self.production_queue = queued
        self.shipments.push(started)

        self._check_invariants()
        return ProductionOutcome(requested=requested, started=started, queued=queued)

    def _queued_production(self) -> int:
        return self.production_queue

    def _restore_production_queue(self, qty: int) -> None:
        self.production_queue = qty


_AGENT_TYPES: dict[Role, type[RoleAgent]] = {
    Role.RETAILER: RetailerAgent,
    Role.WHOLESALER: WholesalerAgent,
    Role.DISTRIBUTOR: DistributorAgent,
    Role.FACTORY: FactoryAgent,
}


def agent_for(role: Role, config: GameConfig) -> RoleAgent:
    """The agent subclass that belongs to `role`, built from `config`."""
    return _AGENT_TYPES[role](role, config)
