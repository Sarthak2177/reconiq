# ReconIQ – platform vs bank settlement reconciliation

Explains *why* month-end platform totals differ from bank settlement totals, via a bridge that ends in an
**unexplained residual (must be 0.00)**.

## Run
```bash
pip install -r requirements-dev.txt   # runtime-only: requirements.txt
python -m reconiq.generator        # (re)writes data/*.csv and data/injected_anomalies.json
python -m pytest -q                # 19 tests
streamlit run app.py
```
## Deploy on Render (free)
1. Push this folder to a GitHub repo.
2. Render dashboard → **New → Blueprint** → select the repo (uses `render.yaml`), or **New → Web Service** with:
   build `pip install -r requirements.txt`, start `streamlit run app.py --server.port $PORT --server.address 0.0.0.0`, plan Free.
3. Free tier notes: ~512 MB RAM (this app uses well under that), the service sleeps after ~15 min idle and takes
   ~30-60 s to wake. Data is generated in memory at start; nothing is stored on disk, so no disk or database is needed.

## Layout
`reconiq/validation.py` typed input checks · `generator.py` seeded data + answer key · `engine.py` matching/bridge (no UI) ·
`app.py` Streamlit UI only · `tests/` pytest.

## Rules
- Platform month = `txn_date`; bank month = `settlement_date`. Match key = `txn_id` (exact).
- Amounts are `Decimal`. Per-item rounding tolerance 0.01; aggregate tolerance 0.05 on **net** rounding (gross also shown).
- Settlement window = 2 business days. No bank line and still inside the window at cut-off → *Pending*; outside → *Missing*.
- Bank line dated after cut-off → *Settled next month* (timing, no action). Not "missing".
- Duplicate = same `txn_id` twice in one file. First copy matched; extras excluded from platform totals / reported as bank-side inflation.
- Orphan refund = REFUND whose `refund_ref` is not any platform `txn_id` (prior-month originals count as known).
- Prior-month platform txns paid by the bank in the period = *Carried in* (reconciling item).
- Precedence: orphan refund overrides the settlement status; the bridge still uses money effect so the residual stays honest.

## Assumptions
Single currency per row, gross amounts (no fees), bank file is line-level with the platform `txn_id` as reference,
refunds negative, one date format (YYYY-MM-DD), uploaded files must include the Dec bank lines to prove timing items clear.

## Limitations
No fee netting, batch-only bank files, partial/multiple refunds exceeding the original, chargebacks, FX, timezones, or fuzzy
matching (different IDs, same amount). Settlement later than the window but inside the month is not flagged.
