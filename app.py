from datetime import date
from decimal import Decimal
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from reconiq.config import Config
from reconiq.engine import (reconcile, LABELS, BRIDGE_LABELS, NEXT, PENDING, ROUNDING, DUP_P, DUP_B, ORPHAN,
                            MISSING, BANK_ONLY, AMOUNT, CCY)
from reconiq.generator import generate
from reconiq.validation import PLATFORM_COLS, BANK_COLS, ValidationError

st.set_page_config(page_title="ReconIQ", page_icon="🧾", layout="wide")
st.markdown("<style>[data-testid='stMetricValue']{font-size:1.5rem}</style>", unsafe_allow_html=True)


@st.cache_data
def sample():
    p, b, key = generate()
    return p, b


@st.cache_data(show_spinner=False)
def sample_result(cfg):
    p, b = sample()
    return reconcile(p, b, cfg)


def money(x, dp=2):
    return f"{x:,.{dp}f}"


def table(df, cols=("txn_id", "txn_type", "txn_date", "settlement_date", "platform_amount", "bank_amount",
                    "difference", "status", "reason")):
    out = df[list(cols)].copy()
    for c in ("platform_amount", "bank_amount", "difference"):
        if c in out:
            out[c] = out[c].map(lambda v: "" if v is None or (not isinstance(v, Decimal) and pd.isna(v)) else f"{v:,.3f}")
    out["status"] = out["status"].map(LABELS)
    return out.rename(columns=lambda c: c.replace("_", " ").title())


with st.sidebar:
    st.title("🧾 ReconIQ")
    st.caption("Platform ledger vs bank settlement reconciliation")
    start = st.date_input("Period start", date(2026, 11, 1))
    end = st.date_input("Period end (cut-off)", date(2026, 11, 30))
    tol = st.number_input("Per-item rounding tolerance", 0.0, 10.0, 0.01, 0.005, format="%.3f")
    agg = st.number_input("Aggregate rounding tolerance", 0.0, 100.0, 0.05, 0.01, format="%.3f")
    window = st.number_input("Settlement window (business days)", 1, 10, 2)
cfg = Config(start, end, Decimal(str(tol)), Decimal(str(agg)), int(window))

tabs = st.tabs(["Overview", "Exceptions", "Transaction search", "Analysis", "Data upload", "Export"])

with tabs[4]:
    st.subheader("Data source")
    sp, sb = sample()
    src = st.radio("Use", ["Built-in sample data", "Uploaded CSV files"], horizontal=True)
    d1, d2 = st.columns(2)
    d1.download_button("⬇ Sample platform CSV", sp.to_csv(index=False), "platform_transactions.csv", "text/csv")
    d2.download_button("⬇ Sample bank CSV", sb.to_csv(index=False), "bank_settlements.csv", "text/csv")
    u1, u2 = st.columns(2)
    up_p = u1.file_uploader("Platform transactions CSV", type="csv")
    up_b = u2.file_uploader("Bank settlements CSV", type="csv")
    st.caption(f"Platform columns: {', '.join(PLATFORM_COLS)}  \nBank columns: {', '.join(BANK_COLS)}  \n"
               "Dates YYYY-MM-DD; payments positive, refunds negative.")
    platform_raw, bank_raw = sp, sb
    if src.startswith("Uploaded"):
        if not (up_p and up_b):
            st.warning("Upload both files to continue.")
            st.stop()
        platform_raw = pd.read_csv(up_p, dtype=str, keep_default_na=False)
        bank_raw = pd.read_csv(up_b, dtype=str, keep_default_na=False)
    try:
        res = sample_result(cfg) if platform_raw is sp else reconcile(platform_raw, bank_raw, cfg)
    except ValidationError as e:
        st.error("Validation failed. Fix these problems and re-upload:")
        for pb in e.problems:
            st.write("• " + pb)
        st.stop()
    st.success(f"Validated {len(res.platform):,} platform rows and {len(res.bank):,} bank rows.")

S, R = res.summary, res.records

with tabs[0]:
    c = st.columns(6)
    c[0].metric("Transaction value", money(S["platform_total"]))
    c[1].metric("Bank settlement value", money(S["bank_total"]))
    c[2].metric("Reconciled value", money(S["reconciled_value"]))
    c[3].metric("Outstanding discrepancy", money(S["outstanding"]))
    c[4].metric("Matched records", f"{S['matched']:,}")
    c[5].metric("Exceptions", f"{S['exceptions']:,}", f"{S['action_required']} need action", delta_color="off")
    if S["unexplained"] == 0:
        st.success("Fully explained: the unexplained residual is 0.00.")
    else:
        st.error(f"Unexplained residual: {money(S['unexplained'], 3)}. Some difference is not classified.")
    st.subheader("Bridge: platform total → bank total")
    keys = list(BRIDGE_LABELS)
    left, right = st.columns([2, 3])
    left.dataframe(pd.DataFrame({"Item": [BRIDGE_LABELS[k] for k in keys],
                                 "Amount": [money(res.bridge[k], 3) for k in keys]}), hide_index=True, width="stretch")
    steps = ["platform_total", "in_transit", "missing", "carry_in", "rounding", "other_diff", "dup_bank", "bank_only"]
    fig = go.Figure(go.Waterfall(x=[BRIDGE_LABELS[k].split(":")[-1].strip()[:28] for k in steps] + ["Bank total"],
                                 measure=["absolute"] + ["relative"] * 7 + ["total"],
                                 y=[float(res.bridge[k]) for k in steps] + [0]))
    fig.update_layout(height=380, margin=dict(t=10, b=10), showlegend=False)
    right.plotly_chart(fig, width="stretch")
    st.caption("Timing items reverse next month and need no action. Duplicates, orphan refunds, missing and bank-only lines need investigation.")

