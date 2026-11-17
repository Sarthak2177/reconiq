"""ReconIQ — Payment reconciliation dashboard."""
from datetime import date, datetime
from decimal import Decimal
import io
import zipfile

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from reconiq.config import Config
from reconiq.engine import (
    reconcile, LABELS, BRIDGE_LABELS,
    MATCHED, CARRY, NEXT, PENDING, ROUNDING,
    DUP_P, DUP_B, ORPHAN, MISSING, BANK_ONLY, AMOUNT, CCY,
)
from reconiq.generator import generate
from reconiq.validation import PLATFORM_COLS, BANK_COLS, ValidationError

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="ReconIQ", page_icon="🧾", layout="wide")

# ── Custom CSS ───────────────────────────────────────────────────────────────
st.markdown("""<style>
/* Metric cards */
[data-testid="stMetric"] {
    background: rgba(0, 212, 170, 0.04);
    border: 1px solid rgba(0, 212, 170, 0.12);
    border-left: 3px solid #00d4aa;
    border-radius: 8px;
    padding: 14px 16px;
}
[data-testid="stMetricValue"] {
    font-size: 1.35rem !important;
    font-weight: 600;
}
/* Tabs */
.stTabs [data-baseweb="tab-list"] { gap: 4px; }
.stTabs [data-baseweb="tab"] { border-radius: 6px 6px 0 0; }
/* Alerts */
[data-testid="stAlert"] { border-radius: 8px; }
/* Header banner */
.riq-hdr {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 24px; border-radius: 10px; margin-bottom: 20px;
}
.riq-hdr.ok {
    background: linear-gradient(135deg, rgba(34,197,94,.08), rgba(0,212,170,.08));
    border: 1px solid rgba(34,197,94,.2);
}
.riq-hdr.err {
    background: linear-gradient(135deg, rgba(239,68,68,.08), rgba(249,115,22,.08));
    border: 1px solid rgba(239,68,68,.2);
}
.riq-hdr h2 { margin: 0; font-size: 1.3rem; }
.pill {
    padding: 4px 14px; border-radius: 20px;
    font-size: 0.85rem; font-weight: 600;
}
.pill-g { background: rgba(34,197,94,.15); color: #22c55e; }
.pill-r { background: rgba(239,68,68,.15); color: #ef4444; }
/* Summary box */
.sbox {
    background: rgba(0,212,170,.04);
    border: 1px solid rgba(0,212,170,.1);
    border-radius: 10px; padding: 16px 20px;
    line-height: 1.8; margin-bottom: 16px; font-size: 1rem;
}
/* Sidebar footer */
.sfooter {
    font-size: 0.75rem; color: #6b7280;
    padding: 8px 0; margin-top: 20px;
}
</style>""", unsafe_allow_html=True)

# ── Constants ────────────────────────────────────────────────────────────────
SD = {
    MATCHED: "✅ Matched", CARRY: "✅ Carried in", ROUNDING: "🟡 Rounding",
    NEXT: "🔵 Next month", PENDING: "🔵 Pending", MISSING: "🔴 Missing",
    DUP_P: "🔴 Dup (platform)", DUP_B: "🔴 Dup (bank)",
    ORPHAN: "🔴 Orphan refund", BANK_ONLY: "🔴 Bank only",
    AMOUNT: "🔴 Amt mismatch", CCY: "🔴 Ccy mismatch",
}

PRIO = {
    MISSING: ("🔴 High", 1), ORPHAN: ("🔴 High", 1),
    DUP_P: ("🔴 High", 1), DUP_B: ("🔴 High", 1),
    AMOUNT: ("🟠 Medium", 2), CCY: ("🟠 Medium", 2),
    BANK_ONLY: ("🟠 Medium", 2), ROUNDING: ("🟡 Low", 3),
}

ACTIONS = {
    MISSING: "Investigate with bank",
    ORPHAN: "Trace refund origin",
    DUP_P: "Remove duplicate ledger entry",
    DUP_B: "Dispute duplicate bank payment",
    AMOUNT: "Investigate amount difference",
    CCY: "Verify currency with bank",
    BANK_ONLY: "Identify source transaction",
    ROUNDING: "Post journal adjustment",
}

CC = {"green": "#22c55e", "red": "#ef4444", "amber": "#f59e0b",
      "blue": "#3b82f6", "teal": "#00d4aa", "gray": "#6b7280"}
