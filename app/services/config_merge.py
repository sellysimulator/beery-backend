"""Merging a host's partial config patch over the stored one.

Owned by section 10, which ships first and whose `PUT /rooms/{code}/config`
is the rule's first collection point (`10-rooms-rest-api.md §3.3`). Section
11's `config_update` is the second (`11-socket-lobby.md §3.4`, which requires
"identical merge semantics"), and it **delegates here** rather than
reimplementing.

That is not stylistic. One rule with two collection points and no named owner
is the defect this build has hit four times -- the display-name sanitiser, the
results normaliser, room-code normalisation, and this. Two copies agree on the
day they are written; the divergence arrives with the first edit to either one,
and here it would mean a host's REST save and their socket save producing
different configs from the same patch.
"""

from __future__ import annotations

from typing import Any

__all__ = ["DEEP_MERGE_KEYS", "deep_merge_dicts", "merge_config_patch"]

# Config keys merged one level deeper than a plain overwrite. `demand` is
# absent deliberately: it is handled separately below.
DEEP_MERGE_KEYS = ("roles", "visibility", "bot")


def deep_merge_dicts(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `patch` over `base`, for plain JSON dicts.

    A key present in `patch` whose value is itself a dict, and whose `base`
    counterpart is also a dict, is merged recursively rather than replaced
    wholesale -- this is what keeps `PUT {"roles": {"RETAILER": {...}}}`
    from clobbering the other three roles, and the untouched fields of
    RETAILER itself (`10 §5` failure mode 7).
    """
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def merge_config_patch(stored: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a host's partial config over the stored one (`10 §3.3` step 4).

    Deep for `roles`, `visibility` and `bot`. `demand` is the one exception:
    when the patch changes `kind`, the whole `demand` object is replaced --
    not merged -- with whatever the patch supplied for the new kind, because
    the parameters of one generator are meaningless to another (a leftover
    `step_week` on a `SEASONAL` block, for instance). `GameConfig.
    from_host_input` fills in any field the patch left unspecified with that
    kind's own default, so this never has to know those defaults itself.
    """
    merged = dict(stored)
    for key, value in patch.items():
        if key == "demand" and isinstance(value, dict):
            existing = stored.get("demand")
            existing_kind = existing.get("kind") if isinstance(existing, dict) else None
            new_kind = value.get("kind", existing_kind)
            if new_kind != existing_kind:
                merged["demand"] = dict(value)
            else:
                merged["demand"] = deep_merge_dicts(existing or {}, value)
        elif (
            key in DEEP_MERGE_KEYS
            and isinstance(value, dict)
            and isinstance(stored.get(key), dict)
        ):
            merged[key] = deep_merge_dicts(stored[key], value)
        else:
            merged[key] = value
    return merged
