"""The immutable, validated, serialisable game configuration.

`GameConfig` holds every parameter of a game (`beer-game-spec.md` §6, as amended
by `00-decisions.md`). It is built during `CONFIGURING`, frozen at start, carried
in the Redis room document for the life of the game, and written to MySQL at the
end. It knows nothing about players, rooms, sockets or the database.

Pure (`00-conventions.md` §4): this module imports nothing from `app.config`,
`app.services`, `app.db`, `app.sockets` or `app.api`, performs no I/O, and reads
no clock and no global RNG. The hard ceilings of `00-decisions.md` §5 arrive as
an injected `Limits` rather than an import of `app.config`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import DemandKind, Distribution, Role, RoleAssignmentMode

__all__ = [
    "DEFAULT_LIMITS",
    "BotConfig",
    "ConfigValidationError",
    "ConstantDemand",
    "CustomDemand",
    "DemandConfig",
    "FactoryConfig",
    "GameConfig",
    "Limits",
    "RampDemand",
    "RoleConfig",
    "SeasonalDemand",
    "StepDemand",
    "StochasticDemand",
    "VisibilityConfig",
]


class ConfigValidationError(ValueError):
    """Raised by `from_host_input` for a violation that cannot be repaired.

    Carries the dotted path of the offending field and a human-readable message.
    Only the nine violations listed in `03-game-config.md` §3.1 are rejected;
    everything else out of range is clamped, swapped or dropped to `None`.
    """

    field: str

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class Limits(BaseModel):
    """The hard ceilings from `00-decisions.md` §5, injected rather than imported,
    so `app/core` stays free of `app.config`."""

    model_config = ConfigDict(frozen=True)

    max_order_quantity: int = 9_999
    max_weeks: int = 104
    min_weeks: int = 8
    max_delay_weeks: int = 8
    min_delay_weeks: int = 1
    max_initial_quantity: int = 9_999
    max_unit_value: float = 1_000_000.0


DEFAULT_LIMITS: Limits = Limits()


# --------------------------------------------------------------------------- #
# Coercion helpers.  Every host-supplied number funnels through these, so a
# non-finite float is caught once (§3.1 rule 8) and a nonsensical value is
# repaired rather than allowed to poison the simulation.
# --------------------------------------------------------------------------- #

# Sentinel default meaning "this value could not be read as a number at all".
# Safe because `_as_float` raises on a non-finite value it actually parsed, so a
# NaN can only come back out when it went in as this fallback.
_UNUSABLE = float("nan")


def _as_float(value: Any, default: float, path: str) -> float:
    """Coerce to a finite float, falling back to `default` for unusable input."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return default
    else:
        return default
    if not math.isfinite(number):
        raise ConfigValidationError(
            path, f"{path} must be a finite number, not {value!r}."
        )
    return number


def _as_int(value: Any, default: int, path: str) -> int:
    """Coerce to an int, falling back to `default` for unusable input."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        return int(_as_float(value, float(default), path))
    return default


def _optional_int(value: Any, path: str) -> int | None:
    """Coerce to an int, or to `None` when the value is absent or unusable.

    Unlike `_as_int`, an unusable value does **not** fall back to a numeric
    default. `random_seed`'s declared default is `None`, which per **D11** means
    "the server generates a seed at start and persists it", while `0` is a
    *fixed* seed: falling back to `0` would hand every room whose host
    fat-fingered the field the same demand series and the same role deal.

    A non-finite float still raises — `_as_float` checks the value it parsed,
    and only ever returns `_UNUSABLE` as the untouched fallback.
    """
    if value is None:
        return None
    number = _as_float(value, _UNUSABLE, path)
    if math.isnan(number):
        return None
    return int(number)


def _clamp_int(value: Any, low: int, high: int, default: int, path: str) -> int:
    """Clamp a host-supplied integer to `[low, high]`."""
    return max(low, min(high, _as_int(value, default, path)))


def _clamp_float(
    value: Any, low: float, high: float, default: float, path: str
) -> float:
    """Clamp a host-supplied float to `[low, high]`."""
    return max(low, min(high, _as_float(value, default, path)))


def _optional_cap(value: Any, high: int, path: str) -> int | None:
    """Repair an optional `int | None` override.

    `None` means "no per-game override". A value of `<= 0` is a mistake, not a
    cap of one unit per week, so it becomes `None` and the global limit applies
    (`03-game-config.md` §3.1). Anything larger than `high` is clamped down.
    """
    if value is None:
        return None
    number = _as_int(value, 0, path)
    if number <= 0:
        return None
    return min(number, high)


def _as_bool(value: Any, default: bool) -> bool:
    """Coerce to a bool, reading the usual JSON-ish strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def _default_of(model: type[BaseModel], name: str) -> Any:
    """The declared default of `model.name`, so clamping never restates one."""
    return model.model_fields[name].default


