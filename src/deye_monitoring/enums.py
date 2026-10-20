"""Human readable names for numeric status registers."""

from __future__ import annotations

RUNNING_STATUS = {
    0: "standby",
    1: "selfcheck",
    2: "normal",
    3: "alarm",
    4: "fault",
}

RUNNING_STATUS_ADDRESS = 0x003B

ENUMS: dict[int, dict[int, str]] = {
    RUNNING_STATUS_ADDRESS: RUNNING_STATUS,
}


def describe(address: int, value: float) -> str | None:
    """Return the enum text for *address* and *value*, if a mapping exists."""
    mapping = ENUMS.get(address)
    if mapping is None:
        return None
    return mapping.get(int(value))
