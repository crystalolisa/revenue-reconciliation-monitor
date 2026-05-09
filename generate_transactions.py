"""
Revenue Reconciliation Monitor — Phase 1: Synthetic Transaction Generation
=========================================================================
Crystal Olisa · Operations Generalist

Generates two paired ledgers that simulate a fintech payment environment:

  processor_ledger.csv  — payment processor view (source of truth for transactions)
  revenue_ledger.csv    — internal revenue records (what the business believes it earned)

The gap between these two ledgers is where revenue leakage lives.

Mismatch types seeded (calibrated against industry benchmarks):
  Type A — Missing entry:      Transaction succeeded at processor, no revenue record exists
  Type B — Amount mismatch:    Revenue record exists but amount differs (rounding, FX, fee deduction)
  Type C — Period shift:       Revenue booked in wrong accounting period (timing gap)
  Type D — Duplicate revenue:  Revenue recorded twice for a single transaction
  Type E — Matched (clean):    Both ledgers agree — no gap

Calibration sources:
  - Stripe reconciliation gap rate: ~2–4% of transaction volume (Stripe Treasury docs, 2023)
  - Period shift rate: ~1.5% in high-volume SaaS environments (Zuora State of the Subscription Economy)
  - Duplicate booking rate: ~0.5% (internal ops benchmarks, published fintech post-mortems)

Seeds:
  - Transaction generation: numpy seed 42
  - Mismatch injection:     separate numpy.default_rng(seed=77) — preserves transaction profile

Output: data/processor_ledger.csv, data/revenue_ledger.csv
Next step: run pipeline/reconcile.py
"""

import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
import uuid

np.random.seed(42)
mismatch_rng = np.random.default_rng(seed=77)

# ── Configuration ──────────────────────────────────────────────────────────
N_TRANSACTIONS      = 2_000
START_DATE          = datetime(2024, 1, 1)
END_DATE            = datetime(2024, 12, 31)
OUTPUT_DIR          = Path(__file__).parent / "data"
OUTPUT_DIR.mkdir(exist_ok=True)

# Mismatch rates (calibrated to industry benchmarks)
MISSING_RATE        = 0.025   # 2.5% — no revenue entry for a succeeded transaction
AMOUNT_MISMATCH_RATE = 0.015  # 1.5% — fee deduction, FX rounding, partial capture
PERIOD_SHIFT_RATE   = 0.015   # 1.5% — booked in wrong period
DUPLICATE_RATE      = 0.005   # 0.5% — double-booked revenue entry
# Remaining ~95.5% are clean matched records

# Product mix — simulates a multi-product SaaS/fintech platform
PRODUCTS = {
    "subscription_monthly":  {"weight": 0.40, "amount_range": (29,  299),  "currency": "USD"},
    "subscription_annual":   {"weight": 0.20, "amount_range": (290, 2990), "currency": "USD"},
    "transaction_fee":       {"weight": 0.25, "amount_range": (1,   150),  "currency": "USD"},
    "api_usage":             {"weight": 0.10, "amount_range": (5,   500),  "currency": "USD"},
    "professional_services": {"weight": 0.05, "amount_range": (500, 5000), "currency": "USD"},
}

# Payment processors
PROCESSORS = ["Stripe", "Adyen", "PayPal", "Square", "Flutterwave"]
PROCESSOR_WEIGHTS = [0.45, 0.25, 0.15, 0.10, 0.05]

# Customer segments
SEGMENTS = ["SMB", "Mid-Market", "Enterprise"]
SEGMENT_WEIGHTS = [0.60, 0.30, 0.10]

# ── Generate transaction IDs and dates ─────────────────────────────────────
date_range_days = (END_DATE - START_DATE).days
tx_dates = [START_DATE + timedelta(days=int(d))
            for d in np.random.randint(0, date_range_days, N_TRANSACTIONS)]
tx_dates.sort()

tx_ids = [f"TX{str(i+1).zfill(6)}" for i in range(N_TRANSACTIONS)]

# ── Generate product assignments ───────────────────────────────────────────
product_names   = list(PRODUCTS.keys())
product_weights = [PRODUCTS[p]["weight"] for p in product_names]
products_assigned = np.random.choice(product_names, size=N_TRANSACTIONS, p=product_weights)

# ── Generate amounts ───────────────────────────────────────────────────────
amounts = []
for product in products_assigned:
    lo, hi = PRODUCTS[product]["amount_range"]
    amounts.append(round(np.random.uniform(lo, hi), 2))
amounts = np.array(amounts)

# ── Generate processor, customer, segment ──────────────────────────────────
processors = np.random.choice(PROCESSORS, size=N_TRANSACTIONS, p=PROCESSOR_WEIGHTS)
customer_ids = [f"CUST{str(np.random.randint(1, 500)).zfill(4)}" for _ in range(N_TRANSACTIONS)]
segments = np.random.choice(SEGMENTS, size=N_TRANSACTIONS, p=SEGMENT_WEIGHTS)

# ── Build processor ledger (source of truth) ───────────────────────────────
processor_ledger = pd.DataFrame({
    "transaction_id":   tx_ids,
    "transaction_date": [d.strftime("%Y-%m-%d") for d in tx_dates],
    "customer_id":      customer_ids,
    "product":          products_assigned,
    "amount_usd":       amounts,
    "currency":         "USD",
    "processor":        processors,
    "segment":          segments,
    "status":           "succeeded",
    "processor_ref":    [str(uuid.uuid4())[:12].upper() for _ in range(N_TRANSACTIONS)],
})