def _mapping(value: Any) -> Mapping[str, Any] | None:
    """View a model or mapping as a plain `{field: value}` mapping."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return None


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class RoleConfig(BaseModel):
    """Starting conditions and costs for one role (`beer-game-spec.md` §6.2, §6.3)."""

    model_config = ConfigDict(frozen=True)

    initial_inventory: int = 12
    initial_backlog: int = 0
    shipping_delay_weeks: int = 2
    information_delay_weeks: int = 2
    initial_pipeline_quantity: int = 4
    initial_order_in_pipeline: int = 4
    holding_cost_per_unit_week: float = 0.50
    backlog_cost_per_unit_week: float = 1.00
    fixed_order_cost: float = 0.00
    unit_purchase_cost: float = 0.00
    starting_capital: float = 0.00


class FactoryConfig(RoleConfig):
    """The Factory has no supplier: it produces, with its own delay and cap."""

    model_config = ConfigDict(frozen=True)

    production_delay_weeks: int = 2
    production_capacity_per_week: int | None = None


class BotConfig(BaseModel):
    """Sterman anchor-and-adjust parameters. See 08-bot-agent.md."""

    model_config = ConfigDict(frozen=True)

    theta: float = 0.25  # demand-smoothing weight,  0.0 .. 1.0
    alpha: float = 0.30  # stock-adjustment gain,    0.0 .. 1.0
    beta: float = 0.25  # supply-line weighting,    0.0 .. 1.0
    target_stock_multiplier: float = 3.0  # S* = initial_inventory * this, 0.0 .. 10.0


class VisibilityConfig(BaseModel):
    """Visibility and difficulty levers (`beer-game-spec.md` §6.5)."""

    model_config = ConfigDict(frozen=True)

    show_true_customer_demand_to_all: bool = False
    show_neighbour_inventory: bool = False
    show_all_inventories: bool = False
    show_supply_line_prominently: bool = True
    show_running_cost_to_players: bool = True
    show_leaderboard_during_game: bool = False
    max_order_quantity: int | None = None
    allow_negative_orders: bool = False


class ConstantDemand(BaseModel):
    """Flat demand forever."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[DemandKind.CONSTANT] = DemandKind.CONSTANT
    value: int = 4


class StepDemand(BaseModel):
    """The classic one-time jump."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[DemandKind.STEP] = DemandKind.STEP
    initial_value: int = 4
    step_week: int = 5
    step_value: int = 8


class RampDemand(BaseModel):
    """Gradual increase, optionally capped."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[DemandKind.RAMP] = DemandKind.RAMP
    initial_value: int = 4
    slope_per_week: float = 1.0
    start_week: int = 5
    cap: int | None = None


class SeasonalDemand(BaseModel):
    """Sinusoidal demand."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[DemandKind.SEASONAL] = DemandKind.SEASONAL
    base: int = 8
    amplitude: int = 4
    period_weeks: int = 12
    phase: float = 0.0


class StochasticDemand(BaseModel):
    """Random draws, bounded by `min` and `max`."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[DemandKind.STOCHASTIC] = DemandKind.STOCHASTIC
    distribution: Distribution = Distribution.NORMAL
    mean: float = 8.0
    stdev: float = 2.0
    min: int = 0
    max: int = 20


