"""Input validation. Turns raw (string) frames into typed frames: Decimal amounts, date objects."""
from decimal import Decimal, InvalidOperation
import pandas as pd

PLATFORM_COLS = ["txn_id", "order_id", "txn_date", "amount", "txn_type", "refund_ref", "currency"]
BANK_COLS = ["settlement_ref", "txn_id", "settlement_date", "amount", "txn_type", "currency"]


class ValidationError(ValueError):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _dec(v):
    try:
        d = Decimal(v)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _clean(df, cols, date_col, name):
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValidationError([f"{name}: missing required columns {missing}"])
    df = df[cols].fillna("").astype(str).apply(lambda s: s.str.strip()).reset_index(drop=True)
    df["txn_type"] = df["txn_type"].str.upper()
    df["currency"] = df["currency"].str.upper()
    problems = []

    def rows(mask):
        return ", ".join(str(i + 2) for i in df.index[mask][:5])  # +2 = CSV line number

    for c in ("txn_id", "currency"):
        if (df[c] == "").any():
            problems.append(f"{name}: blank {c} (CSV lines {rows(df[c] == '')})")
    bad_type = ~df["txn_type"].isin(["PAYMENT", "REFUND"])
    if bad_type.any():
        problems.append(f"{name}: txn_type must be PAYMENT or REFUND (CSV lines {rows(bad_type)})")
    amt = df["amount"].map(_dec)
    bad_amt = amt.isna()
    if bad_amt.any():
        problems.append(f"{name}: non-numeric amount (CSV lines {rows(bad_amt)})")
    else:
        pos = amt.map(lambda x: x > 0).astype(bool)
        zero = amt.map(lambda x: x == 0).astype(bool)
        wrong = ((df["txn_type"] == "PAYMENT") & ~pos) | ((df["txn_type"] == "REFUND") & pos) | zero
        if wrong.any():
            problems.append(f"{name}: PAYMENT amounts must be > 0 and REFUND amounts < 0 (CSV lines {rows(wrong)})")
    dates = pd.to_datetime(df[date_col], format="%Y-%m-%d", errors="coerce")
    if dates.isna().any():
        problems.append(f"{name}: {date_col} must be YYYY-MM-DD (CSV lines {rows(dates.isna())})")
    if problems:
        raise ValidationError(problems)
    df["amount"] = amt
    df[date_col] = dates.dt.date
    return df


def validate_platform(df):
    return _clean(df, PLATFORM_COLS, "txn_date", "platform")


def validate_bank(df):
    return _clean(df, BANK_COLS, "settlement_date", "bank")
