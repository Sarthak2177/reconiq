"""Reconciliation engine. Pure functions, no UI. All money is Decimal."""
from dataclasses import dataclass
from decimal import Decimal
import numpy as np
import pandas as pd
from .config import Config
from .validation import validate_platform, validate_bank

D0 = Decimal(0)
MATCHED, ROUNDING, AMOUNT, CCY = "MATCHED", "ROUNDING_DIFF", "AMOUNT_MISMATCH", "CURRENCY_MISMATCH"
NEXT, PENDING, MISSING = "TIMING_NEXT_MONTH", "TIMING_PENDING", "MISSING_SETTLEMENT"
DUP_P, DUP_B, ORPHAN, BANK_ONLY, CARRY = "DUPLICATE_PLATFORM", "DUPLICATE_BANK", "ORPHAN_REFUND", "BANK_ONLY", "CARRY_IN"
LABELS = {MATCHED: "Matched", ROUNDING: "Rounding difference", AMOUNT: "Amount mismatch",
          CCY: "Currency mismatch", NEXT: "Settled next month", PENDING: "Pending (within window)",
          MISSING: "Missing settlement", DUP_P: "Duplicate (platform)", DUP_B: "Duplicate (bank)",
          ORPHAN: "Refund without original", BANK_ONLY: "Bank record, no transaction", CARRY: "Carried in from prior month"}
NO_ACTION = {MATCHED, CARRY, NEXT, PENDING}
COLS = ["source", "txn_id", "order_id", "txn_type", "txn_date", "settlement_date", "settlement_ref", "currency",
        "platform_amount", "bank_amount", "difference", "status", "needs_action", "bucket", "diff_kind", "reason"]
BRIDGE_LABELS = {
    "platform_total": "Platform total (period, de-duplicated)",
    "in_transit": "Less: settled after period end (timing)",
    "missing": "Less: no settlement received",
    "carry_in": "Add: prior-month transactions settled in period",
    "rounding": "Rounding differences (net)",
    "other_diff": "Other amount differences",
    "dup_bank": "Add: duplicate bank lines",
    "bank_only": "Add: bank lines with no transaction",
    "expected_bank": "Expected bank total",
    "bank_total": "Actual bank total",
    "unexplained": "Unexplained residual",
}


@dataclass
class Result:
    records: pd.DataFrame
    bridge: dict
    summary: dict
    platform: pd.DataFrame
    bank: pd.DataFrame

    @property
    def exceptions(self):
        return self.records[~self.records.status.isin([MATCHED, CARRY])]


def _compare(plat, bk, cfg):
    diff = bk["amount"] - plat["amount"]
    if bk["currency"] != plat["currency"]:
        return diff, "mismatch", CCY
    if diff == 0:
        return diff, "exact", MATCHED
    if abs(diff) <= cfg.item_tolerance:
        return diff, "rounding", ROUNDING
    return diff, "mismatch", AMOUNT


def _rec(plat=None, bk=None, **kw):
    r = dict.fromkeys(COLS)
    if bk:
        r.update(txn_id=bk["txn_id"], txn_type=bk["txn_type"], currency=bk["currency"], settlement_ref=bk["settlement_ref"],
                 settlement_date=bk["settlement_date"], bank_amount=bk["amount"])
    if plat:
        r.update(txn_id=plat["txn_id"], order_id=plat["order_id"], txn_type=plat["txn_type"], txn_date=plat["txn_date"],
                 currency=plat["currency"], platform_amount=plat["amount"])
    r["source"] = "PLATFORM" if plat else "BANK"
    r.update(kw)
    return r


def _sum(df, mask, col):
    return sum(df.loc[mask, col].tolist(), D0)