class CustomDemand(BaseModel):
    """A host-supplied series, at least `duration_weeks` long."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[DemandKind.CUSTOM] = DemandKind.CUSTOM
    values: list[int]


_AnyDemand = (
    ConstantDemand
    | StepDemand
    | RampDemand
    | SeasonalDemand
    | StochasticDemand
    | CustomDemand
)

DemandConfig = Annotated[_AnyDemand, Field(discriminator="kind")]


class _RoleMap(dict[Role, RoleConfig]):
    """A `dict` that refuses to be mutated.

    `GameConfig` is frozen, and `03-game-config.md` §6 failure mode 8 requires
    that `cfg.roles[Role.RETAILER]` cannot be replaced either — a plain dict
    would leave the whole per-role configuration writable behind the freeze.
    """

    _MESSAGE = "GameConfig.roles is immutable."

    def __setitem__(self, key: Role, value: RoleConfig) -> NoReturn:
        raise TypeError(self._MESSAGE)

    def __delitem__(self, key: Role) -> NoReturn:
        raise TypeError(self._MESSAGE)

    def clear(self) -> NoReturn:
        raise TypeError(self._MESSAGE)

    def pop(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError(self._MESSAGE)

    def popitem(self) -> NoReturn:
        raise TypeError(self._MESSAGE)

    def setdefault(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError(self._MESSAGE)

    def update(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError(self._MESSAGE)


class GameConfig(BaseModel):
    """Every parameter of one game, immutable and round-trippable."""

    model_config = ConfigDict(frozen=True)

    duration_weeks: int = 36
    stage_count: Literal[4] = 4
    pause_on_disconnect: bool = True
    bot_fill_empty_roles: bool = False
    random_seed: int | None = None
    currency_symbol: str = "$"
    role_assignment_mode: RoleAssignmentMode = RoleAssignmentMode.HOST_ASSIGNS
    preset_name: str | None = None
    # An immutable mapping (`_RoleMap`), not a plain dict: pydantic's frozen=True
    # blocks attribute assignment only, so a plain dict behind it would leave the
    # whole per-role configuration writable after the freeze (§6 failure mode 8).
    # FACTORY's value is a FactoryConfig.
    roles: Mapping[Role, RoleConfig]
    demand: DemandConfig
    visibility: VisibilityConfig = VisibilityConfig()
    bot: BotConfig = BotConfig()

    # --- validation -------------------------------------------------------- #

    @model_validator(mode="before")
    @classmethod
    def _coerce_role_keys(cls, data: Any) -> Any:
        """Accept string role keys and rebuild the Factory as a `FactoryConfig`.

        `to_payload` writes `"FACTORY": {...}`; without this the Factory would
        come back as a plain `RoleConfig` and the §3.7 round-trip would fail.
        """
        if not isinstance(data, Mapping):
            return data
        roles = data.get("roles")
        if not isinstance(roles, Mapping):
            return data
        coerced: dict[Role, Any] = {}
        for key, value in roles.items():
            role = key if isinstance(key, Role) else Role(key)
            if (
                role is Role.FACTORY
                and isinstance(value, Mapping)
                and not isinstance(value, BaseModel)
            ):
                value = FactoryConfig(**dict(value))
            coerced[role] = value
        return {**data, "roles": coerced}

    @model_validator(mode="after")
    def _check_roles(self) -> GameConfig:
        """Require all four roles, a typed Factory, and freeze the mapping."""
        missing = [role.value for role in Role if role not in self.roles]
        if missing:
            raise ConfigValidationError(
                "roles", f"roles is missing {', '.join(missing)}."
            )
        if not isinstance(self.roles[Role.FACTORY], FactoryConfig):
            raise ConfigValidationError(
                "roles.FACTORY", "roles[FACTORY] must be a FactoryConfig."
            )
        if not isinstance(self.roles, _RoleMap):
            self.__dict__["roles"] = _RoleMap(self.roles)
        return self

    # --- accessors --------------------------------------------------------- #

    def role_config(self, role: Role) -> RoleConfig:
        """The configuration of `role`."""
        return self.roles[role]

    def factory_config(self) -> FactoryConfig:
        """The Factory's configuration, typed."""
        config = self.roles[Role.FACTORY]
        if not isinstance(config, FactoryConfig):
            raise ConfigValidationError(
                "roles.FACTORY", "roles[FACTORY] must be a FactoryConfig."
            )
        return config

    def inbound_delay_weeks(self, role: Role) -> int:
        """Length of `role`'s incoming-shipment pipeline.

        The Factory's inbound pipeline is its production line, so it is
        `production_delay_weeks` rather than `shipping_delay_weeks`.
        """
        if role is Role.FACTORY:
            return self.factory_config().production_delay_weeks
        return self.role_config(role).shipping_delay_weeks

    def order_delay_weeks(self, role: Role) -> int:
        """Length of `role`'s incoming-order pipeline.

        `information_delay_weeks` for all four roles, the Factory included —
        its orders come from the Distributor and still take time to arrive.
        The Retailer's value is carried but unused: customer demand reaches the
        Retailer immediately, so it has no incoming-order pipeline at all
        (`03-game-config.md` §3.2).
        """
        return self.role_config(role).information_delay_weeks

    # --- construction ------------------------------------------------------ #

    @classmethod
    def from_host_input(cls, payload: dict, limits: Limits) -> GameConfig:
        """Build a config from untrusted host input.

        Values outside their range are clamped to the nearest bound, an inverted
        stochastic range is swapped, and a nonsensical optional override becomes
        `None`. Only the nine violations of `03-game-config.md` §3.1 raise.
        """
        # A whole GameConfig goes through to_payload: a plain model_dump would
        # serialise the Factory by its declared type and drop its extra fields.
        raw = (
            payload.to_payload()
            if isinstance(payload, GameConfig)
            else _mapping(payload)
        )
        if raw is None:
            raise ConfigValidationError("config", "The config payload must be a dict.")

        duration_weeks = _clamp_int(
            raw.get("duration_weeks", _default_of(cls, "duration_weeks")),
            limits.min_weeks,
            limits.max_weeks,
            _default_of(cls, "duration_weeks"),
            "duration_weeks",
        )

        currency_symbol = raw.get(
            "currency_symbol", _default_of(cls, "currency_symbol")
        )
        if not isinstance(currency_symbol, str):
            currency_symbol = str(_default_of(cls, "currency_symbol"))
        if len(currency_symbol) > 4:
            raise ConfigValidationError(
                "currency_symbol",
                "currency_symbol must be at most 4 characters.",
            )

        # An unusable seed falls back to None, never to 0: see `_optional_int`.
        random_seed = _optional_int(raw.get("random_seed"), "random_seed")

        preset_raw = raw.get("preset_name")
        preset_name = preset_raw if isinstance(preset_raw, str) else None

        mode_raw = raw.get("role_assignment_mode")
        try:
            mode = (
                RoleAssignmentMode(mode_raw)
                if mode_raw is not None
                else RoleAssignmentMode.HOST_ASSIGNS
            )
        except ValueError:
            mode = RoleAssignmentMode.HOST_ASSIGNS

        return cls(
            duration_weeks=duration_weeks,
            stage_count=4,
            pause_on_disconnect=_as_bool(
                raw.get("pause_on_disconnect"),
                bool(_default_of(cls, "pause_on_disconnect")),
            ),
            bot_fill_empty_roles=_as_bool(
                raw.get("bot_fill_empty_roles"),
                bool(_default_of(cls, "bot_fill_empty_roles")),
            ),
            random_seed=random_seed,
            currency_symbol=currency_symbol,
            role_assignment_mode=mode,
            preset_name=preset_name,
            roles=_clamped_roles(raw.get("roles"), limits),
            demand=_clamped_demand(raw.get("demand"), duration_weeks, limits),
            visibility=_clamped_visibility(raw.get("visibility"), limits),
            bot=_clamped_bot(raw.get("bot")),
        )

    def to_payload(self) -> dict:
        """A plain, JSON-safe dict. `Role` keys and enums become their strings.

        The roles are dumped one by one rather than through the parent model, so
        the Factory's extra fields survive: pydantic serialises a field by its
        *declared* type, which would silently drop them.
        """
        payload: dict[str, Any] = self.model_dump(mode="json", exclude={"roles"})
        payload["roles"] = {
            role.value: config.model_dump(mode="json")
            for role, config in self.roles.items()
        }
        return payload

    @classmethod
    def from_payload(cls, payload: dict) -> GameConfig:
        """The inverse of `to_payload`.

        No clamping: the payload was validated when it was built. A structurally
        invalid payload raises.
        """
        return cls.model_validate(payload)


