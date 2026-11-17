"""Synthetic data with a ground-truth answer key. Run: python -m reconiq.generator"""
import json, random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import numpy as np
import pandas as pd
from .validation import PLATFORM_COLS, BANK_COLS

SEED = 2026


def _cents(n):
    return Decimal(n).scaleb(-2)


def _settle(d, lag):
    return np.busday_offset(d, lag, roll="forward").item()


def generate(seed=SEED, n_payments=1150, n_refunds=60):
    rng = random.Random(seed)
    end = date(2026, 11, 30)
    days = [date(2026, 10, 29) + timedelta(i) for i in range(33)]  # late Oct = carry-in, then Nov
    plat, bank, payments = [], [], []

    def add(tid, d, amt, typ="PAYMENT", ref="", lag=1, bank_amt=None, on_bank=True):
        plat.append(dict(txn_id=tid, order_id=f"ORD-{100001 + len(plat)}", txn_date=d.isoformat(),
                         amount=str(amt), txn_type=typ, refund_ref=ref, currency="INR"))
        if on_bank:
            bank.append(dict(settlement_ref=f"STL-{len(bank) + 1:06d}", txn_id=tid,
                             settlement_date=_settle(d, lag).isoformat(),
                             amount=str(amt if bank_amt is None else bank_amt), txn_type=typ, currency="INR"))

    for i in range(n_payments):
        d, amt = rng.choice(days), _cents(rng.randint(500, 250000))
        add(f"TXN-{i + 1:05d}", d, amt, lag=rng.choice([1, 1, 2]))
        payments.append((f"TXN-{i + 1:05d}", d, amt))
    for i in range(n_refunds):
        oid, od, oamt = rng.choice(payments)
        c = int(oamt * 100)
        add(f"RFD-{i + 1:05d}", min(od + timedelta(rng.randint(1, 10)), end),
            _cents(-(c if rng.random() < 0.5 else rng.randint(100, c))), "REFUND", oid, rng.choice([1, 2]))

    # --- Injected anomalies -------------------------------------------------
    add("TXN-CUT-0001", end, Decimal("18250.00"), lag=1)                       # A: Nov 30 -> settles Dec 1
    rnd = []
    for i in range(20):                                                        # B: 20 x -0.005
        base = f"{rng.randint(100, 9999)}.{rng.randint(0, 99):02d}"
        add(f"TXN-RND-{i + 1:04d}", date(2026, 11, rng.randint(3, 20)), Decimal(base + "5"),
            bank_amt=Decimal(base))
        rnd.append(f"TXN-RND-{i + 1:04d}")
    add("TXN-DUP-0001", date(2026, 11, 9), Decimal("749.00"))                  # C: bank pays twice
    bank.append(dict(bank[-1]))
    add("RFD-ORPH-0001", date(2026, 11, 18), Decimal("-1499.00"), "REFUND", "TXN-NOEXIST-99")  # D
    add("TXN-MISS-0001", date(2026, 11, 12), Decimal("3200.00"), on_bank=False)   # extra: never settled
    bank.append(dict(settlement_ref="STL-GHOST1", txn_id="TXN-GHOST-0001", settlement_date="2026-11-20",
                     amount="880.00", txn_type="PAYMENT", currency="INR"))     # extra: bank-only

    p = pd.DataFrame(plat, columns=PLATFORM_COLS).sort_values(["txn_date", "txn_id"]).reset_index(drop=True)
    b = pd.DataFrame(bank, columns=BANK_COLS)
    b = b[b["settlement_date"] >= "2026-11-01"].sort_values(["settlement_date", "settlement_ref"]).reset_index(drop=True)
    key = {
        "A_timing_next_month": {"txn_id": "TXN-CUT-0001", "amount": "18250.00", "status": "TIMING_NEXT_MONTH"},
        "B_rounding": {"txn_ids": rnd, "net_difference": str(Decimal("-0.005") * 20), "status": "ROUNDING_DIFF"},
        "C_duplicate": {"txn_id": "TXN-DUP-0001", "dataset": "bank", "amount": "749.00", "status": "DUPLICATE_BANK"},
        "D_orphan_refund": {"txn_id": "RFD-ORPH-0001", "refund_ref": "TXN-NOEXIST-99", "status": "ORPHAN_REFUND"},
        "extra_missing_settlement": {"txn_id": "TXN-MISS-0001", "status": "MISSING_SETTLEMENT"},
        "extra_bank_only": {"txn_id": "TXN-GHOST-0001", "status": "BANK_ONLY"},
    }
    return p, b, key


def write_samples(folder="data"):
    p, b, key = generate()
    out = Path(folder)
    out.mkdir(exist_ok=True)
    p.to_csv(out / "platform_transactions.csv", index=False)
    b.to_csv(out / "bank_settlements.csv", index=False)
    (out / "injected_anomalies.json").write_text(json.dumps(key, indent=2))
    return len(p), len(b)


if __name__ == "__main__":
    print("platform rows, bank rows:", write_samples())