TRANSPARENT = "rgba(0,0,0,0)"
GRID = "rgba(255,255,255,0.05)"


# ── Helpers ──────────────────────────────────────────────────────────────────
def money(x, dp=2):
    """Format a Decimal as a comma-separated string."""
    return f"{x:,.{dp}f}"


def _fa(v):
    """Format an amount value for table display."""
    if v is None or (not isinstance(v, Decimal) and pd.isna(v)):
        return ""
    return f"{v:,.2f}"


def _tbl(df, action=False):
    """Format a records DataFrame for display with optional action columns."""
    base = ["txn_id", "txn_type", "txn_date", "settlement_date",
            "platform_amount", "bank_amount", "difference", "status", "reason"]
    if action:
        base = ["priority", "txn_id", "txn_type", "platform_amount", "bank_amount",
                "difference", "status", "suggested_action", "amount_at_risk", "reason"]
    out = df[[c for c in base if c in df.columns]].copy()
    for c in ("platform_amount", "bank_amount", "difference", "amount_at_risk"):
        if c in out:
            out[c] = out[c].map(_fa)
    if "status" in out:
        out["status"] = out["status"].map(SD)
    return out.rename(columns=lambda c: c.replace("_", " ").title())


def _zip(res, cfg):
    """Build a ZIP evidence pack with all reconciliation artifacts."""
    buf = io.BytesIO()
    s = res.summary
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("summary.csv", pd.DataFrame(
            [[k.replace("_", " ").title(), str(v)] for k, v in s.items()],
            columns=["Metric", "Value"]).to_csv(index=False))
        zf.writestr("bridge.csv", pd.DataFrame(
            {"Item": [BRIDGE_LABELS[k] for k in BRIDGE_LABELS],
             "Amount": [str(res.bridge[k]) for k in BRIDGE_LABELS]}).to_csv(index=False))
        zf.writestr("exceptions.csv",
                     res.exceptions.drop(columns=["bucket", "diff_kind"],
                                         errors="ignore").to_csv(index=False))
        carry = res.records[res.records.status.isin([NEXT, PENDING])]
        zf.writestr("carry_forward.csv",
                     carry.drop(columns=["bucket", "diff_kind"],
                                errors="ignore").to_csv(index=False))
        zf.writestr("full_results.csv",
                     res.records.drop(columns=["bucket", "diff_kind"],
                                      errors="ignore").to_csv(index=False))
        zf.writestr("run_info.txt", "\n".join([
            "ReconIQ Evidence Pack",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Period: {cfg.period_start} to {cfg.period_end}",
            f"Item tolerance: {cfg.item_tolerance}",
            f"Aggregate tolerance: {cfg.aggregate_tolerance}",
            f"Settlement window: {cfg.settlement_window_days} business days",
            f"Platform rows: {len(res.platform):,}",
            f"Bank rows: {len(res.bank):,}",
            f"Matched: {s['matched']:,}",
            f"Exceptions: {s['exceptions']:,}",
            f"Action required: {s['action_required']}",
            f"Unexplained residual: {res.bridge['unexplained']}",
        ]))
    buf.seek(0)
    return buf


# ── Data loading ─────────────────────────────────────────────────────────────
@st.cache_data
def _sample():
    p, b, _ = generate()
    return p, b


@st.cache_data(show_spinner=False)
def _sample_res(cfg):
    p, b = _sample()
    return reconcile(p, b, cfg)


# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🧾 ReconIQ")
    st.caption("Payment reconciliation engine")
    st.divider()
    with st.expander("📅 Period", expanded=True):
        start = st.date_input("Start", date(2026, 11, 1))
        end = st.date_input("End (cut-off)", date(2026, 11, 30))
    with st.expander("⚙️ Tolerances", expanded=True):
        tol = st.number_input("Per-item rounding", 0.0, 10.0, 0.01, 0.005, format="%.3f")
        agg = st.number_input("Aggregate rounding", 0.0, 100.0, 0.05, 0.01, format="%.3f")
        window = st.number_input("Settlement window (biz days)", 1, 10, 2)
    st.markdown('<p class="sfooter">ReconIQ v1.0 · Built for Onelab</p>',
                unsafe_allow_html=True)

