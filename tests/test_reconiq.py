from decimal import Decimal as D
import pandas as pd
import pytest
from reconiq.config import Config
from reconiq.engine import reconcile
from reconiq.generator import generate
from reconiq.validation import PLATFORM_COLS, BANK_COLS, ValidationError


def P(tid, d, amt, typ="PAYMENT", ref="", cur="INR"):
    return dict(txn_id=tid, order_id="O-" + tid, txn_date=d, amount=str(amt), txn_type=typ, refund_ref=ref, currency=cur)


def B(tid, d, amt, typ="PAYMENT", cur="INR"):
    return dict(settlement_ref="S-" + tid, txn_id=tid, settlement_date=d, amount=str(amt), txn_type=typ, currency=cur)


def run(p, b, **kw):
    return reconcile(pd.DataFrame(p, columns=PLATFORM_COLS), pd.DataFrame(b, columns=BANK_COLS), Config(**kw))


def statuses(res, tid):
    return res.records[res.records.txn_id == tid].status.tolist()


def test_correct_match():
    r = run([P("T1", "2026-11-10", "100.00")], [B("T1", "2026-11-11", "100.00")])
    assert statuses(r, "T1") == ["MATCHED"]
    assert r.summary["reconciled_value"] == D("100.00") and r.bridge["unexplained"] == 0 and r.summary["exceptions"] == 0


def test_next_month_settlement_is_timing_not_missing():
    r = run([P("T1", "2026-11-30", "500.00")], [B("T1", "2026-12-01", "500.00")])
    assert statuses(r, "T1") == ["TIMING_NEXT_MONTH"]
    assert r.bridge["in_transit"] == D("-500.00") and r.bridge["bank_total"] == 0 and r.bridge["unexplained"] == 0


def test_missing_settlement_vs_pending():
    r = run([P("T1", "2026-11-10", "300.00"), P("T2", "2026-11-30", "70.00")], [])
    assert statuses(r, "T1") == ["MISSING_SETTLEMENT"] and statuses(r, "T2") == ["TIMING_PENDING"]
    assert r.bridge["missing"] == D("-300.00") and r.bridge["in_transit"] == D("-70.00") and r.bridge["unexplained"] == 0


def test_duplicate_bank_record_not_double_counted_in_match():
    r = run([P("T1", "2026-11-10", "100.00")], [B("T1", "2026-11-11", "100.00"), B("T1", "2026-11-11", "100.00")])
    assert sorted(statuses(r, "T1")) == ["DUPLICATE_BANK", "MATCHED"]
    assert r.bridge["dup_bank"] == D("100.00") and r.bridge["bank_total"] == D("200.00") and r.bridge["unexplained"] == 0


def test_duplicate_platform_record_excluded_from_totals():
    r = run([P("T1", "2026-11-10", "100.00"), P("T1", "2026-11-10", "100.00")], [B("T1", "2026-11-11", "100.00")])
    assert sorted(statuses(r, "T1")) == ["DUPLICATE_PLATFORM", "MATCHED"]
    assert r.summary["platform_total"] == D("100.00") and r.bridge["unexplained"] == 0


def test_orphan_refund_flagged_but_valid_refund_not():
    r = run([P("T1", "2026-11-05", "100.00"), P("R1", "2026-11-10", "-40.00", "REFUND", "T1"),
             P("R2", "2026-11-12", "-25.00", "REFUND", "NOPE")],
            [B("T1", "2026-11-06", "100.00"), B("R1", "2026-11-11", "-40.00", "REFUND"), B("R2", "2026-11-13", "-25.00", "REFUND")])
    assert statuses(r, "R1") == ["MATCHED"] and statuses(r, "R2") == ["ORPHAN_REFUND"]
    assert r.summary["exceptions"] == 1 and r.bridge["unexplained"] == 0


