"""Pure lobby and seating rules (``11-socket-lobby.md``).

Owned solely by section 11 (**D19**). This module holds no I/O of its own:
no Redis, no Socket.IO, no clock beyond what a caller hands it. Handlers in
``app/sockets/handlers/lobby.py`` call it inside ``state_svc.lock(room_code)``
and persist the result themselves.

``RoomService`` is the one place the room document's membership and
eligibility rules are written down. ``lobby.py`` calls it; it does not
restate any of it -- ``can_start`` in particular is evaluated at exactly one
place (``11-socket-lobby.md §2.2``) and consulted, never re-derived, at two
collection points (the lobby's Start button and ``start_game`` itself).
"""

from __future__ import annotations

import hmac
import random

from ..core.config_models import GameConfig
from ..core.enums import ROLE_ORDER, Role, RoleAssignmentMode
from .config_merge import merge_config_patch
from .display_name import sanitise_display_name as _sanitise_display_name

__all__ = ["RoomService", "merge_config_patch"]

_LOCKED_STATES = frozenset({"RUNNING", "PAUSED", "FINISHED", "ABANDONED"})
_ALREADY_RUNNING = "The game is already running."
_ALREADY_FINISHED = "This game has already finished."
_CONFIG_NOT_VALID = "The configuration is not valid yet."

_UNFILLED_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four"}


def _unfilled_reason(unfilled: int) -> str:
    word = _UNFILLED_WORDS.get(unfilled, str(unfilled))
    noun = "role" if unfilled == 1 else "roles"
    verb = "is" if unfilled == 1 else "are"
    return f"{word} {noun} {verb} still empty. Assign them, or turn on bot fill."


def _alias_sort_key(alias: str) -> int:
    try:
        return int(alias[1:])
    except (ValueError, IndexError):
        return 0