# --------------------------------------------------------------------------- #
# Clamping of the nested sections
# --------------------------------------------------------------------------- #

_ROLE_QUANTITY_FIELDS = (
    "initial_inventory",
    "initial_backlog",
    "initial_pipeline_quantity",
    "initial_order_in_pipeline",
)
_ROLE_DELAY_FIELDS = ("shipping_delay_weeks", "information_delay_weeks")
_ROLE_COST_FIELDS = (
    "holding_cost_per_unit_week",
    "backlog_cost_per_unit_week",
    "fixed_order_cost",
    "unit_purchase_cost",
    "starting_capital",
)


def _clamped_role(
    raw: Mapping[str, Any], role: Role, limits: Limits
) -> RoleConfig | FactoryConfig:
    """Clamp one role's numbers into range."""
    path = f"roles.{role.value}"
    model: type[RoleConfig] = FactoryConfig if role is Role.FACTORY else RoleConfig
    fields: dict[str, Any] = {}
    for name in _ROLE_QUANTITY_FIELDS:
        fields[name] = _clamp_int(
            raw.get(name, _default_of(model, name)),
            0,
            limits.max_initial_quantity,
            _default_of(model, name),
            f"{path}.{name}",
        )
    for name in _ROLE_DELAY_FIELDS:
        fields[name] = _clamp_int(
            raw.get(name, _default_of(model, name)),
            limits.min_delay_weeks,
            limits.max_delay_weeks,
            _default_of(model, name),
            f"{path}.{name}",
        )
    for name in _ROLE_COST_FIELDS:
        fields[name] = _clamp_float(
            raw.get(name, _default_of(model, name)),
            0.0,
            limits.max_unit_value,
            _default_of(model, name),
            f"{path}.{name}",
        )
    if role is not Role.FACTORY:
        return RoleConfig(**fields)
    fields["production_delay_weeks"] = _clamp_int(
        raw.get(
            "production_delay_weeks",
            _default_of(FactoryConfig, "production_delay_weeks"),
        ),
        limits.min_delay_weeks,
        limits.max_delay_weeks,
        _default_of(FactoryConfig, "production_delay_weeks"),
        f"{path}.production_delay_weeks",
    )
    fields["production_capacity_per_week"] = _optional_cap(
        raw.get("production_capacity_per_week"),
        limits.max_order_quantity,
        f"{path}.production_capacity_per_week",
    )
    return FactoryConfig(**fields)


