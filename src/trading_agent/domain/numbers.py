"""Float to Decimal at a fixed number of places; via str, so 0.1 stays 0.1."""

from decimal import Decimal


def to_decimal(value: float, places: int = 4) -> Decimal:
    return Decimal(str(round(value, places)))


def optional_decimal(value: float | None, places: int) -> Decimal | None:
    return None if value is None else to_decimal(value, places)