with tabs[1]:
    def section(title, note, statuses, action):
        sub = R[R.status.isin(statuses)]
        st.subheader(f"{title} ({len(sub)})")
        st.caption(f"{note}  **Action:** {action}")
        if len(sub):
            st.dataframe(table(sub), hide_index=True, width="stretch")
        else:
            st.info("None found.")
    section("A. Settled next month (timing)", "Valid transactions inside the period that the bank settled after the cut-off.",
            [NEXT], "none; they clear in the next period.")
    st.subheader(f"B. Rounding differences ({S['rounding_count']})")
    m = st.columns(3)
    m[0].metric("Net rounding", money(S["rounding_net"], 3))
    m[1].metric("Gross rounding", money(S["rounding_gross"], 3))
    m[2].metric("Aggregate tolerance", money(cfg.aggregate_tolerance, 3))
    (st.error if S["rounding_breach"] else st.success)(
        "Net rounding exceeds the aggregate tolerance. Each item is within tolerance but together they need an adjustment."
        if S["rounding_breach"] else "Net rounding is within the aggregate tolerance.")
    if S["rounding_count"]:
        st.dataframe(table(R[R.status == ROUNDING]), hide_index=True, width="stretch")
    section("C. Duplicate records", "Same transaction ID appears more than once in one dataset.", [DUP_P, DUP_B],
            "dispute duplicate bank payment or remove duplicate ledger entry.")
    section("D. Refunds without an original transaction", "Refund references a transaction ID that is not in the platform data.",
            [ORPHAN], "investigate the refund's origin.")
    section("Other discrepancies", "Missing settlements, bank-only lines, amount/currency mismatches, pending items.",
            [MISSING, BANK_ONLY, AMOUNT, CCY, PENDING], "investigate with the bank.")

with tabs[2]:
    q = st.text_input("Transaction ID (partial match allowed)", placeholder="e.g. TXN-CUT-0001").strip()
    if q:
        hit = lambda df: df[df.txn_id.str.contains(q, case=False, regex=False)]
        a, b_, c_ = hit(R), hit(res.platform), hit(res.bank)
        if a.empty and b_.empty and c_.empty:
            st.warning("No record found in either dataset.")
        else:
            st.markdown("**Reconciliation result**")
            for _, r in a.iterrows():
                st.write(f"`{r.txn_id}` → **{LABELS[r.status]}**: {r.reason}")
            st.markdown("**Platform record(s)**")
            st.dataframe(b_.astype(str), hide_index=True, width="stretch")
            st.markdown("**Bank record(s)**")
            st.dataframe(c_.astype(str), hide_index=True, width="stretch")

with tabs[3]:
    pf = res.platform.assign(v=res.platform.amount.map(float)).groupby("txn_date").v.sum().rename("Platform (by transaction date)")
    bf = res.bank.assign(v=res.bank.amount.map(float)).groupby("settlement_date").v.sum().rename("Bank (by settlement date)")
    daily = pd.concat([pf, bf], axis=1).fillna(0).reset_index(names="date")
    daily = daily[(daily.date >= cfg.period_start) & (daily.date <= cfg.period_end)]
    st.plotly_chart(px.bar(daily.melt("date"), x="date", y="value", color="variable", barmode="group",
                           title="Daily totals: platform vs bank", labels={"value": "Amount", "variable": ""}),
                    width="stretch")
    cat = R.assign(Category=R.status.map(LABELS)).groupby("Category").size().reset_index(name="Records")
    st.plotly_chart(px.bar(cat.sort_values("Records"), x="Records", y="Category", orientation="h",
                           title="Records by reconciliation status", log_x=True), width="stretch")

with tabs[5]:
    st.download_button("⬇ Full reconciliation results (CSV)", R.drop(columns=["bucket", "diff_kind"]).to_csv(index=False),
                       "reconciliation_results.csv", "text/csv")
    st.download_button("⬇ Exception report (CSV)", res.exceptions.drop(columns=["bucket", "diff_kind"]).to_csv(index=False),
                       "exception_report.csv", "text/csv")
    st.download_button("⬇ Bridge summary (CSV)", pd.DataFrame({"item": [BRIDGE_LABELS[k] for k in BRIDGE_LABELS],
                       "amount": [str(res.bridge[k]) for k in BRIDGE_LABELS]}).to_csv(index=False), "bridge.csv", "text/csv")
