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
from .config_models import DEFAULT_LIMITS, GameConfig
from .demand import generate_demand_series
from .enums import ROLE_ORDER, Role
from .records import OrderCharge, ProductionOutcome, WeekSettlement

__all__ = ["EngineStateError", "GameEngine", "GamePhase", "WeekRecord"]


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
    Phase A pass 3. It is deliberately *not* the quantity `player_view` sends
    under the same name, which is the **supplier's** pipeline (§3.8).
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
    week_cost: float
    cumulative_cost: float
    production_started: int | None  # FACTORY only, else None
    production_queued: int | None  # FACTORY only, else None


class EngineStateError(RuntimeError):
    """A method was called in a phase that does not permit it."""


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
    """Rebuild the `WeekSettlement` that `_dataclass_payload` described."""
    return WeekSettlement(
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
        week_cost=float(payload["week_cost"]),
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
        """The ceiling `submit_order` enforces.

        `visibility.max_order_quantity` when set, and the hard ceiling from
        `DEFAULT_LIMITS` always. `GameConfig` carries no `limits`, so the number
        is read rather than written twice; section 03 has already clamped the
        visibility value to the same ceiling, so the second bound only ever
        catches a caller, never a config.
        """
        ceiling = DEFAULT_LIMITS.max_order_quantity
        configured = self.config.visibility.max_order_quantity
        if configured is not None:
            ceiling = min(ceiling, configured)
        return ceiling

    def _clamp_order(self, qty: int) -> int:
        """Coerce and clamp a decision, in the order of §3.3."""
        raw: Any = qty
        if isinstance(raw, float) and not raw.is_integer():
            raise ValueError(f"order quantity must be an integer, got {qty!r}")
        try:
            quantity = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"order quantity must be an integer, got {qty!r}") from exc
        ceiling = self._order_ceiling()
        floor = -ceiling if self.config.visibility.allow_negative_orders else 0
        return max(floor, min(ceiling, quantity))

    def submit_order(self, role: Role, qty: int) -> int:
        """Record a decision for the open week.

        Idempotent by role: a second call REPLACES the value. Returns the
        clamped quantity actually stored, so the caller can tell the player what
        was recorded.

        Bot roles are not special: section 08's bot produces a quantity and the
        caller submits it exactly as a human's.

        Raises `EngineStateError` unless phase is `DECISION`.
        Raises `ValueError` if `role` is not a real role.
        """
        if self.phase is not GamePhase.DECISION:
            raise EngineStateError(
                f"orders are accepted in {GamePhase.DECISION.value} only, "
                f"not {self.phase.value}."
            )
        target = _require_role(role)
        quantity = self._clamp_order(qty)
        self.pending_orders[target] = quantity
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
            was_bot=agent.is_bot,
            was_forced=role in missing,
            holding_cost=settlement.holding_cost,
            backlog_cost=settlement.backlog_cost,
            fixed_order_cost=charge.fixed_order_cost,
            purchase_cost=charge.purchase_cost,
            week_cost=settlement.carrying_cost + charge.order_cost,
            cumulative_cost=agent.accumulated_cost,
            production_started=None if outcome is None else outcome.started,
            production_queued=None if outcome is None else outcome.queued,
        )

    def end_early(self) -> None:
        """Abandon the open week without settling it and move to `FINISHED`.

        The open week has been settled by Phase A but has no decisions, so it
        produces **no** `WeekRecord` and is not counted in `weeks_played`
        (§3.5). A half-week with no orders would otherwise contribute a zero
        order and distort the bullwhip ratio.

        Raises `EngineStateError` if phase is already `FINISHED`.
        """
        if self.phase is GamePhase.FINISHED:
            raise EngineStateError("the game has already finished.")
        self.phase = GamePhase.FINISHED

    def set_bot(self, role: Role, is_bot: bool = True) -> None:
        """Mark a role as bot-played from now on. Idempotent, legal in any phase.

        This is what section 12's `substitute_bot` calls: the room document's
        participant flag is about the seat, and only this flag reaches
        `WeekRecord.was_bot`, which is what the export and the results screen
        attribute the play by. Records already in `history` are untouched.

        Raises `ValueError` if `role` is not a real role.
        """
        self.agents[_require_role(role)].is_bot = bool(is_bot)

    # --- views ------------------------------------------------------------- #

    def _order_arrival_lead_weeks(self, role: Role) -> int:
        """Weeks from ordering now to that order arriving.

        An order spends the **supplier's** information delay reaching them and
        the goods spend this role's own shipping delay coming back. The Factory
        has no information delay at all: its production delay is the whole lead
        time. The engine computes it because a client assembling it from
        individual delays gets both ends of the chain wrong (§3.8).
        """
        supplier = role.upstream
        inbound = self.config.inbound_delay_weeks(role)
        if supplier is None:
            return inbound
        return self.config.order_delay_weeks(supplier) + inbound

    def _previous_cumulative(self, role: Role) -> float:
        """That role's `cumulative_cost` at the last complete week, else 0.0."""
        for record in reversed(self.history):
            if record.role is role:
                return record.cumulative_cost
        return 0.0

    def _stock_of(self, role: Role) -> dict:
        agent = self.agents[role]
        return {"inventory": agent.inventory, "backlog": agent.backlog}

    def _awaiting_roles(self) -> list[str]:
        return [role.value for role in ROLE_ORDER if role not in self.pending_orders]

    def player_view(self, role: Role) -> dict:
        """Everything a player of `role` is entitled to see, and nothing more.

        Redaction happens **here**, server-side, before anything reaches the
        socket layer (`00-conventions.md` §3).

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
            "orders_in_flight": sum(in_flight_slots),
            "orders_in_flight_slots": in_flight_slots,
            "incoming_order": (0 if settlement is None else settlement.incoming_order),
            "last_order": agent.last_order,
            "settlement": (
                None if settlement is None else _dataclass_payload(settlement)
            ),
            "has_submitted": target in self.pending_orders,
            "awaiting_roles": self._awaiting_roles(),
            "own_history": [
                _dataclass_payload(record)
                for record in self.history
                if record.role is target
            ],
            # The ceiling the server actually enforces, never None: the client
            # cannot derive it, and `00-conventions.md` §4 forbids it trying.
            "max_order_quantity": self._order_ceiling(),
            "allow_negative_orders": visibility.allow_negative_orders,
            "show_supply_line_prominently": visibility.show_supply_line_prominently,
            "order_arrival_lead_weeks": self._order_arrival_lead_weeks(target),
            "production_queue": (
                agent.production_queue if isinstance(agent, FactoryAgent) else None
            ),
            "holding_cost_per_unit_week": role_config.holding_cost_per_unit_week,
            "backlog_cost_per_unit_week": role_config.backlog_cost_per_unit_week,
        }

        if visibility.show_running_cost_to_players:
            view["accumulated_cost"] = agent.accumulated_cost
            # What has been charged since the last week closed: the carrying
            # portion while the week is open, because the order portion is not
            # charged until the week closes (D10).
            view["week_cost"] = agent.accumulated_cost - self._previous_cumulative(
                target
            )
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
        return {
            "week": self.week,
            "duration_weeks": self.config.duration_weeks,
            "phase": self.phase.value,
            "currency_symbol": self.config.currency_symbol,
            "demand_series": list(self.demand_series),
            "awaiting_roles": self._awaiting_roles(),
            "chain_total_cost": sum(
                self.agents[role].accumulated_cost for role in ROLE_ORDER
            ),
            "roles": roles,
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
        engine.pending_orders = {
            role: int(pending[role.value])
            for role in ROLE_ORDER
            if role.value in pending
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
            and self.settlements == other.settlements
            and self.history == other.history
            and self.agents == other.agents
        )

    def __repr__(self) -> str:
        return (
            f"GameEngine(week={self.week}, phase={self.phase.value}, "
            f"weeks_played={self.weeks_played}, seed={self.seed})"
        )