cfg = Config(start, end, Decimal(str(tol)), Decimal(str(agg)), int(window))

# ── Tabs ─────────────────────────────────────────────────────────────────────
tabs = st.tabs(["📊 Overview", "⚠️ Exceptions", "🔍 Search",
                "📈 Analysis", "📁 Data", "📥 Export", "📖 Methodology"])

# ── Tab: Data (runs first to produce `res`) ──────────────────────────────────
with tabs[4]:
    st.subheader("Data source")
    sp, sb = _sample()
    src = st.radio("Use", ["Built-in sample data", "Uploaded CSV files"],
                   horizontal=True)
    d1, d2 = st.columns(2)
    d1.download_button("⬇ Sample platform CSV", sp.to_csv(index=False),
                       "platform_transactions.csv", "text/csv")
    d2.download_button("⬇ Sample bank CSV", sb.to_csv(index=False),
                       "bank_settlements.csv", "text/csv")
    u1, u2 = st.columns(2)
    up_p = u1.file_uploader("Platform CSV", type="csv")
    up_b = u2.file_uploader("Bank CSV", type="csv")
    st.caption(f"Platform: {', '.join(PLATFORM_COLS)}  \n"
               f"Bank: {', '.join(BANK_COLS)}  \n"
               "Dates YYYY-MM-DD · payments positive · refunds negative")
    pr, br = sp, sb
    if src.startswith("Up"):
        if not (up_p and up_b):
            st.warning("Upload both files to continue.")
            st.stop()
        pr = pd.read_csv(up_p, dtype=str, keep_default_na=False)
        br = pd.read_csv(up_b, dtype=str, keep_default_na=False)
    try:
        res = _sample_res(cfg) if pr is sp else reconcile(pr, br, cfg)
    except ValidationError as e:
        st.error("Validation failed:")
        for pb in e.problems:
            st.write("• " + pb)
        st.stop()
    st.success(f"✅ {len(res.platform):,} platform rows · {len(res.bank):,} bank rows")

S, R = res.summary, res.records

# ── Tab: Overview ────────────────────────────────────────────────────────────
with tabs[0]:
    # Status banner
    bal = S["unexplained"] == 0
    st.markdown(
        f'<div class="riq-hdr {"ok" if bal else "err"}">'
        f'<h2>🧾 Reconciliation — {cfg.period_start.strftime("%B %Y")}</h2>'
        f'<span class="pill {"pill-g" if bal else "pill-r"}">'
        f'{"✅ Fully Reconciled" if bal else "⚠️ Discrepancies"}</span></div>',
        unsafe_allow_html=True)

    # Plain-English summary
    timing_v = abs(res.bridge.get("in_transit", Decimal(0)))
    carry_v = res.bridge.get("carry_in", Decimal(0))
    gap = S["bank_total"] - S["platform_total"]
    dr = "lower" if gap < 0 else "higher"
    ac = S["action_required"]
    st.markdown(
        f'<div class="sbox">'
        f'Bank total is <strong>{money(abs(gap))}</strong> {dr} than the platform. '
        f'<strong>{money(timing_v)}</strong> is timing (settles next month), '
        f'<strong>{money(carry_v)}</strong> was carried in from the prior period, '
        f'and <strong>{ac} item{"s" if ac != 1 else ""}</strong> '
        f'need{"s" if ac == 1 else ""} investigation. '
        f'Unexplained residual: <strong>{money(S["unexplained"], 3)}</strong> '
        f'{"✅" if bal else "❌"}</div>',
        unsafe_allow_html=True)

    # Metric cards
    c = st.columns(6)
    c[0].metric("Platform total", money(S["platform_total"]),
                help="De-duplicated platform transactions in the period")
    c[1].metric("Bank total", money(S["bank_total"]),
                help="All bank settlements dated within the period")
    c[2].metric("Reconciled", money(S["reconciled_value"]),
                help="Matched exactly or within rounding tolerance")
    c[3].metric("Outstanding", money(abs(gap)),
                help="Absolute difference between platform and bank totals")
    c[4].metric("Matched", f"{S['matched']:,}",
                help="Records with exact or near-exact bank match")
    c[5].metric("⚡ Need action", f"{ac}",
                delta=f"{S['exceptions'] - ac} auto-clear",
                delta_color="off",
                help="Items requiring investigation. The rest are timing items.")

    # Bridge table + waterfall
    st.subheader("Bridge: platform total → bank total")
    keys = list(BRIDGE_LABELS)
    lt, rt = st.columns([2, 3])
    lt.dataframe(
        pd.DataFrame({"Item": [BRIDGE_LABELS[k] for k in keys],
                       "Amount": [money(res.bridge[k], 3) for k in keys]}),
        hide_index=True, use_container_width=True)

    steps = ["platform_total", "in_transit", "missing", "carry_in",
             "rounding", "other_diff", "dup_bank", "bank_only"]
    fig = go.Figure(go.Waterfall(
        x=[BRIDGE_LABELS[k].split(":")[-1].strip()[:28] for k in steps]
          + ["Bank total"],
        measure=["absolute"] + ["relative"] * 7 + ["total"],
        y=[float(res.bridge[k]) for k in steps] + [0],
        increasing=dict(marker_color=CC["green"]),
        decreasing=dict(marker_color=CC["red"]),
        totals=dict(marker_color=CC["teal"]),
        connector=dict(line=dict(color="rgba(255,255,255,0.1)")),
    ))
    fig.update_layout(height=380, margin=dict(t=10, b=10), showlegend=False,
                      paper_bgcolor=TRANSPARENT, plot_bgcolor=TRANSPARENT,
                      font_color="#e6edf3",
                      yaxis=dict(gridcolor=GRID))
    rt.plotly_chart(fig, use_container_width=True)
    st.caption("Timing items reverse next month. "
               "Duplicates, orphans, missing and bank-only need investigation.")