def test_individual_rounding_within_tolerance_and_above():
    r = run([P("T1", "2026-11-10", "100.005"), P("T2", "2026-11-10", "100.00")],
            [B("T1", "2026-11-11", "100.00"), B("T2", "2026-11-11", "100.02")])
    assert statuses(r, "T1") == ["ROUNDING_DIFF"] and statuses(r, "T2") == ["AMOUNT_MISMATCH"]
    assert r.bridge["rounding"] == D("-0.005") and r.bridge["other_diff"] == D("0.02") and r.bridge["unexplained"] == 0


def test_aggregate_rounding_breach_and_no_breach():
    def n(k):
        return run([P(f"T{i}", "2026-11-10", "10.005") for i in range(k)], [B(f"T{i}", "2026-11-11", "10.00") for i in range(k)])
    big, small = n(12), n(5)
    assert big.summary["rounding_net"] == D("-0.060") and big.summary["rounding_breach"] is True
    assert big.records.needs_action.all()
    assert small.summary["rounding_net"] == D("-0.025") and small.summary["rounding_breach"] is False
    assert not small.records.needs_action.any()


def test_offsetting_rounding_reported_gross():
    r = run([P("T1", "2026-11-10", "10.005"), P("T2", "2026-11-10", "9.995")], [B("T1", "2026-11-11", "10.00"), B("T2", "2026-11-11", "10.00")])
    assert r.summary["rounding_net"] == 0 and r.summary["rounding_gross"] == D("0.010")


def test_carry_in_from_prior_month():
    r = run([P("T0", "2026-10-30", "250.00")], [B("T0", "2026-11-02", "250.00")])
    assert statuses(r, "T0") == ["CARRY_IN"] and r.bridge["carry_in"] == D("250.00") and r.bridge["unexplained"] == 0


@pytest.mark.parametrize("bad", [
    P("T1", "2026-11-10", "abc"), P("T1", "10/11/2026", "5.00"), P("T1", "2026-11-10", "-5.00"),
    P("", "2026-11-10", "5.00"), P("T1", "2026-11-10", "5.00", typ="CHARGEBACK")])
def test_invalid_platform_rows_rejected(bad):
    with pytest.raises(ValidationError):
        run([bad], [])


def test_missing_columns_rejected():
    with pytest.raises(ValidationError, match="missing required columns"):
        reconcile(pd.DataFrame({"txn_id": ["T1"]}), pd.DataFrame(columns=BANK_COLS))


def test_empty_datasets():
    r = run([], [])
    assert r.records.empty and r.summary["platform_total"] == 0 and r.bridge["unexplained"] == 0 and r.summary["exceptions"] == 0


def test_generated_data_matches_answer_key_with_no_false_positives():
    p, b, key = generate()
    assert 1000 <= len(p) <= 1500
    r = reconcile(p, b)
    rec = r.records.set_index("txn_id")
    assert rec.loc[key["A_timing_next_month"]["txn_id"], "status"] == "TIMING_NEXT_MONTH"
    assert set(rec[rec.status == "ROUNDING_DIFF"].index) == set(key["B_rounding"]["txn_ids"])  # exactly the planted ones
    assert r.summary["rounding_net"] == D(key["B_rounding"]["net_difference"]) and r.summary["rounding_breach"]
    assert r.records[r.records.status == "DUPLICATE_BANK"].txn_id.tolist() == [key["C_duplicate"]["txn_id"]]
    assert r.records[r.records.status == "ORPHAN_REFUND"].txn_id.tolist() == [key["D_orphan_refund"]["txn_id"]]
    assert r.records[r.records.status == "MISSING_SETTLEMENT"].txn_id.tolist() == ["TXN-MISS-0001"]
    assert r.records[r.records.status == "BANK_ONLY"].txn_id.tolist() == ["TXN-GHOST-0001"]
    assert not r.records.status.isin(["AMOUNT_MISMATCH", "CURRENCY_MISMATCH", "DUPLICATE_PLATFORM", "TIMING_PENDING"]).any()
    assert r.bridge["unexplained"] == 0


def test_generator_is_deterministic():
    assert generate()[0].equals(generate()[0]) and generate()[1].equals(generate()[1])
