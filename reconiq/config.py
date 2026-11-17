from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Config:
    period_start: date = date(2026, 11, 1)
    period_end: date = date(2026, 11, 30)
    item_tolerance: Decimal = Decimal("0.01")        # max |bank - platform| treated as rounding
    aggregate_tolerance: Decimal = Decimal("0.05")   # max |net rounding| before it needs an adjustment
    settlement_window_days: int = 2                  # business days the bank may take to settle