# ── Tab: Exceptions ──────────────────────────────────────────────────────────
with tabs[1]:
    act = R[R.needs_action].copy()
    tim = R[R.status.isin([NEXT, PENDING])]
    rnd = R[R.status == ROUNDING]

    m = st.columns(4)
    m[0].metric("⚡ Need action", f"{len(act)}",
                help="Requires manual investigation")
    m[1].metric("🔵 Timing", f"{len(tim)}",
                help="Auto-clears next month — no action needed")
    m[2].metric("🟡 Rounding", f"{len(rnd)}",
                help="Within per-item tolerance; check aggregate")
    m[3].metric("✅ Matched", f"{S['matched']:,}",
                help="Fully reconciled records")

    st.divider()

    # Action queue — prioritised
    if len(act):
        st.subheader(f"🔴 Action required ({len(act)})")
        act["priority"] = act.status.map(
            lambda s: PRIO.get(s, ("⚪ Info", 9))[0])
        act["_ps"] = act.status.map(
            lambda s: PRIO.get(s, ("", 9))[1])
        act["suggested_action"] = act.status.map(
            lambda s: ACTIONS.get(s, ""))
        act["amount_at_risk"] = act.apply(
            lambda r: (abs(r.platform_amount)
                       if isinstance(r.platform_amount, Decimal) and r.platform_amount
                       else abs(r.bank_amount)
                       if isinstance(r.bank_amount, Decimal) and r.bank_amount
                       else Decimal(0)),
            axis=1)
        act = act.sort_values(["_ps", "amount_at_risk"],
                              ascending=[True, False])
        st.dataframe(_tbl(act, action=True),
                     hide_index=True, use_container_width=True)
    else:
        st.success("🎉 No items need action!")

    st.divider()

    # Rounding section
    st.subheader(f"🟡 Rounding ({S['rounding_count']})")
    rm = st.columns(3)
    rm[0].metric("Net", money(S["rounding_net"], 3))
    rm[1].metric("Gross", money(S["rounding_gross"], 3))
    rm[2].metric("Tolerance", money(cfg.aggregate_tolerance, 3))
    if S["rounding_breach"]:
        adj = abs(S["rounding_net"])
        st.error(f"⚡ Net rounding exceeds tolerance → "
                 f"**Post adjustment of {money(adj, 2)}**")
    else:
        st.success("Net rounding is within aggregate tolerance.")
    if S["rounding_count"]:
        with st.expander(f"View {S['rounding_count']} rounding items"):
            st.dataframe(_tbl(rnd), hide_index=True,
                         use_container_width=True)

    st.divider()

    # Timing items — collapsed
    with st.expander(f"🔵 Timing — auto-clears next month ({len(tim)})"):
        if len(tim):
            st.dataframe(_tbl(tim), hide_index=True,
                         use_container_width=True)
        else:
            st.info("None.")