# ── Seed mismatches using separate RNG ─────────────────────────────────────
n = N_TRANSACTIONS
indices = np.arange(n)
mismatch_rng.shuffle(indices)

# Assign mismatch types
n_missing   = int(n * MISSING_RATE)
n_amount    = int(n * AMOUNT_MISMATCH_RATE)
n_period    = int(n * PERIOD_SHIFT_RATE)
n_duplicate = int(n * DUPLICATE_RATE)

missing_idx   = set(indices[:n_missing])
amount_idx    = set(indices[n_missing:n_missing+n_amount])
period_idx    = set(indices[n_missing+n_amount:n_missing+n_amount+n_period])
duplicate_idx = set(indices[n_missing+n_amount+n_period:n_missing+n_amount+n_period+n_duplicate])

# ── Product to revenue category mapping ───────────────────────────────────
def _map_product_to_category(product):
    mapping = {
        "subscription_monthly":  "Recurring Revenue",
        "subscription_annual":   "Recurring Revenue",
        "transaction_fee":       "Transaction Revenue",
        "api_usage":             "Usage Revenue",
        "professional_services": "Services Revenue",
    }
    return mapping.get(product, "Other")


# ── Build revenue ledger ────────────────────────────────────────────────────
revenue_rows = []

for i, row in processor_ledger.iterrows():
    tx_id    = row["transaction_id"]
    tx_date  = datetime.strptime(row["transaction_date"], "%Y-%m-%d")
    amount   = row["amount_usd"]
    product  = row["product"]
    cust_id  = row["customer_id"]

    if i in missing_idx:
        # Type A — no revenue entry: skip this transaction entirely
        continue

    elif i in amount_idx:
        # Type B — amount mismatch: fee deducted or FX rounding
        variance_pct = mismatch_rng.uniform(0.005, 0.04)  # 0.5%–4% variance
        direction    = mismatch_rng.choice([-1, 1])
        booked_amount = round(amount * (1 + direction * variance_pct), 2)
        mismatch_type = "amount_mismatch"
        booked_date   = tx_date

    elif i in period_idx:
        # Type C — period shift: booked 1–45 days late
        shift_days    = int(mismatch_rng.integers(1, 46))
        booked_date   = tx_date + timedelta(days=shift_days)
        booked_amount = amount
        mismatch_type = "period_shift"

    elif i in duplicate_idx:
        # Type D — duplicate: same transaction booked twice
        booked_amount = amount
        booked_date   = tx_date
        mismatch_type = "duplicate"
        # Add the first entry
        revenue_rows.append({
            "revenue_record_id": f"REV{str(len(revenue_rows)+1).zfill(6)}",
            "transaction_id":    tx_id,
            "booking_date":      booked_date.strftime("%Y-%m-%d"),
            "customer_id":       cust_id,
            "product":           product,
            "booked_amount_usd": booked_amount,
            "revenue_category":  _map_product_to_category(product),
            "mismatch_type":     mismatch_type,
        })
        # Second (duplicate) entry — small date variation
        dup_date = booked_date + timedelta(days=int(mismatch_rng.integers(0, 3)))
        revenue_rows.append({
            "revenue_record_id": f"REV{str(len(revenue_rows)+1).zfill(6)}",
            "transaction_id":    tx_id,
            "booking_date":      dup_date.strftime("%Y-%m-%d"),
            "customer_id":       cust_id,
            "product":           product,
            "booked_amount_usd": booked_amount,
            "revenue_category":  _map_product_to_category(product),
            "mismatch_type":     "duplicate",
        })
        continue

    else:
        # Type E — clean match
        booked_amount = amount
        booked_date   = tx_date
        mismatch_type = "matched"

    revenue_rows.append({
        "revenue_record_id": f"REV{str(len(revenue_rows)+1).zfill(6)}",
        "transaction_id":    tx_id,
        "booking_date":      booked_date.strftime("%Y-%m-%d"),
        "customer_id":       cust_id,
        "product":           product,
        "booked_amount_usd": booked_amount,
        "revenue_category":  _map_product_to_category(product),
        "mismatch_type":     mismatch_type,
    })


revenue_ledger = pd.DataFrame(revenue_rows)

# ── Save outputs ───────────────────────────────────────────────────────────
processor_ledger.to_csv(OUTPUT_DIR / "processor_ledger.csv", index=False)
revenue_ledger.to_csv(OUTPUT_DIR / "revenue_ledger.csv", index=False)

# ── Summary ────────────────────────────────────────────────────────────────
print(f"Processor ledger: {len(processor_ledger):,} transactions")
print(f"Revenue ledger:   {len(revenue_ledger):,} records")
print()
print("Mismatch distribution (seeded):")
print(f"  Type A — Missing entries:    {n_missing}  ({n_missing/n*100:.1f}%)")
print(f"  Type B — Amount mismatch:    {n_amount}   ({n_amount/n*100:.1f}%)")
print(f"  Type C — Period shift:       {n_period}   ({n_period/n*100:.1f}%)")
print(f"  Type D — Duplicate booking:  {n_duplicate} ({n_duplicate/n*100:.1f}%)")
print(f"  Type E — Clean matches:      {n - n_missing - n_amount - n_period - n_duplicate}")
print()
print(f"Total revenue at risk (Types A+B): "
      f"${processor_ledger.iloc[list(missing_idx | amount_idx)]['amount_usd'].sum():,.2f}")
print()
print("Output:")
print(f"  data/processor_ledger.csv")
print(f"  data/revenue_ledger.csv")
print()
print("Next step: run pipeline/reconcile.py")
