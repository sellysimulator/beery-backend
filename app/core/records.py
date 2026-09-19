"""The immutable records a role agent produces (`06-role-agents.md` §2).

Pure: this module imports only from `app.core` and the standard library. It
performs no I/O, reads no clock, holds no RNG and does no logging.

Each record is the *result* of one phase of one week for one role, frozen at
the moment it is produced. Nothing here mutates, and nothing here holds a
reference to an agent: a record is safe to keep, to compare and to project into
a payload long after the week it describes has closed.

Money is `float` and is never rounded here (`00-conventions.md` §4). Rounding
to two decimals happens at the persistence and display boundaries only.
"""

from __future__ import annotations

from dataclasses import dataclass

from .enums import Role

__all__ = ["OrderCharge", "ProductionOutcome", "WeekSettlement"]


@dataclass(frozen=True)
class WeekSettlement:
    """The result of Phase A for one role.

    Every field is what a player is shown in the settlement recap. Costs are
    charged on the **closing** inventory and backlog, after shipping
    (`06-role-agents.md` §3.3), so a role that receives exactly what it owes
    and ships it all pays nothing.

    There is deliberately no supply-line figure: a role's shipment pipeline is
    refilled by its upstream neighbour settling, so a snapshot taken inside
    `settle()` would depend on the order roles are visited — the very
    order-dependence two-pass settlement exists to eliminate (`00-decisions.md`
    D9). The engine samples the supply line after every role has settled.
    """

    role: Role
    week: int
    opening_inventory: int
    opening_backlog: int
    arrived: int
    incoming_order: int
    obligation: int  # incoming_order + opening_backlog
    shipped: int
    unfulfilled: int  # obligation - shipped
    closing_inventory: int
    closing_backlog: int
    holding_cost: float
    backlog_cost: float
    carrying_cost: float  # holding_cost + backlog_cost


@dataclass(frozen=True)
class OrderCharge:
    """The result of Phase C for one role.

    Order-related cost lands in the week the order is placed (`00-decisions.md`
    D10), which is why it is a separate record from the settlement recap the
    player saw before deciding.
    """

    role: Role
    week: int
    order: int
    fixed_order_cost: float
    purchase_cost: float
    order_cost: float  # fixed_order_cost + purchase_cost


@dataclass(frozen=True)
class ProductionOutcome:
    """FACTORY only.

    Excess is queued, not lost: `queued` is carried forward indefinitely and is
    drained first-in-first-out by being added to the next week's request.
    """

    requested: int  # order + queue carried in
    started: int  # what entered the production pipeline
    queued: int  # what could not start and waits