# ── Tab: Search ──────────────────────────────────────────────────────────────
with tabs[2]:
    st.subheader("🔍 Transaction lookup")
    q = st.text_input("Transaction ID (partial match)",
                      placeholder="TXN-CUT-0001").strip()
    if q:
        hit = lambda df: df[df.txn_id.str.contains(q, case=False,
                                                    regex=False)]
        a, b_, c_ = hit(R), hit(res.platform), hit(res.bank)
        if a.empty and b_.empty and c_.empty:
            st.warning("No matches found.")
        else:
            st.markdown("**Reconciliation result**")
            for _, row in a.iterrows():
                st.write(f"`{row.txn_id}` → "
                         f"**{SD.get(row.status, row.status)}**: "
                         f"{row.reason}")
            x1, x2 = st.columns(2)
            with x1:
                st.markdown("**Platform record(s)**")
                st.dataframe(b_.astype(str), hide_index=True,
                             use_container_width=True)
            with x2:
                st.markdown("**Bank record(s)**")
                st.dataframe(c_.astype(str), hide_index=True,
                             use_container_width=True)

# ── Tab: Analysis ────────────────────────────────────────────────────────────
with tabs[3]:
    # Daily totals
    pf = (res.platform
          .assign(v=res.platform.amount.map(float))
          .groupby("txn_date").v.sum()
          .rename("Platform"))
    bf = (res.bank
          .assign(v=res.bank.amount.map(float))
          .groupby("settlement_date").v.sum()
          .rename("Bank"))
    daily = (pd.concat([pf, bf], axis=1).fillna(0)
             .reset_index(names="date"))
    daily = daily[(daily.date >= cfg.period_start)
                  & (daily.date <= cfg.period_end)]

    fig1 = px.bar(daily.melt("date"), x="date", y="value",
                  color="variable", barmode="group",
                  title="Daily totals: platform vs bank",
                  labels={"value": "Amount", "variable": ""},
                  color_discrete_map={"Platform": CC["teal"],
                                      "Bank": CC["blue"]})
    fig1.update_layout(paper_bgcolor=TRANSPARENT, plot_bgcolor=TRANSPARENT,
                       font_color="#e6edf3",
                       yaxis=dict(gridcolor=GRID))
    st.plotly_chart(fig1, use_container_width=True)

    ca, cb = st.columns(2)

    # Amount at risk by category
    with ca:
        adf = R[R.needs_action].copy()
        if not adf.empty:
            adf["abs_amt"] = adf.apply(
                lambda r: (float(abs(r.platform_amount))
                           if isinstance(r.platform_amount, Decimal)
                              and r.platform_amount
                           else float(abs(r.bank_amount))
                           if isinstance(r.bank_amount, Decimal)
                              and r.bank_amount
                           else 0),
                axis=1)
            cat = (adf.assign(Cat=adf.status.map(LABELS))
                   .groupby("Cat").abs_amt.sum()
                   .reset_index(name="Amount"))
            fig2 = px.bar(cat.sort_values("Amount"),
                          x="Amount", y="Cat", orientation="h",
                          title="Amount at risk by category",
                          color_discrete_sequence=[CC["red"]])
            fig2.update_layout(
                paper_bgcolor=TRANSPARENT, plot_bgcolor=TRANSPARENT,
                font_color="#e6edf3",
                yaxis=dict(gridcolor=GRID, title=""))
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("No action-required items to chart.")

    # Settlement lag histogram
    with cb:
        mb = R[(R.txn_date.notna()) & (R.settlement_date.notna())
               & R.status.isin([MATCHED, CARRY, ROUNDING])].copy()
        if not mb.empty:
            mb["lag"] = mb.apply(
                lambda r: ((r.settlement_date - r.txn_date).days
                           if isinstance(r.txn_date, date)
                              and isinstance(r.settlement_date, date)
                           else None),
                axis=1)
            mb = mb.dropna(subset=["lag"])
            if not mb.empty:
                fig3 = px.histogram(
                    mb, x="lag", nbins=10,
                    title="Settlement lag (days)",
                    labels={"lag": "Days to settle"},
                    color_discrete_sequence=[CC["teal"]])
                fig3.update_layout(
                    paper_bgcolor=TRANSPARENT, plot_bgcolor=TRANSPARENT,
                    font_color="#e6edf3",
                    yaxis=dict(gridcolor=GRID), showlegend=False)
                st.plotly_chart(fig3, use_container_width=True)
            else:
                st.info("No settlement lag data.")
        else:
            st.info("No settlement lag data available.")

    # Records by status
    cat_all = (R.assign(Cat=R.status.map(SD))
               .groupby("Cat").size()
               .reset_index(name="Count"))
    fig4 = px.bar(cat_all.sort_values("Count"),
                  x="Count", y="Cat", orientation="h",
                  title="Records by reconciliation status",
                  color_discrete_sequence=[CC["blue"]])
    fig4.update_layout(paper_bgcolor=TRANSPARENT, plot_bgcolor=TRANSPARENT,
                       font_color="#e6edf3",
                       yaxis=dict(gridcolor=GRID, title=""))
    st.plotly_chart(fig4, use_container_width=True)

