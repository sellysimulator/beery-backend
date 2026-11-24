"""Enumerations for the Beery domain core.

Pure: this module imports nothing from `app.config`, `app.services`, `app.db`,
`app.sockets` or `app.api`, performs no I/O and reads no clock.

The supply chain runs

    customer <- RETAILER <- WHOLESALER <- DISTRIBUTOR <- FACTORY <- production

so a role's *downstream* neighbour is the one it ships to and its *upstream*
neighbour is the one it orders from (spec §6.2).
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """One of the four stations in the chain."""

    RETAILER = "RETAILER"
    WHOLESALER = "WHOLESALER"
    DISTRIBUTOR = "DISTRIBUTOR"
    FACTORY = "FACTORY"

    @property
    def position(self) -> int:
        """Position in `ROLE_ORDER`: RETAILER 0 .. FACTORY 3."""
        return ROLE_ORDER.index(self)

    @property
    def downstream(self) -> Role | None:
        """The role this one ships to, or `None` for the Retailer.

        The Retailer's customer is the demand series, which is not a role.
        """
        position = self.position
        if position == 0:
            return None
        return ROLE_ORDER[position - 1]

    @property
    def upstream(self) -> Role | None:
        """The role this one orders from, or `None` for the Factory.

        The Factory's supplier is its own production line.
        """
        position = self.position
        if position + 1 >= len(ROLE_ORDER):
            return None
        return ROLE_ORDER[position + 1]


ROLE_ORDER: tuple[Role, ...] = (
    Role.RETAILER,
    Role.WHOLESALER,
    Role.DISTRIBUTOR,
    Role.FACTORY,
)


class RoleAssignmentMode(StrEnum):
    """How the four roles are handed out in the lobby."""

    HOST_ASSIGNS = "HOST_ASSIGNS"
    PLAYER_CHOOSES = "PLAYER_CHOOSES"
    RANDOM = "RANDOM"


class DemandKind(StrEnum):
    """The end-customer demand generator the host picked."""

    CONSTANT = "CONSTANT"
    STEP = "STEP"
    RAMP = "RAMP"
    SEASONAL = "SEASONAL"
    STOCHASTIC = "STOCHASTIC"
    CUSTOM = "CUSTOM"


class Distribution(StrEnum):
    """The draw used by `STOCHASTIC` demand."""

    NORMAL = "NORMAL"
    POISSON = "POISSON"
    UNIFORM = "UNIFORM"


class RoomState(StrEnum):
    """Lifecycle of a room."""

    LOBBY = "LOBBY"
    CONFIGURING = "CONFIGURING"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    ABANDONED = "ABANDONED"