def _clamped_roles(raw: Any, limits: Limits) -> dict[Role, RoleConfig]:
    """Clamp all four roles, rejecting a missing one or an untyped Factory."""
    if not isinstance(raw, Mapping):
        raise ConfigValidationError("roles", "roles must hold all four roles.")
    supplied: dict[Role, Any] = {}
    for key, value in raw.items():
        try:
            role = key if isinstance(key, Role) else Role(key)
        except ValueError:
            continue
        supplied[role] = value
    missing = [role.value for role in Role if role not in supplied]
    if missing:
        raise ConfigValidationError("roles", f"roles is missing {', '.join(missing)}.")
    factory_raw = supplied[Role.FACTORY]
    if isinstance(factory_raw, BaseModel) and not isinstance(
        factory_raw, FactoryConfig
    ):
        raise ConfigValidationError(
            "roles.FACTORY", "roles[FACTORY] must be a FactoryConfig."
        )
    roles: dict[Role, RoleConfig] = {}
    for role in Role:
        fields = _mapping(supplied[role])
        if fields is None:
            raise ConfigValidationError(
                f"roles.{role.value}", f"roles[{role.value}] must be a dict."
            )
        roles[role] = _clamped_role(fields, role, limits)
    return roles


def _clamped_visibility(raw: Any, limits: Limits) -> VisibilityConfig:
    """Clamp the visibility levers; a nonsensical order cap becomes `None`."""
    fields = _mapping(raw) or {}
    values: dict[str, Any] = {
        name: _as_bool(
            fields.get(name, _default_of(VisibilityConfig, name)),
            bool(_default_of(VisibilityConfig, name)),
        )
        for name in (
            "show_true_customer_demand_to_all",
            "show_neighbour_inventory",
            "show_all_inventories",
            "show_supply_line_prominently",
            "show_running_cost_to_players",
            "show_leaderboard_during_game",
            "allow_negative_orders",
        )
    }
    values["max_order_quantity"] = _optional_cap(
        fields.get("max_order_quantity"),
        limits.max_order_quantity,
        "visibility.max_order_quantity",
    )
    return VisibilityConfig(**values)


