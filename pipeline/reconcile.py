"""
Revenue Reconciliation Monitor — Phase 2: Reconciliation Pipeline
=================================================================
Crystal Olisa · Operations Generalist

Matches the processor ledger against the revenue ledger to surface
every gap between what the payment processor recorded and what the
business booked as revenue.

Five gap types detected:

  Type A — Missing entry:    Transaction in processor ledger, no revenue record
  Type B — Amount mismatch:  Revenue record exists but amount differs by > 0.5%
  Type C — Period shift:     Revenue booked in a different calendar month
  Type D — Duplicate:        Same transaction ID appears more than once in revenue ledger
  Type E — Matched:          Both ledgers agree — no gap

Output: data/reconciliation_results.csv
Next step: run notebooks/revenue_reconciliation_monitor.ipynb
"""

import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR   = Path(__file__).parent.parent / "data"
AMOUNT_TOLERANCE_PCT = 0.005  # 0.5% — below this, amount variance is within rounding tolerance

def load_ledgers():
    processor = pd.read_csv(DATA_DIR / "processor_ledger.csv", parse_dates=["transaction_date"])
    revenue   = pd.read_csv(DATA_DIR / "revenue_ledger.csv",   parse_dates=["booking_date"])
    print(f"Loaded processor ledger: {len(processor):,} transactions")
    print(f"Loaded revenue ledger:   {len(revenue):,} records")
    return processor, revenue


def detect_duplicates(revenue):
    """Gate 1 (non-blocking): flag transaction IDs appearing more than once."""
    dup_counts = revenue.groupby("transaction_id").size()
    dup_ids    = set(dup_counts[dup_counts > 1].index)
    flagged    = revenue[revenue["transaction_id"].isin(dup_ids)].copy()
    flagged["gap_type"]    = "duplicate"
    flagged["gap_detail"]  = flagged["transaction_id"].map(
        lambda x: f"Transaction {x} appears {dup_counts[x]}x in revenue ledger"
    )
    print(f"Gate 1 — Duplicate detection: {len(dup_ids)} transaction IDs with duplicate revenue entries")
    return dup_ids