class RoomService:
    """Membership, roles and start-eligibility rules. No I/O."""

    # --- host authority --------------------------------------------------- #

    def check_host_secret(self, room: dict, data: dict) -> bool:
        """``hmac.compare_digest`` against ``room["host_secret"]``.

        Returns ``False`` immediately for a missing or empty supplied
        secret, **before** comparing: ``hmac.compare_digest("", "")`` is
        ``True``, so a bare comparison would authorise an empty payload
        against a room whose stored secret is somehow empty.
        """
        stored = room.get("host_secret")
        payload = data if isinstance(data, dict) else {}
        supplied = payload.get("host_secret")
        if not isinstance(stored, str) or not stored:
            return False
        if not isinstance(supplied, str) or not supplied:
            return False
        return hmac.compare_digest(stored, supplied)

    def check_host_identity(self, room: dict, identity: str | None) -> bool:
        """Match only -- never bootstraps a null stored identity (**D18**)."""
        stored = room.get("host_identity")
        if not isinstance(stored, str) or not stored:
            return False
        if not identity:
            return False
        return stored == identity

    # --- alias resolution --------------------------------------------------#

    def find_alias_for_reconnect(
        self, room: dict, session_token: str | None, identity: str | None
    ) -> str | None:
        """Honour ``session_token`` only when its owner's stored identity
        equals the connection's verified identity, so a stolen token cannot
        be replayed by someone else."""
        if not isinstance(session_token, str) or not session_token:
            return None
        if not identity:
            return None
        for alias, participant in room["participants"].items():
            if participant.get("identity") != identity:
                continue
            stored_token = participant.get("session_token")
            if (
                isinstance(stored_token, str)
                and stored_token
                and hmac.compare_digest(stored_token, session_token)
            ):
                return alias
        return None

    def find_alias_for_identity(self, room: dict, identity: str | None) -> str | None:
        """Makes ``join`` idempotent: one identity always maps back to the
        same participant, however many sids it joins from."""
        if not identity:
            return None
        for alias, participant in room["participants"].items():
            if participant.get("identity") == identity:
                return alias
        return None

    # --- display names ------------------------------------------------------#

    def sanitise_display_name(self, raw: str | None, fallback: str) -> str:
        """Delegates to ``app/services/display_name.py`` (section 10)."""
        return _sanitise_display_name(raw, fallback)

    # --- payload shaping ---------------------------------------------------#

    def seats_taken(self, room: dict) -> int:
        """``len(room["participants"])``. Participants, not filled roles."""
        return len(room["participants"])

    def lobby_payload(self, room: dict, seq: int) -> dict:
        can_start, start_blocked_reason = self.can_start(room)
        participants = [
            {
                "alias": alias,
                "display_name": participant["display_name"],
                "role": participant["role"],
                "is_bot": participant["is_bot"],
                "connected": participant["connected"],
                "is_host": False,
            }
            for alias, participant in sorted(
                room["participants"].items(), key=lambda item: _alias_sort_key(item[0])
            )
        ]
        config_payload = room["config"] if isinstance(room["config"], dict) else {}
        role_assignment_mode = config_payload.get(
            "role_assignment_mode", RoleAssignmentMode.HOST_ASSIGNS.value
        )
        return {
            "seq": seq,
            "state": room["state"],
            "host_display_name": room["host_display_name"],
            "participants": participants,
            "role_to_alias": dict(room["role_to_alias"]),
            "role_assignment_mode": role_assignment_mode,
            "seats_total": 4,
            "config_locked": room["state"] in _LOCKED_STATES,
            "can_start": can_start,
            "start_blocked_reason": start_blocked_reason,
        }

    def public_config(self, config: GameConfig, role: Role | None = None) -> dict:
        """The subset every player may see once running: no costs, no
        delays, no demand parameters (``11-socket-lobby.md §3.6``).

        ``role`` is accepted for section 12's use and is ignored here --
        nothing in v1 varies the public subset by role.
        """
        visibility = config.visibility
        return {
            "duration_weeks": config.duration_weeks,
            "stage_count": config.stage_count,
            "currency_symbol": config.currency_symbol,
            "visibility": {
                "show_true_customer_demand_to_all": (
                    visibility.show_true_customer_demand_to_all
                ),
                "show_neighbour_inventory": visibility.show_neighbour_inventory,
                "show_all_inventories": visibility.show_all_inventories,
                "show_supply_line_prominently": visibility.show_supply_line_prominently,
                "show_running_cost_to_players": visibility.show_running_cost_to_players,
                "show_leaderboard_during_game": visibility.show_leaderboard_during_game,
                "max_order_quantity": visibility.max_order_quantity,
                "allow_negative_orders": visibility.allow_negative_orders,
            },
        }

    # --- role dealing --------------------------------------------------------#

    def assign_roles_randomly(self, room: dict, rng: random.Random) -> None:
        """Deal the four roles over the seated participants, in alias order,
        so the deal is reproducible from ``random.Random(f"{seed}:roles")``
        (**D11**).

        With fewer than four seated participants, the shortfall of roles is
        left unassigned for ``start_game``'s bot-fill step to pick up.
        """
        aliases = sorted(room["participants"].keys(), key=_alias_sort_key)
        roles = list(ROLE_ORDER)
        rng.shuffle(roles)
        chosen = roles[: len(aliases)]

        for role in ROLE_ORDER:
            room["role_to_alias"][role.value] = None
        for participant in room["participants"].values():
            participant["role"] = None

        for alias, role in zip(aliases, chosen):
            room["role_to_alias"][role.value] = alias
            room["participants"][alias]["role"] = role.value

    # --- eligibility -----------------------------------------------------------#

    def can_start(self, room: dict) -> tuple[bool, str | None]:
        """The single eligibility rule (``11-socket-lobby.md §2.2``).
        ``start_game`` calls this; it does not restate any part of it."""
        state = room["state"]
        if state in ("RUNNING", "PAUSED"):
            return False, _ALREADY_RUNNING
        if state in ("FINISHED", "ABANDONED"):
            return False, _ALREADY_FINISHED

        try:
            config = GameConfig.from_payload(room["config"])
        except (ValueError, TypeError, KeyError):
            # ValueError covers pydantic's ValidationError; TypeError/KeyError
            # cover a payload that is not even shaped like a mapping of the
            # right fields. "Does not build a GameConfig" is deliberately
            # this broad -- can_start's job is to say no cleanly, not to
            # enumerate every way a stored payload can be malformed.
            return False, _CONFIG_NOT_VALID

        if config.bot_fill_empty_roles:
            return True, None

        if config.role_assignment_mode is RoleAssignmentMode.RANDOM:
            occupied = self.seats_taken(room)
        else:
            occupied = sum(
                1 for alias in room["role_to_alias"].values() if alias is not None
            )
        occupied = min(occupied, 4)
        unfilled = 4 - occupied
        if unfilled <= 0:
            return True, None
        return False, _unfilled_reason(unfilled)
