"""The orchestrator (`07-game-engine.md`).

`GameEngine` owns the four agents, the demand series, the week counter and the
phase state machine, and it is the **only** object permitted to mutate game
state.

Pure: this module imports only from `app.core` and the standard library. It
performs no I/O, holds no Redis or database handle, reads no clock and touches
no global RNG. It is constructed from a config and a state payload, driven by
method calls, and returns records that section 12 turns into events and section
14 turns into rows.

The single most important thing in this file is `_run_phase_a`. Settlement is
split into three passes (`00-decisions.md` D9, `07-game-engine.md` §3.2) so that
the result is provably independent of the order roles are visited in. Collapsing
them reintroduces off-by-one-week shipments and an order-dependent supply line.

Money accumulates as `float` and is never rounded here (`00-conventions.md` §4);
quantities are always `int`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from .agents import FactoryAgent, RoleAgent, agent_for
from .config_models import GameConfig
from .demand import generate_demand_series
from .enums import ROLE_ORDER, Role
from .limits import DEFAULT_LIMITS
from .records import OrderCharge, ProductionOutcome, WeekSettlement

__all__ = [
    "EngineStateError",
    "GameEngine",
    "GamePhase",
    "OrderOutOfRangeError",
    "WeekRecord",
]


class GamePhase(str, Enum):
    """Where a game is in its lifecycle.

    A *constructed* engine is never `AWAITING_START`: `start()` returns one in
    `DECISION` and there is no other public constructor. The member exists
    because the room has a pre-start state that section 12 reports as a phase,
    and at that point section 09 stores `"engine": null` — there is no engine to
    be in it.
    """

    AWAITING_START = "AWAITING_START"
    DECISION = "DECISION"  # Phase B — orders are being collected for `week`
    FINISHED = "FINISHED"


@dataclass(frozen=True)
class WeekRecord:
    """One role's complete record for one week.

    Assembled across Phase A and Phase C, and the unit that section 14
    persists.

    `orders_in_flight_after` is this role's **own** order pipeline, sampled in
    Phase A pass 3: orders its downstream customer has placed that are still
    travelling *to* this role. It is deliberately *not* the quantity
    `player_view` sends as `orders_in_flight`, which is the **supplier's**
    pipeline -- this role's own orders going upstream (§3.8). The export names
    the column `incoming_orders_in_flight_after` for that reason
    (`15-results-and-export-api.md` §3.2).

    `total_week_cost` is the whole week: holding and backlog cost plus this
    week's order cost (`fixed_order_cost + purchase_cost`). It was called
    `week_cost` until 2026-11-02, when the screens stopped showing a single
    "week cost" -- they show "Holding & backlog" and "Order cost" separately
    -- and the stored sum was renamed so nobody mistakes it for either.

    `was_bot` says who placed *this week's* order: true when a bot did. A
    week whose seat changed hands while it was open (the host swapped a bot
    in after the player had ordered, or the player took the seat back after
    the bot had) is attributed to whoever's order stood when it closed. A
    forced week, with no order at all, goes to whoever held the seat at the
    close (2026-11-03; it used to read the seat at the close every time).
    """

    role: Role
    week: int
    opening_inventory: int
    opening_backlog: int
    arrived: int
    incoming_order: int
    obligation: int
    shipped: int
    unfulfilled: int
    closing_inventory: int
    closing_backlog: int
    supply_line_after: int
    orders_in_flight_after: int
    order: int
    was_bot: bool
    was_forced: bool  # the host closed the week without this order
    holding_cost: float
    backlog_cost: float
    fixed_order_cost: float
    purchase_cost: float
    total_week_cost: float  # carrying cost + order cost
    cumulative_cost: float
    production_started: int | None  # FACTORY only, else None
    production_queued: int | None  # FACTORY only, else None


class EngineStateError(RuntimeError):
    """A method was called in a phase that does not permit it."""


class OrderOutOfRangeError(ValueError):
    """An order outside `[low, high]`, refused rather than clamped (§3.3).

    The message is written for the player: the socket layer sends it as is.
    """

    low: int
    high: int

    def __init__(self, low: int, high: int) -> None:
        super().__init__(f"Order must be a whole number from {low} to {high}.")
        self.low = low
        self.high = high


def _require_role(value: object) -> Role:
    """Coerce `value` to a `Role`, raising `ValueError` if it is not one."""
    if isinstance(value, Role):
        return value
    try:
        return Role(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{value!r} is not a role.") from exc


def _dataclass_payload(record: Any) -> dict:
    """A JSON-safe dict for a frozen record whose only enum field is `role`."""
    payload = asdict(record)
    payload["role"] = record.role.value
    return payload


def _settlement_from_payload(payload: dict) -> WeekSettlement:
    """Rebuild the `WeekSettlement` that `_dataclass_payload` described.

    A room saved before 2026-11-03 has no `inventory_after_arrival`. Its
    settlement is rebuilt with the stock it had after receiving, which for
    those rooms is exactly the opening stock plus what arrived (`settle()`
    step 1), so a game in flight across the deploy keeps loading.
    """
    opening_inventory = int(payload["opening_inventory"])
    arrived = int(payload["arrived"])
    after_arrival = payload.get("inventory_after_arrival")
    return WeekSettlement(
        role=Role(payload["role"]),
        week=int(payload["week"]),
        opening_inventory=opening_inventory,
        opening_backlog=int(payload["opening_backlog"]),
        arrived=arrived,
        inventory_after_arrival=(
            opening_inventory + arrived if after_arrival is None else int(after_arrival)
        ),
        incoming_order=int(payload["incoming_order"]),
        obligation=int(payload["obligation"]),
        shipped=int(payload["shipped"]),
        unfulfilled=int(payload["unfulfilled"]),
        closing_inventory=int(payload["closing_inventory"]),
        closing_backlog=int(payload["closing_backlog"]),
        holding_cost=float(payload["holding_cost"]),
        backlog_cost=float(payload["backlog_cost"]),
        carrying_cost=float(payload["carrying_cost"]),
    )


def _optional_int(value: Any) -> int | None:
    """`None` passed through, anything else coerced to `int`."""
    return None if value is None else int(value)


def _record_from_payload(payload: dict) -> WeekRecord:
    """Rebuild the `WeekRecord` that `_dataclass_payload` described."""
    return WeekRecord(
        role=Role(payload["role"]),
        week=int(payload["week"]),
        opening_inventory=int(payload["opening_inventory"]),
        opening_backlog=int(payload["opening_backlog"]),
        arrived=int(payload["arrived"]),
        incoming_order=int(payload["incoming_order"]),
        obligation=int(payload["obligation"]),
        shipped=int(payload["shipped"]),
        unfulfilled=int(payload["unfulfilled"]),
        closing_inventory=int(payload["closing_inventory"]),
        closing_backlog=int(payload["closing_backlog"]),
        supply_line_after=int(payload["supply_line_after"]),
        orders_in_flight_after=int(payload["orders_in_flight_after"]),
        order=int(payload["order"]),
        was_bot=bool(payload["was_bot"]),
        was_forced=bool(payload["was_forced"]),
        holding_cost=float(payload["holding_cost"]),
        backlog_cost=float(payload["backlog_cost"]),
        fixed_order_cost=float(payload["fixed_order_cost"]),
        purchase_cost=float(payload["purchase_cost"]),
        # A room saved before the rename still holds `week_cost`.
        total_week_cost=float(
            payload["total_week_cost"]
            if "total_week_cost" in payload
            else payload["week_cost"]
        ),
        cumulative_cost=float(payload["cumulative_cost"]),
        production_started=_optional_int(payload["production_started"]),
        production_queued=_optional_int(payload["production_queued"]),
    )


class GameEngine:
    """The week machine. See `07-game-engine.md` §3."""

    config: GameConfig
    demand_series: list[int]
    seed: int
    week: int  # the week currently open for decisions
    phase: GamePhase
    agents: dict[Role, RoleAgent]
    pending_orders: dict[Role, int]  # orders received for the open week
    pending_by_bot: set[Role]  # which of those a bot placed
    history: list[WeekRecord]
    settlements: dict[Role, WeekSettlement]  # Phase A output for the open week
    role_order: tuple[Role, ...]  # the settlement seam of §3.2

    def __init__(
        self,
        config: GameConfig,
        demand_series: Sequence[int],
        seed: int,
        role_order: Sequence[Role] = ROLE_ORDER,
    ) -> None:
        """Build a blank engine. Not a public constructor.

        It leaves the engine in `AWAITING_START` and with no Phase A run, which
        is a state no caller may observe: `start()` and `from_payload()` are the
        only two entry points, and both move the engine out of it before
        returning (§3.1).
        """
        self.config = config
        self.demand_series = [int(value) for value in demand_series]
        self.seed = int(seed)
        self.role_order = tuple(_require_role(role) for role in role_order)
        self.week = 1
        self.phase = GamePhase.AWAITING_START
        self.agents = {role: agent_for(role, config) for role in ROLE_ORDER}
        self.pending_orders = {}
        self.pending_by_bot = set()
        self.history = []
        self.settlements = {}
        # Phase A pass 3 samples. They feed `WeekRecord.supply_line_after` and
        # `.orders_in_flight_after` and nothing else, so they are not part of
        # `to_payload()`; `from_payload()` re-derives them from the restored
        # agents, which is exact because nothing between pass 3 and Phase C
        # touches a pipeline.
        self._supply_line_after: dict[Role, int] = {}
        self._orders_in_flight_after: dict[Role, int] = {}

    # --- construction ------------------------------------------------------ #

    @classmethod
    def start(
        cls,
        config: GameConfig,
        seed: int,
        bot_roles: frozenset[Role] = frozenset(),
        role_order: Sequence[Role] = ROLE_ORDER,
    ) -> GameEngine:
        """Generate the demand series, build the agents, run Phase A for week 1
        and open the week-1 decision window.

        Returns an engine in `DECISION` phase with `week == 1`.

        `role_order` is the order the three settlement passes visit roles in. It
        exists so that acceptance criterion 4 — the order-independence invariant
        that D9 is for — can be asserted from the public surface instead of by
        reaching into the module. Production always passes the default. It is a
        construction-time choice, is not part of `to_payload()`, and takes no
        part in `__eq__`.
        """
        series = generate_demand_series(config.demand, config.duration_weeks, seed)
        engine = cls(config, series, seed, role_order)
        for role in bot_roles:
            engine.agents[_require_role(role)].is_bot = True
        engine._run_phase_a()
        return engine

    # --- the week ---------------------------------------------------------- #

    def _run_phase_a(self) -> None:
        """Settlement for `self.week`, in three passes (§3.2)  [CRITICAL].

        Pass 1 advances **every** role's two pipelines before **any** role
        settles, so that every shipment pushed in pass 2 lands in a pipeline in
        the same state. Pass 3 samples the supply lines only after the shipment
        lines have been refilled, because a figure read inside `settle()` would
        be one shipment short or not depending purely on iteration order.
        """
        week = self.week
        arrivals: dict[Role, tuple[int, int]] = {}

        # PASS 1 — every role advances both pipelines. Nobody settles.
        for role in self.role_order:
            arriving, incoming = self.agents[role].advance()
            if role is Role.RETAILER:
                # Customer demand reaches the Retailer immediately; it has no
                # order pipeline at all (`06-role-agents.md` §3.1).
                incoming = self.demand_series[week - 1]
            if incoming is None:
                raise EngineStateError(
                    f"{role.value} has no order pipeline and no demand source."
                )
            arrivals[role] = (arriving, incoming)

        # PASS 2 — every role settles and hands its shipment downstream.
        settlements: dict[Role, WeekSettlement] = {}
        for role in self.role_order:
            arriving, incoming = arrivals[role]
            settlement = self.agents[role].settle(week, arriving, incoming)
            settlements[role] = settlement
            downstream = role.downstream
            if downstream is not None:
                self.agents[downstream].receive_shipment(settlement.shipped)
            # RETAILER: the units leave the system to the customer.

        # PASS 3 — sample the pipelines, now the shipment lines are refilled.
        self._sample_pipelines()

        self.settlements = {role: settlements[role] for role in ROLE_ORDER}
        self.pending_orders = {}
        self.pending_by_bot = set()
        self.phase = GamePhase.DECISION

    def _sample_pipelines(self) -> None:
        """Record every role's supply line and own order pipeline."""
        for role in ROLE_ORDER:
            self._supply_line_after[role] = self.agents[role].supply_line()
            self._orders_in_flight_after[role] = self.agents[role].orders_in_flight()

    # --- driving the game -------------------------------------------------- #

    @property
    def weeks_played(self) -> int:
        """Complete weeks in `history` — `len(history) // 4`.

        The single definition of the figure sections 12, 14 and `compute_stats`
        all take as an argument. Nothing else may derive it from `week`, because
        `week` and `weeks_played` differ after `end_early()` (§3.5).
        """
        return len(self.history) // len(ROLE_ORDER)

    def _order_ceiling(self) -> int:
        """The highest order `submit_order` accepts.

        `visibility.max_order_quantity` when set, and the hard ceiling from
        `DEFAULT_LIMITS` always. `GameConfig` carries no `limits`, so the number
        is read rather than written twice; section 03 has already refused a
        visibility value above the same ceiling, so the second bound only ever
        catches a caller, never a config.
        """
        ceiling = DEFAULT_LIMITS.max_order_quantity
        configured = self.config.visibility.max_order_quantity
        if configured is not None:
            ceiling = min(ceiling, configured)
        return ceiling

    def order_bounds(self) -> tuple[int, int]:
        """`(lowest, highest)` order this game accepts, both inclusive.

        The floor is always 0: there are no negative orders (the
        `allow_negative_orders` option was removed on 2026-11-02). The player
        view carries both numbers, so the order form checks exactly what this
        engine enforces.
        """
        return 0, self._order_ceiling()

    def _checked_order(self, qty: int) -> int:
        """Coerce a decision and refuse it when out of range (§3.3).

        Never clamped: a clamped order is an order nobody placed, and the
        player would only find out from a supply line that does not add up.
        """
        raw: Any = qty
        if isinstance(raw, float) and not raw.is_integer():
            raise ValueError(f"order quantity must be an integer, got {qty!r}")
        try:
            quantity = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"order quantity must be an integer, got {qty!r}") from exc
        low, high = self.order_bounds()
        if not low <= quantity <= high:
            raise OrderOutOfRangeError(low, high)
        return quantity

    def submit_order(self, role: Role, qty: int) -> int:
        """Record a decision for the open week.

        Idempotent by role: a second call REPLACES the value. Returns the
        quantity stored, which is always exactly the one passed in.

        Bot roles are not special: section 08's bot produces a quantity and the
        caller submits it exactly as a human's -- the play loop caps the bot's
        own figure at `order_bounds()` first, because a heuristic has nobody to
        show a refusal to. The one thing kept is who placed it: the role's bot
        flag at this moment, which `WeekRecord.was_bot` reports when the week
        closes. A player who takes the seat back and changes the bot's order
        makes the week theirs; one who leaves it stands as the bot's.

        Raises `EngineStateError` unless phase is `DECISION`.
        Raises `OrderOutOfRangeError` (a `ValueError`) for an order outside
        `order_bounds()`, and `ValueError` for a non-integer quantity or a
        role that is not a real role. A refused order changes nothing.
        """
        if self.phase is not GamePhase.DECISION:
            raise EngineStateError(
                f"orders are accepted in {GamePhase.DECISION.value} only, "
                f"not {self.phase.value}."
            )
        target = _require_role(role)
        quantity = self._checked_order(qty)
        self.pending_orders[target] = quantity
        if self.agents[target].is_bot:
            self.pending_by_bot.add(target)
        else:
            self.pending_by_bot.discard(target)
        return quantity

    def all_orders_in(self) -> bool:
        """True when all four roles have a decision for the open week."""
        return all(role in self.pending_orders for role in ROLE_ORDER)

    def close_week(self, force: bool = False) -> list[WeekRecord]:
        """Run Phase C for the open week, then Phase A for the next, and open it.

        Returns the `WeekRecord`s for the week just closed, in `ROLE_ORDER`.

        Raises `EngineStateError` if phase is not `DECISION`, or if orders are
        missing and `force` is False. With `force=True` a missing order is
        recorded as 0 and only that role's record carries `was_forced` (§3.6).

        When the closed week was the last, phase becomes `FINISHED` and no new
        week is opened. **The week counter is not incremented on the final
        close** (§3.4): a finished engine reports `week == duration_weeks`.
        """
        if self.phase is not GamePhase.DECISION:
            raise EngineStateError(
                f"only a week in {GamePhase.DECISION.value} can be closed, "
                f"not one in {self.phase.value}."
            )
        missing = tuple(role for role in ROLE_ORDER if role not in self.pending_orders)
        if missing and not force:
            names = ", ".join(role.value for role in missing)
            raise EngineStateError(
                f"week {self.week} has no order from {names}; "
                f"pass force=True to record 0 for them."
            )

        week = self.week
        charges: dict[Role, OrderCharge] = {}
        production: dict[Role, ProductionOutcome] = {}

        # Phase C — record the decision, then route it (§3.4).
        for role in ROLE_ORDER:
            quantity = self.pending_orders.get(role, 0)
            agent = self.agents[role]
            charges[role] = agent.record_order(week, quantity)
            if isinstance(agent, FactoryAgent):
                production[role] = agent.start_production(quantity)
            else:
                supplier = role.upstream
                if supplier is None:
                    raise EngineStateError(
                        f"{role.value} has no supplier and cannot produce."
                    )
                self.agents[supplier].receive_order(quantity)

        records = [
            self._build_record(role, charges[role], production.get(role), missing)
            for role in ROLE_ORDER
        ]
        self.history.extend(records)
        self.pending_orders = {}
        self.pending_by_bot = set()

        if week >= self.config.duration_weeks:
            self.phase = GamePhase.FINISHED
        else:
            self.week = week + 1
            self._run_phase_a()
        return records

    def _build_record(
        self,
        role: Role,
        charge: OrderCharge,
        outcome: ProductionOutcome | None,
        missing: tuple[Role, ...],
    ) -> WeekRecord:
        """Assemble one role's `WeekRecord` from Phase A and Phase C output."""
        settlement = self.settlements[role]
        agent = self.agents[role]
        return WeekRecord(
            role=role,
            week=settlement.week,
            opening_inventory=settlement.opening_inventory,
            opening_backlog=settlement.opening_backlog,
            arrived=settlement.arrived,
            incoming_order=settlement.incoming_order,
            obligation=settlement.obligation,
            shipped=settlement.shipped,
            unfulfilled=settlement.unfulfilled,
            closing_inventory=settlement.closing_inventory,
            closing_backlog=settlement.closing_backlog,
            supply_line_after=self._supply_line_after[role],
            orders_in_flight_after=self._orders_in_flight_after[role],
            order=charge.order,
            # Whoever placed the order that stands; the seat holder at the
            # close only when nobody did (a forced week).
            was_bot=(
                role in self.pending_by_bot
                if role in self.pending_orders
                else agent.is_bot
            ),
            was_forced=role in missing,
            holding_cost=settlement.holding_cost,
            backlog_cost=settlement.backlog_cost,
            fixed_order_cost=charge.fixed_order_cost,
            purchase_cost=charge.purchase_cost,
            total_week_cost=settlement.carrying_cost + charge.order_cost,
            cumulative_cost=agent.accumulated_cost,
            production_started=None if outcome is None else outcome.started,
            production_queued=None if outcome is None else outcome.queued,
        )

    def end_early(self) -> None:
        """Abandon the open week and move to `FINISHED`.

        The open week has been settled by Phase A but has no decisions, so it
        produces **no** `WeekRecord` and is not counted in `weeks_played`
        (§3.5). A half-week with no orders would otherwise contribute a zero
        order and distort the bullwhip ratio.

        Phase A did charge that week's holding and backlog cost, though, and
        every live total reads `accumulated_cost`. So the charge is rolled back
        out: each role's `accumulated_cost` returns to its last recorded
        `cumulative_cost` (0 when no week closed), restored from the record
        rather than by subtracting, so it is exact. The chain total, a
        balance, the leaderboard -- all derived from it -- then agree with the
        results, which read the same records. Before this, a Classic game ended
        in week 10 showed the host $180 live and $164 on the results.

        Raises `EngineStateError` if phase is already `FINISHED`.
        """
        if self.phase is GamePhase.FINISHED:
            raise EngineStateError("the game has already finished.")
        for role in ROLE_ORDER:
            self.agents[role].accumulated_cost = self._previous_cumulative(role)
        self.phase = GamePhase.FINISHED

    def set_bot(self, role: Role, is_bot: bool = True) -> None:
        """Mark a role as bot-played from now on, or, with `is_bot=False`,
        human-played again. Idempotent, legal in any phase.

        Section 12's `substitute_bot` calls it with True and `reclaim_seat`
        with False: the room document's participant flag is about the seat,
        and only this flag reaches `WeekRecord.was_bot` (through the order
        `submit_order` records next), which is what the export and the results
        screen attribute the play by. Records already in `history` are
        untouched, and so is an order already in for the open week -- it stays
        attributed to whoever placed it.

        Raises `ValueError` if `role` is not a real role.
        """
        self.agents[_require_role(role)].is_bot = bool(is_bot)

    # --- views ------------------------------------------------------------- #

    def _order_arrival_lead_weeks(self, role: Role) -> int:
        """Weeks from ordering now to that order arriving.

        Delegates to `GameConfig.order_arrival_lead_weeks`, the one
        implementation, which the host form's config preview also calls
        (`core.preview`). The engine sends the figure because a client
        assembling it from individual delays gets both ends of the chain wrong
        (§3.8).
        """
        return self.config.order_arrival_lead_weeks(role)

    def _previous_cumulative(self, role: Role) -> float:
        """That role's `cumulative_cost` at the last complete week, else 0.0."""
        record = self._last_record(role)
        return 0.0 if record is None else record.cumulative_cost

    def _last_record(self, role: Role) -> WeekRecord | None:
        """That role's record for the last complete week, or `None`."""
        for record in reversed(self.history):
            if record.role is role:
                return record
        return None

    def _week_cost_figures(self, role: Role) -> tuple[float, float | None]:
        """`(holding & backlog, order cost)` for the week a player is looking at.

        The two are never added together on screen (2026-11-02): they are
        charged at different moments, so one "week cost" was always either
        incomplete or not what the recap showed.

        - While a week is open it is that week's: the holding and backlog
          Phase A charged, and `None` for the order cost, which is charged
          only when the week closes (D10) -- "not yet charged".
        - Once the game is over it is the last closed week's record.
        """
        if self.phase is GamePhase.DECISION:
            settlement = self.settlements.get(role)
            carrying = 0.0 if settlement is None else settlement.carrying_cost
            return carrying, None
        record = self._last_record(role)
        if record is None:
            return 0.0, None
        return (
            record.holding_cost + record.backlog_cost,
            record.fixed_order_cost + record.purchase_cost,
        )

    def _stock_of(self, role: Role) -> dict:
        agent = self.agents[role]
        return {"inventory": agent.inventory, "backlog": agent.backlog}

    def _awaiting_roles(self) -> list[str]:
        return [role.value for role in ROLE_ORDER if role not in self.pending_orders]

    def player_view(self, role: Role, *, with_history: bool = True) -> dict:
        """Everything a player of `role` is entitled to see, and nothing more.

        Redaction happens **here**, server-side, before anything reaches the
        socket layer (`00-conventions.md` §3).

        `with_history=False` leaves `own_history` out. The per-round
        `your_state` uses it: the week's one new record has already gone out
        in `your_week_closed`, and resending every earlier week each round
        grows the traffic with the square of the game's length. The full list
        goes wherever a client may have missed a record -- game start, a
        reconnect, a state request.

        `orders_in_flight` and `orders_in_flight_slots` are what this role has
        ordered and its **supplier** has not yet received — the supplier's order
        pipeline, whose contents are by construction exactly this role's own
        orders. They are `0` and `[]` for the Factory, which has no supplier.
        They are emphatically **not** `agents[role].orders_in_flight()`, which is
        the downstream neighbour's orders travelling towards this role; sending
        that would put next week's incoming demand on the player's screen and
        destroy the information delay the game exists to teach  [CRITICAL].

        No other role's order quantity appears under any visibility setting.
        """
        target = _require_role(role)
        agent = self.agents[target]
        config = self.config
        visibility = config.visibility
        role_config = config.role_config(target)
        settlement = self.settlements.get(target)

        supplier = target.upstream
        supplier_orders = None if supplier is None else self.agents[supplier].orders
        in_flight_slots = [] if supplier_orders is None else supplier_orders.slots()
        in_flight_weeks = (
            [] if supplier_orders is None else supplier_orders.weeks_until_out()
        )
        lead_weeks = self._order_arrival_lead_weeks(target)

        view: dict = {
            "role": target.value,
            "week": self.week,
            "duration_weeks": config.duration_weeks,
            "phase": self.phase.value,
            "currency_symbol": config.currency_symbol,
            "inventory": agent.inventory,
            "backlog": agent.backlog,
            "supply_line": agent.supply_line(),
            "supply_line_slots": agent.shipments.slots(),
            # Weeks until each slot above arrives, aligned with it. The board
            # reads these rather than counting slot positions (2026-11-03).
            "supply_line_slot_weeks": agent.shipments.weeks_until_out(),
            "orders_in_flight": sum(in_flight_slots),
            "orders_in_flight_slots": in_flight_slots,
            "orders_in_flight_slot_weeks": in_flight_weeks,
            "incoming_order": (0 if settlement is None else settlement.incoming_order),
            "last_order": agent.last_order,
            "settlement": (
                None if settlement is None else _dataclass_payload(settlement)
            ),
            "has_submitted": target in self.pending_orders,
            "awaiting_roles": self._awaiting_roles(),
            # The bounds the server actually enforces, never None: the client
            # cannot derive them, and `00-conventions.md` §4 forbids it trying.
            # The order form refuses outside them with the same message.
            "min_order_quantity": self.order_bounds()[0],
            "max_order_quantity": self.order_bounds()[1],
            "show_supply_line_prominently": visibility.show_supply_line_prominently,
            "order_arrival_lead_weeks": lead_weeks,
            # The week an order placed now arrives: the open week plus the lead
            # time, added here so the order form prints a week, not a sum.
            "order_arrival_week": self.week + lead_weeks,
            "production_queue": (
                agent.production_queue if isinstance(agent, FactoryAgent) else None
            ),
            "holding_cost_per_unit_week": role_config.holding_cost_per_unit_week,
            "backlog_cost_per_unit_week": role_config.backlog_cost_per_unit_week,
        }

        if with_history:
            view["own_history"] = [
                _dataclass_payload(record)
                for record in self.history
                if record.role is target
            ]

        if visibility.show_running_cost_to_players:
            view["accumulated_cost"] = agent.accumulated_cost
            # Two figures, never one "week cost": the holding and backlog
            # already charged for the open week, and its order cost, which is
            # `None` ("not yet charged") until the week closes (D10).
            holding_backlog, order_cost = self._week_cost_figures(target)
            view["week_holding_backlog_cost"] = holding_backlog
            view["week_order_cost"] = order_cost
            if role_config.starting_capital > 0:
                view["balance"] = role_config.starting_capital - agent.accumulated_cost

        if visibility.show_true_customer_demand_to_all:
            # Truncated to the weeks played so far: a player must never see
            # future demand.
            view["customer_demand_series"] = self.demand_series[: self.week]

        if visibility.show_neighbour_inventory:
            view["neighbours"] = {
                neighbour.value: self._stock_of(neighbour)
                for neighbour in (target.upstream, target.downstream)
                if neighbour is not None
            }

        if visibility.show_all_inventories:
            view["chain"] = {other.value: self._stock_of(other) for other in ROLE_ORDER}

        if visibility.show_leaderboard_during_game:
            ranked = sorted(
                ROLE_ORDER,
                key=lambda other: (self.agents[other].accumulated_cost, other.index),
            )
            view["leaderboard"] = [
                {
                    "role": other.value,
                    "accumulated_cost": self.agents[other].accumulated_cost,
                }
                for other in ranked
            ]

        return view

    def host_view(self) -> dict:
        """The god view. Unredacted by design, and delivered with `emit_to_sid`.

        Here `orders_in_flight` is the role's **own** order pipeline — the same
        figure as `WeekRecord.orders_in_flight_after`, and not the redacted
        quantity `player_view` sends under that name (§3.8). `production_queue`
        is `0` for the three non-Factory roles rather than `null`, so the host's
        four role cards are one shape. `demand_series` is the full series,
        future weeks included, which is why this view is never broadcast.
        `order_history` maps each role to the `order` of every closed week, in
        week order (`[]` before the first week closes). It is derived from
        `history`, never stored: the open week adds nothing until it closes, a
        forced week carries the `0` the engine recorded, and a bot week the
        bot's recorded order.
        """
        roles: dict = {}
        for role in ROLE_ORDER:
            agent = self.agents[role]
            settlement = self.settlements.get(role)
            roles[role.value] = {
                "inventory": agent.inventory,
                "backlog": agent.backlog,
                "supply_line": agent.supply_line(),
                "orders_in_flight": agent.orders_in_flight(),
                "last_order": agent.last_order,
                "incoming_order": (
                    0 if settlement is None else settlement.incoming_order
                ),
                "accumulated_cost": agent.accumulated_cost,
                "production_queue": int(agent.state_payload()["production_queue"]),
                "has_submitted": role in self.pending_orders,
                "is_bot": agent.is_bot,
            }
        order_history: dict[str, list[int]] = {
            role.value: [r.order for r in self.history if r.role is role]
            for role in ROLE_ORDER
        }
        return {
            "week": self.week,
            "duration_weeks": self.config.duration_weeks,
            # Complete weeks so far -- the figure the results will report and
            # what ending now would keep, since `end_early` drops the open week
            # (§3.5). `week` is one higher while a week is open, and after an
            # early end; the console reads this, never `week` (2026-11-03).
            "weeks_played": self.weeks_played,
            "phase": self.phase.value,
            "currency_symbol": self.config.currency_symbol,
            "demand_series": list(self.demand_series),
            "awaiting_roles": self._awaiting_roles(),
            "chain_total_cost": sum(
                self.agents[role].accumulated_cost for role in ROLE_ORDER
            ),
            "roles": roles,
            "order_history": order_history,
        }

    # --- persistence -------------------------------------------------------- #

    def to_payload(self) -> dict:
        """Full engine state as a JSON-safe dict.

        `config` is **not** included — it is passed separately, because the
        Redis room document stores it once. There is no `bot_roles` key either:
        which roles are bots lives on the agents and rides along in each
        `state_payload()`. `weeks_played` is derived, not stored.
        """
        return {
            "seed": self.seed,
            "demand_series": list(self.demand_series),
            "week": self.week,
            "phase": self.phase.value,
            "pending_orders": {
                role.value: self.pending_orders[role]
                for role in ROLE_ORDER
                if role in self.pending_orders
            },
            "pending_by_bot": [
                role.value for role in ROLE_ORDER if role in self.pending_by_bot
            ],
            "settlements": {
                role.value: _dataclass_payload(self.settlements[role])
                for role in ROLE_ORDER
                if role in self.settlements
            },
            "history": [_dataclass_payload(record) for record in self.history],
            "agents": {
                role.value: self.agents[role].state_payload() for role in ROLE_ORDER
            },
        }

    @classmethod
    def from_payload(cls, payload: dict, config: GameConfig) -> GameEngine:
        """Rebuild the engine a `to_payload()` describes.

        `from_payload(e.to_payload(), cfg) == e` must hold at every phase a
        constructed engine can be in (§3.11). This is how the engine survives
        between socket events.
        """
        engine = cls(config, payload["demand_series"], int(payload["seed"]))
        engine.week = int(payload["week"])
        engine.phase = GamePhase(payload["phase"])

        pending = payload["pending_orders"]
        # A negative pending order can only be a room saved while the removed
        # `allow_negative_orders` option was on. It would raise when the week
        # closes and freeze the room for good, so it is dropped: that role
        # shows as undecided and its player orders again.
        engine.pending_orders = {
            role: int(pending[role.value])
            for role in ROLE_ORDER
            if role.value in pending and int(pending[role.value]) >= 0
        }

        settlements = payload["settlements"]
        engine.settlements = {
            role: _settlement_from_payload(settlements[role.value])
            for role in ROLE_ORDER
            if role.value in settlements
        }

        engine.history = [_record_from_payload(record) for record in payload["history"]]
        engine.agents = {
            role: RoleAgent.from_payload(payload["agents"][role.value], config)
            for role in ROLE_ORDER
        }

        # A room saved before 2026-11-03 has no `pending_by_bot`. Its orders
        # are attributed the way they were then: by the seat's flag now.
        by_bot = payload.get("pending_by_bot")
        if by_bot is None:
            engine.pending_by_bot = {
                role for role in engine.pending_orders if engine.agents[role].is_bot
            }
        else:
            engine.pending_by_bot = {
                role
                for role in ROLE_ORDER
                if role.value in by_bot and role in engine.pending_orders
            }

        engine._sample_pipelines()
        return engine

    # --- comparison --------------------------------------------------------- #

    def __eq__(self, other: object) -> bool:
        """Value equality over exactly the fields `to_payload()` carries.

        `config` is excluded because it is not in the payload, and `role_order`
        because it is a test seam: an engine settled in reverse must compare
        equal to one settled forward, or criterion 4 asserts nothing.
        """
        if not isinstance(other, GameEngine):
            return NotImplemented
        return (
            self.seed == other.seed
            and self.demand_series == other.demand_series
            and self.week == other.week
            and self.phase == other.phase
            and self.pending_orders == other.pending_orders
            and self.pending_by_bot == other.pending_by_bot
            and self.settlements == other.settlements
            and self.history == other.history
            and self.agents == other.agents
        )

    def __repr__(self) -> str:
        return (
            f"GameEngine(week={self.week}, phase={self.phase.value}, "
            f"weeks_played={self.weeks_played}, seed={self.seed})"
        )