def reconcile(processor, revenue, dup_ids):
    """
    Core reconciliation logic.
    For each processor transaction, find its revenue counterpart(s).
    Classify the gap type based on match quality.
    """
    # Use the first (earliest booking) revenue record per transaction for matching
    # Duplicates are flagged separately below — we keep first record for amount/period checks
    rev_deduped = (
        revenue[~revenue["transaction_id"].isin(dup_ids)]
        .sort_values("booking_date")
        .drop_duplicates(subset="transaction_id", keep="first")
    )

    # Merge processor to revenue on transaction_id
    merged = processor.merge(
        rev_deduped[["transaction_id", "booking_date", "booked_amount_usd", "revenue_category"]],
        on="transaction_id",
        how="left"
    )

    results = []

    # ── Process duplicates first ───────────────────────────────────────────
    # For each duplicate tx_id: output one row per extra occurrence
    # The first record is treated as legitimate; each additional one is the gap
    dup_revenue = revenue[revenue["transaction_id"].isin(dup_ids)].sort_values("booking_date")
    dup_proc    = processor.set_index("transaction_id")

    for tx_id, group in dup_revenue.groupby("transaction_id"):
        if tx_id not in dup_proc.index:
            continue
        proc_row      = dup_proc.loc[tx_id]
        tx_date       = proc_row["transaction_date"]
        proc_amount   = proc_row["amount_usd"]
        product       = proc_row["product"]
        segment       = proc_row["segment"]
        processor_name = proc_row["processor"]

        # First occurrence — matched
        first = group.iloc[0]
        results.append({
            "transaction_id":   tx_id,
            "transaction_date": tx_date.strftime("%Y-%m-%d"),
            "processor":        processor_name,
            "product":          product,
            "segment":          segment,
            "processor_amount": proc_amount,
            "booked_amount":    first["booked_amount_usd"],
            "variance_usd":     0,
            "variance_pct":     0,
            "gap_type":         "matched",
            "gap_detail":       None,
            "processor_month":  tx_date.strftime("%Y-%m"),
            "booking_month":    first["booking_date"].strftime("%Y-%m"),
            "days_to_detect":   (first["booking_date"] - tx_date).days,
            "revenue_at_risk":  0,
        })

        # Each additional occurrence — duplicate gap
        for _, extra in group.iloc[1:].iterrows():
            results.append({
                "transaction_id":   tx_id,
                "transaction_date": tx_date.strftime("%Y-%m-%d"),
                "processor":        processor_name,
                "product":          product,
                "segment":          segment,
                "processor_amount": proc_amount,
                "booked_amount":    extra["booked_amount_usd"],
                "variance_usd":     extra["booked_amount_usd"],
                "variance_pct":     None,
                "gap_type":         "duplicate",
                "gap_detail":       f"Revenue booked {len(group)}x for this transaction — {len(group)-1} extra occurrence(s)",
                "processor_month":  tx_date.strftime("%Y-%m"),
                "booking_month":    extra["booking_date"].strftime("%Y-%m"),
                "days_to_detect":   (extra["booking_date"] - tx_date).days,
                "revenue_at_risk":  extra["booked_amount_usd"],
            })

    for _, row in merged.iterrows():
        tx_id          = row["transaction_id"]
        tx_date        = row["transaction_date"]
        proc_amount    = row["amount_usd"]
        booking_date   = row["booking_date"]
        booked_amount  = row["booked_amount_usd"]
        product        = row["product"]
        segment        = row["segment"]
        processor_name = row["processor"]

        # ── Type A: Missing entry ──────────────────────────────────────
        if pd.isna(booked_amount):
            results.append({
                "transaction_id":      tx_id,
                "transaction_date":    tx_date.strftime("%Y-%m-%d"),
                "processor":           processor_name,
                "product":             product,
                "segment":             segment,
                "processor_amount":    proc_amount,
                "booked_amount":       None,
                "variance_usd":        proc_amount,
                "variance_pct":        None,
                "gap_type":            "missing",
                "gap_detail":          "Transaction succeeded at processor — no revenue record found",
                "processor_month":     tx_date.strftime("%Y-%m"),
                "booking_month":       None,
                "days_to_detect":      None,
                "revenue_at_risk":     proc_amount,
            })
            continue

        # ── Type B/C/E: Amount mismatch, period shift, matched ────────
        variance_usd = round(booked_amount - proc_amount, 2)
        variance_pct = abs(variance_usd) / proc_amount if proc_amount > 0 else 0
        tx_month     = tx_date.strftime("%Y-%m")
        book_month   = booking_date.strftime("%Y-%m")
        days_gap     = (booking_date - tx_date).days

        # ── Type C: Period shift ──────────────────────────────────────
        if tx_month != book_month:
            results.append({
                "transaction_id":      tx_id,
                "transaction_date":    tx_date.strftime("%Y-%m-%d"),
                "processor":           processor_name,
                "product":             product,
                "segment":             segment,
                "processor_amount":    proc_amount,
                "booked_amount":       booked_amount,
                "variance_usd":        variance_usd,
                "variance_pct":        round(variance_pct * 100, 2),
                "gap_type":            "period_shift",
                "gap_detail":          f"Transaction in {tx_month}, revenue booked in {book_month} ({days_gap} days later)",
                "processor_month":     tx_month,
                "booking_month":       book_month,
                "days_to_detect":      days_gap,
                "revenue_at_risk":     0,  # revenue exists, timing is the issue
            })
            continue

        # ── Type B: Amount mismatch ───────────────────────────────────
        if variance_pct > AMOUNT_TOLERANCE_PCT:
            results.append({
                "transaction_id":      tx_id,
                "transaction_date":    tx_date.strftime("%Y-%m-%d"),
                "processor":           processor_name,
                "product":             product,
                "segment":             segment,
                "processor_amount":    proc_amount,
                "booked_amount":       booked_amount,
                "variance_usd":        variance_usd,
                "variance_pct":        round(variance_pct * 100, 2),
                "gap_type":            "amount_mismatch",
                "gap_detail":          f"Booked ${booked_amount:.2f} vs processor ${proc_amount:.2f} ({variance_pct*100:.2f}% variance)",
                "processor_month":     tx_month,
                "booking_month":       book_month,
                "days_to_detect":      days_gap,
                "revenue_at_risk":     abs(variance_usd),
            })
            continue

        # ── Type E: Clean match ───────────────────────────────────────
        results.append({
            "transaction_id":      tx_id,
            "transaction_date":    tx_date.strftime("%Y-%m-%d"),
            "processor":           processor_name,
            "product":             product,
            "segment":             segment,
            "processor_amount":    proc_amount,
            "booked_amount":       booked_amount,
            "variance_usd":        variance_usd,
            "variance_pct":        round(variance_pct * 100, 2),
            "gap_type":            "matched",
            "gap_detail":          None,
            "processor_month":     tx_month,
            "booking_month":       book_month,
            "days_to_detect":      days_gap,
            "revenue_at_risk":     0,
        })

    return pd.DataFrame(results)


def print_summary(results):
    print()
    print("=" * 60)
    print("RECONCILIATION SUMMARY")
    print("=" * 60)
    print(f"Total transactions reconciled: {len(results):,}")
    print()
    print("Gap type distribution:")
    gap_counts = results["gap_type"].value_counts()
    for gap_type, count in gap_counts.items():
        pct = count / len(results) * 100
        print(f"  {gap_type:<20} {count:>4}  ({pct:.1f}%)")
    print()

    at_risk = results[results["gap_type"].isin(["missing", "amount_mismatch", "duplicate"])]
    total_at_risk = at_risk["revenue_at_risk"].sum()
    print(f"Total revenue at risk:         ${total_at_risk:,.2f}")
    print()

    period_gaps = results[results["gap_type"] == "period_shift"]
    if len(period_gaps) > 0:
        avg_shift = period_gaps["days_to_detect"].mean()
        max_shift = period_gaps["days_to_detect"].max()
        print(f"Period shift — avg days late:  {avg_shift:.0f} days")
        print(f"Period shift — max days late:  {max_shift:.0f} days")
        print()

    print("Revenue at risk by product:")
    by_product = (
        at_risk.groupby("product")["revenue_at_risk"]
        .sum()
        .sort_values(ascending=False)
    )
    for product, amount in by_product.items():
        print(f"  {product:<30} ${amount:,.2f}")
    print()
    print("Output: data/reconciliation_results.csv")
    print("Next step: run notebooks/revenue_reconciliation_monitor.ipynb")


def run():
    processor, revenue = load_ledgers()
    dup_ids = detect_duplicates(revenue)
    results = reconcile(processor, revenue, dup_ids)
    results.to_csv(DATA_DIR / "reconciliation_results.csv", index=False)
    print_summary(results)


if __name__ == "__main__":
    run()