def reconcile(platform_raw, bank_raw, cfg=None):
    cfg = cfg or Config()
    p_all, b_all = validate_platform(platform_raw), validate_bank(bank_raw)
    p_dupmask, b_dupmask = p_all.duplicated("txn_id"), b_all.duplicated("txn_id")
    p, p_dups, b, b_dups = p_all[~p_dupmask], p_all[p_dupmask], b_all[~b_dupmask], b_all[b_dupmask]
    known = set(p.txn_id)
    bank_map = {r["txn_id"]: r for r in b.to_dict("records")}
    inp = lambda d: cfg.period_start <= d <= cfg.period_end
    out = []

    for r in p.to_dict("records"):
        d, bk = r["txn_date"], bank_map.get(r["txn_id"])
        if d > cfg.period_end:
            continue
        settled_in = bk is not None and inp(bk["settlement_date"])
        if d < cfg.period_start:  # prior period: only relevant if the bank paid it in this period
            if settled_in:
                diff, kind, _ = _compare(r, bk, cfg)
                out.append(_rec(r, bk, status=CARRY, bucket="carry_in", difference=diff, diff_kind=kind,
                                reason=f"Platform txn dated {d} (prior month) settled by bank on {bk['settlement_date']}."))
            continue
        diff, kind = None, None
        if bk is None:
            exp = np.busday_offset(d, cfg.settlement_window_days, roll="forward").item()
            if exp > cfg.period_end:
                status, bucket = PENDING, "in_transit"
                reason = f"Not settled yet; still inside the {cfg.settlement_window_days}-business-day window (expected by {exp})."
            else:
                status, bucket = MISSING, "missing"
                reason = f"No bank settlement found; expected by {exp}, which is on or before period end."
        elif bk["settlement_date"] > cfg.period_end:
            status, bucket = NEXT, "in_transit"
            reason = f"Valid transaction dated {d}; bank settled it on {bk['settlement_date']}, after period end. Timing difference, not an error."
        else:
            bucket = "settled"
            diff, kind, status = _compare(r, bk, cfg)
            reason = {MATCHED: "Amount and currency match the bank record.",
                      ROUNDING: f"Bank differs by {diff}, within the per-item tolerance of {cfg.item_tolerance}.",
                      AMOUNT: f"Bank differs by {diff}, above the per-item tolerance of {cfg.item_tolerance}.",
                      CCY: f"Currency differs (platform {r['currency']}, bank {bk['currency']})."}[status]
        if r["txn_type"] == "REFUND" and r["refund_ref"] not in known:
            status = ORPHAN
            reason = f"Refund references '{r['refund_ref'] or '(blank)'}', which is not a known platform transaction."
        out.append(_rec(r, bk, status=status, bucket=bucket, difference=diff, diff_kind=kind, reason=reason))

    for r in p_dups.to_dict("records"):
        out.append(_rec(r, None, status=DUP_P, bucket="dup_platform",
                        reason="txn_id appears more than once in the platform file; extra copy excluded from totals."))
    for bk in b_dups.to_dict("records"):
        if inp(bk["settlement_date"]):
            out.append(_rec(None, bk, status=DUP_B, bucket="dup_bank",
                            reason="Bank settled this txn_id more than once; the extra line inflates the bank total."))
    for bk in b.to_dict("records"):
        if bk["txn_id"] not in known and inp(bk["settlement_date"]):
            out.append(_rec(None, bk, status=BANK_ONLY, bucket="bank_only",
                            reason="Bank settlement with no matching platform transaction."))

    df = pd.DataFrame(out, columns=COLS)
    df["needs_action"] = ~df.status.isin(NO_ACTION | {ROUNDING})
    settled = df.bucket.isin(["settled", "carry_in"])
    bridge = {
        "platform_total": _sum(df, df.bucket.isin(["settled", "in_transit", "missing"]), "platform_amount"),
        "in_transit": -_sum(df, df.bucket == "in_transit", "platform_amount"),
        "missing": -_sum(df, df.bucket == "missing", "platform_amount"),
        "carry_in": _sum(df, df.bucket == "carry_in", "platform_amount"),
        "rounding": _sum(df, settled & (df.diff_kind == "rounding"), "difference"),
        "other_diff": _sum(df, settled & (df.diff_kind == "mismatch"), "difference"),
        "dup_bank": _sum(df, df.bucket == "dup_bank", "bank_amount"),
        "bank_only": _sum(df, df.bucket == "bank_only", "bank_amount"),
    }
    bridge["expected_bank"] = sum(bridge.values(), D0)
    bridge["bank_total"] = sum([x for x, d in zip(b_all.amount, b_all.settlement_date) if inp(d)], D0)
    bridge["unexplained"] = bridge["bank_total"] - bridge["expected_bank"]

    rmask = df.status == ROUNDING
    r_net = _sum(df, rmask, "difference")
    breach = abs(r_net) > cfg.aggregate_tolerance
    df.loc[rmask, "needs_action"] = breach
    summary = {
        "platform_total": bridge["platform_total"], "bank_total": bridge["bank_total"],
        "reconciled_value": _sum(df, df.status.isin([MATCHED, ROUNDING]), "platform_amount"),
        "outstanding": bridge["bank_total"] - bridge["platform_total"], "unexplained": bridge["unexplained"],
        "matched": int((df.status == MATCHED).sum()),
        "exceptions": int((~df.status.isin([MATCHED, CARRY])).sum()),
        "action_required": int(df.needs_action.sum()),
        "rounding_count": int(rmask.sum()), "rounding_net": r_net,
        "rounding_gross": sum([abs(x) for x in df.loc[rmask, "difference"]], D0), "rounding_breach": bool(breach),
    }
    return Result(df, bridge, summary, p_all, b_all)