# ── Tab: Export ──────────────────────────────────────────────────────────────
with tabs[5]:
    st.subheader("📥 Reports")
    e1, e2 = st.columns(2)
    e1.download_button(
        "📄 Full results (CSV)",
        R.drop(columns=["bucket", "diff_kind"]).to_csv(index=False),
        "reconciliation_results.csv", "text/csv")
    e1.download_button(
        "📄 Exceptions (CSV)",
        res.exceptions.drop(columns=["bucket", "diff_kind"]).to_csv(index=False),
        "exception_report.csv", "text/csv")

    cf = R[R.status.isin([NEXT, PENDING])]
    e2.download_button(
        "📄 Carry-forward (CSV)",
        cf.drop(columns=["bucket", "diff_kind"],
                errors="ignore").to_csv(index=False),
        "carry_forward.csv", "text/csv")
    e2.download_button(
        "📄 Bridge (CSV)",
        pd.DataFrame({"Item": [BRIDGE_LABELS[k] for k in BRIDGE_LABELS],
                       "Amount": [str(res.bridge[k]) for k in BRIDGE_LABELS]})
        .to_csv(index=False),
        "bridge.csv", "text/csv")

    st.divider()
    st.subheader("📦 Evidence pack")
    st.caption("ZIP containing summary, bridge, exceptions, carry-forward, "
               "full results, and run metadata.")
    st.download_button(
        "⬇ Download evidence pack (ZIP)",
        _zip(res, cfg),
        f"reconiq_evidence_{cfg.period_start}_{cfg.period_end}.zip",
        "application/zip")

# ── Tab: Methodology ─────────────────────────────────────────────────────────
with tabs[6]:
    st.subheader("📖 Reconciliation methodology")
    st.markdown("""
### Matching rules
- **Match key:** `txn_id` (exact match between platform and bank)
- **Platform month:** `txn_date` · **Bank month:** `settlement_date`
- Amounts compared with **Decimal** arithmetic (no float rounding errors)

### Timing

| Scenario | Classification | Action |
|---|---|---|
| Bank settles after period end | Settled next month | None — reverses next month |
| No bank record, within window | Pending | None — still within SLA |
| No bank record, past window | Missing settlement | Investigate with bank |

### Rounding
- **Per-item tolerance** (default 0.01): differences within this → "Rounding"
- **Aggregate tolerance** (default 0.05) on **net** rounding across all items
- If net rounding exceeds aggregate tolerance → journal adjustment recommended

### Duplicates
- Same `txn_id` twice in one file = duplicate
- First copy matched normally; extras flagged
- Platform duplicates excluded from totals · bank duplicates = inflation

### Refunds
- **Orphan refund:** `refund_ref` not found in platform transactions
- **Valid refund:** references a known platform transaction (matched normally)

### Assumptions

| Assumption | Detail |
|---|---|
| Single currency per row | No FX conversion |
| Gross amounts | No fee netting |
| Line-level bank file | Bank uses platform `txn_id` as reference |
| Date format | YYYY-MM-DD |
| Refund sign | Negative amounts |

### Known limitations
No fee netting · no batch-only bank files · no partial/split refunds ·
no chargebacks · no FX · no timezone handling · no fuzzy matching
(different IDs, same amount/date)
""")