def _clamped_bot(raw: Any) -> BotConfig:
    """Clamp the Sterman parameters; `BotConfig` is never rejected (§3.5)."""
    fields = _mapping(raw) or {}
    values = {
        name: _clamp_float(
            fields.get(name, _default_of(BotConfig, name)),
            0.0,
            1.0,
            _default_of(BotConfig, name),
            f"bot.{name}",
        )
        for name in ("theta", "alpha", "beta")
    }
    values["target_stock_multiplier"] = _clamp_float(
        fields.get(
            "target_stock_multiplier", _default_of(BotConfig, "target_stock_multiplier")
        ),
        0.0,
        10.0,
        _default_of(BotConfig, "target_stock_multiplier"),
        "bot.target_stock_multiplier",
    )
    return BotConfig(**values)


def _demand_int(
    raw: Mapping[str, Any], name: str, model: type[BaseModel], high: int
) -> int:
    """Clamp one non-negative demand quantity."""
    return _clamp_int(
        raw.get(name, _default_of(model, name)),
        0,
        high,
        _default_of(model, name),
        f"demand.{name}",
    )


def _clamped_demand(raw: Any, duration_weeks: int, limits: Limits) -> _AnyDemand:
    """Build the demand generator, clamping magnitudes and swapping an inverted
    stochastic range. Rules 1 to 5 of §3.1 are rejected here."""
    fields = _mapping(raw)
    if fields is None:
        raise ConfigValidationError("demand", "demand must be a dict naming a kind.")
    try:
        kind = DemandKind(fields.get("kind"))
    except ValueError:
        raise ConfigValidationError(
            "demand.kind", f"Unknown demand kind {fields.get('kind')!r}."
        ) from None
    cap = limits.max_order_quantity

    if kind is DemandKind.CONSTANT:
        return ConstantDemand(value=_demand_int(fields, "value", ConstantDemand, cap))

    if kind is DemandKind.STEP:
        step_week = _as_int(
            fields.get("step_week", _default_of(StepDemand, "step_week")),
            _default_of(StepDemand, "step_week"),
            "demand.step_week",
        )
        if not 2 <= step_week <= duration_weeks:
            raise ConfigValidationError(
                "demand.step_week",
                f"step_week must be between 2 and {duration_weeks}.",
            )
        return StepDemand(
            initial_value=_demand_int(fields, "initial_value", StepDemand, cap),
            step_week=step_week,
            step_value=_demand_int(fields, "step_value", StepDemand, cap),
        )

    if kind is DemandKind.RAMP:
        start_week = _as_int(
            fields.get("start_week", _default_of(RampDemand, "start_week")),
            _default_of(RampDemand, "start_week"),
            "demand.start_week",
        )
        if not 1 <= start_week <= duration_weeks:
            raise ConfigValidationError(
                "demand.start_week",
                f"start_week must be between 1 and {duration_weeks}.",
            )
        return RampDemand(
            initial_value=_demand_int(fields, "initial_value", RampDemand, cap),
            slope_per_week=_clamp_float(
                fields.get("slope_per_week", _default_of(RampDemand, "slope_per_week")),
                -float(cap),
                float(cap),
                _default_of(RampDemand, "slope_per_week"),
                "demand.slope_per_week",
            ),
            start_week=start_week,
            cap=(
                None
                if fields.get("cap") is None
                else _clamp_int(fields.get("cap"), 0, cap, cap, "demand.cap")
            ),
        )

    if kind is DemandKind.SEASONAL:
        period_weeks = _as_int(
            fields.get("period_weeks", _default_of(SeasonalDemand, "period_weeks")),
            _default_of(SeasonalDemand, "period_weeks"),
            "demand.period_weeks",
        )
        if period_weeks < 2:
            raise ConfigValidationError(
                "demand.period_weeks", "period_weeks must be at least 2."
            )
        return SeasonalDemand(
            base=_demand_int(fields, "base", SeasonalDemand, cap),
            amplitude=_demand_int(fields, "amplitude", SeasonalDemand, cap),
            period_weeks=period_weeks,
            phase=_as_float(
                fields.get("phase", _default_of(SeasonalDemand, "phase")),
                _default_of(SeasonalDemand, "phase"),
                "demand.phase",
            ),
        )

    if kind is DemandKind.STOCHASTIC:
        distribution_raw = fields.get("distribution")
        try:
            distribution = (
                Distribution(distribution_raw)
                if distribution_raw is not None
                else Distribution.NORMAL
            )
        except ValueError:
            distribution = Distribution.NORMAL
        low = _demand_int(fields, "min", StochasticDemand, cap)
        high = _demand_int(fields, "max", StochasticDemand, cap)
        if low > high:
            # An inverted range makes random.randint(min, max) raise on every
            # subsequent week, wedging the room permanently.  [HARD-WON]
            low, high = high, low
        return StochasticDemand(
            distribution=distribution,
            mean=_clamp_float(
                fields.get("mean", _default_of(StochasticDemand, "mean")),
                0.0,
                float(cap),
                _default_of(StochasticDemand, "mean"),
                "demand.mean",
            ),
            stdev=_clamp_float(
                fields.get("stdev", _default_of(StochasticDemand, "stdev")),
                0.0,
                float(cap),
                _default_of(StochasticDemand, "stdev"),
                "demand.stdev",
            ),
            min=low,
            max=high,
        )

    values_raw = fields.get("values")
    if isinstance(values_raw, (str, bytes)) or not isinstance(values_raw, Sequence):
        raise ConfigValidationError(
            "demand.values", "CUSTOM demand needs a list of integers."
        )
    # A numeric string is a number here: beer-game-spec.md §6.4 makes paste and
    # CSV upload the input path for a custom series, so every entry arrives as a
    # string. "4" reads as 4, 4.7 truncates to 4, a bool reads as 0/1, and only a
    # genuinely non-numeric entry rejects. Entries are stored as int.
    values: list[int] = []
    for item in values_raw:
        number = _as_float(item, _UNUSABLE, "demand.values")
        if math.isnan(number):
            raise ConfigValidationError(
                "demand.values",
                f"CUSTOM demand values must be numbers, not {item!r}.",
            )
        # Rule 2 is evaluated on the parsed number, *before* truncation. An
        # entry in (-1, 0) truncates toward zero to 0, so truncating first
        # would repair an unambiguously negative entry into valid demand the
        # host never asked for.
        if number < 0:
            raise ConfigValidationError(
                "demand.values",
                f"CUSTOM demand values must not be negative, got {item!r}.",
            )
        values.append(int(number))
    if len(values) < duration_weeks:
        raise ConfigValidationError(
            "demand.values",
            f"CUSTOM demand needs at least {duration_weeks} values, got {len(values)}.",
        )
    return CustomDemand(values=[min(value, cap) for value in values])
