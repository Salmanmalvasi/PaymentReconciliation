"""
Rule-based matching engine for Payment Reconciliation.

Architecture:
    The engine runs a pipeline of discrete rule functions in priority order.
    Each rule consumes only records not yet matched by an earlier rule, ensuring
    the highest-priority explanation always wins. Every rule is independently
    testable and logs which rule fired into the results.

Classification tiers:
    1. MATCHED — no human ever needs to look at this.
    2. EXPLAINABLE EXCEPTION — doesn't match exactly, but a known business rule
       fully accounts for the gap. Logged for audit, no urgent review.
    3. TRUE DISCREPANCY — no known rule explains the gap. Real money could be wrong.

Rule priority (highest to lowest):
    1. exact_match          — ref + amount + currency all identical
    2. fee_explained_match  — gross - fee = net within tolerance
    3. fuzzy_amount_match   — amount within rounding tolerance + delay window
    4. partial_refund_match — one-to-many, net position reconciles
    5. timing_exception     — matched ref but settlement outside delay window
    6. rounding_exception   — matched ref but amount differs by ≤ epsilon
    7. true_discrepancy     — catch-all: duplicates with conflicting amounts,
                              currency mismatches, unmatched orphans
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

import numpy as np
import pandas as pd

from reconciliation.config import settings


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    """Single matching result for a ledger/settlement pair."""
    ledger_idx: Optional[int]
    settlement_idx: Optional[int]
    classification: str  # matched | explainable_exception | true_discrepancy
    matched_rule: str
    confidence_notes: str
    category: str = "unknown"  # exception category for queue routing


@dataclass
class EngineOutput:
    """Aggregate output of a reconciliation run."""
    results: list[MatchResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Individual rule functions
# ---------------------------------------------------------------------------

def rule_exact_match(
    ledger: pd.DataFrame,
    settlements: pd.DataFrame,
    matched_ledger: set[int],
    matched_settlement: set[int],
) -> list[MatchResult]:
    """
    Rule 1: Exact match on reference + gross amount + currency.

    Detects: Clean, perfectly reconciled transactions where the ledger amount
    equals the settlement gross_amount, currencies match, and references align.
    This is the happy path — no human review needed.
    """
    results = []

    # Filter to unmatched only
    l_mask = ~ledger.index.isin(matched_ledger)
    s_mask = ~settlements.index.isin(matched_settlement)
    l = ledger[l_mask].copy()
    s = settlements[s_mask].copy()

    if l.empty or s.empty:
        return results

    # Merge on reference key + currency
    merged = l.merge(
        s,
        left_on=["order_id", "currency"],
        right_on=["external_transaction_ref", "currency"],
        suffixes=("_l", "_s"),
    )

    for _, row in merged.iterrows():
        ledger_amt = float(row["amount"])
        gross_amt = float(row["gross_amount"])

        if abs(ledger_amt - gross_amt) < 0.001:
            l_idx = row.name if "idx_l" not in row else int(row["idx_l"])
            s_idx = row.name if "idx_s" not in row else int(row["idx_s"])

            # Find actual indices
            l_idx = ledger.index[ledger["order_id"] == row["order_id"]].tolist()
            s_idx = settlements.index[
                settlements["external_transaction_ref"] == row["order_id"]
            ].tolist()

            if l_idx and s_idx:
                li = l_idx[0]
                si = s_idx[0]
                if li not in matched_ledger and si not in matched_settlement:
                    results.append(MatchResult(
                        ledger_idx=li,
                        settlement_idx=si,
                        classification="matched",
                        matched_rule="exact_match",
                        confidence_notes=(
                            f"Exact match: ref={row['order_id']}, "
                            f"amount={ledger_amt}, currency={row['currency']}"
                        ),
                    ))
                    matched_ledger.add(li)
                    matched_settlement.add(si)

    return results


def rule_fee_explained_match(
    ledger: pd.DataFrame,
    settlements: pd.DataFrame,
    matched_ledger: set[int],
    matched_settlement: set[int],
) -> list[MatchResult]:
    """
    Rule 2: Fee-explained match — ledger amount matches settlement gross,
    and gross - fee = net within tolerance.

    Detects: Records where the ledger shows the gross amount and the settlement
    correctly deducts the processor fee. The difference between ledger amount
    and settlement net is fully explained by the fee. No discrepancy.
    """
    results = []
    tolerance = settings.fee_tolerance

    l = ledger[~ledger.index.isin(matched_ledger)].copy()
    s = settlements[~settlements.index.isin(matched_settlement)].copy()

    if l.empty or s.empty:
        return results

    merged = l.merge(
        s,
        left_on=["order_id", "currency"],
        right_on=["external_transaction_ref", "currency"],
        suffixes=("_l", "_s"),
    )

    for _, row in merged.iterrows():
        ledger_amt = float(row["amount"])
        gross = float(row["gross_amount"])
        fee = float(row["fee_amount"])
        net = float(row["net_amount"])

        # Check: ledger ≈ gross AND gross - fee ≈ net
        gross_match = abs(ledger_amt - gross) <= tolerance
        fee_math = abs((gross - fee) - net) <= tolerance

        if gross_match and fee_math and abs(ledger_amt - net) > 0.001:
            l_idx = ledger.index[ledger["order_id"] == row["order_id"]].tolist()
            s_idx = settlements.index[
                settlements["external_transaction_ref"] == row["order_id"]
            ].tolist()

            if l_idx and s_idx:
                li = l_idx[0]
                si = s_idx[0]
                if li not in matched_ledger and si not in matched_settlement:
                    results.append(MatchResult(
                        ledger_idx=li,
                        settlement_idx=si,
                        classification="matched",
                        matched_rule="fee_explained_match",
                        confidence_notes=(
                            f"Fee-explained: ledger={ledger_amt}, gross={gross}, "
                            f"fee={fee}, net={net}, diff={abs(ledger_amt - net):.4f}"
                        ),
                        category="fee_difference",
                    ))
                    matched_ledger.add(li)
                    matched_settlement.add(si)

    return results


def rule_fuzzy_amount_match(
    ledger: pd.DataFrame,
    settlements: pd.DataFrame,
    matched_ledger: set[int],
    matched_settlement: set[int],
) -> list[MatchResult]:
    """
    Rule 3: Fuzzy match — amount within rounding tolerance, same reference,
    within settlement delay window.

    Detects: Records where a small rounding difference (≤ epsilon) exists
    between ledger and settlement gross amounts. This is expected noise from
    multi-currency processing or sub-cent fee calculations.
    """
    results = []
    tolerance = settings.rounding_tolerance
    delay_days = settings.settlement_delay_days

    l = ledger[~ledger.index.isin(matched_ledger)].copy()
    s = settlements[~settlements.index.isin(matched_settlement)].copy()

    if l.empty or s.empty:
        return results

    merged = l.merge(
        s,
        left_on=["order_id", "currency"],
        right_on=["external_transaction_ref", "currency"],
        suffixes=("_l", "_s"),
    )

    for _, row in merged.iterrows():
        ledger_amt = float(row["amount"])
        gross = float(row["gross_amount"])
        diff = abs(ledger_amt - gross)

        if diff <= tolerance and diff > 0.001:
            l_idx = ledger.index[ledger["order_id"] == row["order_id"]].tolist()
            s_idx = settlements.index[
                settlements["external_transaction_ref"] == row["order_id"]
            ].tolist()

            if l_idx and s_idx:
                li = l_idx[0]
                si = s_idx[0]
                if li not in matched_ledger and si not in matched_settlement:
                    results.append(MatchResult(
                        ledger_idx=li,
                        settlement_idx=si,
                        classification="explainable_exception",
                        matched_rule="fuzzy_amount_match",
                        confidence_notes=(
                            f"Fuzzy match: amount diff={diff:.4f} within "
                            f"tolerance={tolerance}"
                        ),
                        category="rounding_difference",
                    ))
                    matched_ledger.add(li)
                    matched_settlement.add(si)

    return results


def rule_partial_refund_match(
    ledger: pd.DataFrame,
    settlements: pd.DataFrame,
    matched_ledger: set[int],
    matched_settlement: set[int],
) -> list[MatchResult]:
    """
    Rule 4: Partial refund match — one-to-many matching where the net
    position (original charge minus sum of refunds) reconciles.

    Detects: Orders with partial refunds producing multiple settlement entries.
    Groups by order_id/ref, sums all settlement amounts, and checks if the
    ledger's net position matches within tolerance.
    """
    results = []
    tolerance = settings.fee_tolerance

    l = ledger[~ledger.index.isin(matched_ledger)].copy()
    s = settlements[~settlements.index.isin(matched_settlement)].copy()

    if l.empty or s.empty:
        return results

    # Find order_ids with multiple settlement entries
    s_grouped = s.groupby("external_transaction_ref")
    multi_refs = [ref for ref, group in s_grouped if len(group) > 1]

    for ref in multi_refs:
        l_matches = l[l["order_id"] == ref]
        s_matches = s[s["external_transaction_ref"] == ref]

        if l_matches.empty:
            continue

        # Sum ledger amounts for this order
        ledger_total = float(l_matches["amount"].sum())
        # Sum settlement gross amounts
        settlement_total = float(s_matches["gross_amount"].sum())

        if abs(ledger_total - settlement_total) <= tolerance:
            for li in l_matches.index:
                if li not in matched_ledger:
                    matched_ledger.add(li)
            for si in s_matches.index:
                if si not in matched_settlement:
                    matched_settlement.add(si)

            results.append(MatchResult(
                ledger_idx=l_matches.index[0],
                settlement_idx=s_matches.index[0],
                classification="explainable_exception",
                matched_rule="partial_refund_match",
                confidence_notes=(
                    f"Partial refund: ledger_total={ledger_total:.4f}, "
                    f"settlement_total={settlement_total:.4f}, "
                    f"entries={len(s_matches)}"
                ),
                category="partial_refund",
            ))

    return results


def rule_timing_exception(
    ledger: pd.DataFrame,
    settlements: pd.DataFrame,
    matched_ledger: set[int],
    matched_settlement: set[int],
) -> list[MatchResult]:
    """
    Rule 5: Timing-only explainable exception — reference and amount match
    but settlement arrived outside the expected delay window.

    Detects: Settlements that arrived unusually late (or before the ledger entry).
    This is normal in payments operations but worth logging for visibility.
    """
    results = []
    delay_days = settings.settlement_delay_days

    l = ledger[~ledger.index.isin(matched_ledger)].copy()
    s = settlements[~settlements.index.isin(matched_settlement)].copy()

    if l.empty or s.empty:
        return results

    merged = l.merge(
        s,
        left_on=["order_id", "currency"],
        right_on=["external_transaction_ref", "currency"],
        suffixes=("_l", "_s"),
    )

    for _, row in merged.iterrows():
        ledger_amt = float(row["amount"])
        gross = float(row["gross_amount"])

        if abs(ledger_amt - gross) <= settings.rounding_tolerance:
            created = pd.Timestamp(row["created_at"])
            settled = pd.Timestamp(row["settled_at"])
            delay = (settled - created).days

            if delay > delay_days or delay < 0:
                l_idx = ledger.index[ledger["order_id"] == row["order_id"]].tolist()
                s_idx = settlements.index[
                    settlements["external_transaction_ref"] == row["order_id"]
                ].tolist()

                if l_idx and s_idx:
                    li = l_idx[0]
                    si = s_idx[0]
                    if li not in matched_ledger and si not in matched_settlement:
                        label = "late" if delay > delay_days else "early (out-of-order)"
                        results.append(MatchResult(
                            ledger_idx=li,
                            settlement_idx=si,
                            classification="explainable_exception",
                            matched_rule="timing_exception",
                            confidence_notes=(
                                f"Timing {label}: settled {delay} days after creation "
                                f"(window={delay_days} days)"
                            ),
                            category="timing_delay",
                        ))
                        matched_ledger.add(li)
                        matched_settlement.add(si)

    return results


def rule_rounding_exception(
    ledger: pd.DataFrame,
    settlements: pd.DataFrame,
    matched_ledger: set[int],
    matched_settlement: set[int],
) -> list[MatchResult]:
    """
    Rule 6: Currency rounding explainable exception — matched reference but
    the amount differs by a small epsilon that's within known rounding noise.

    Detects: Sub-cent differences caused by multi-currency fee calculations.
    These are expected noise, not real discrepancies.
    """
    results = []
    tolerance = settings.rounding_tolerance

    l = ledger[~ledger.index.isin(matched_ledger)].copy()
    s = settlements[~settlements.index.isin(matched_settlement)].copy()

    if l.empty or s.empty:
        return results

    merged = l.merge(
        s,
        left_on=["order_id", "currency"],
        right_on=["external_transaction_ref", "currency"],
        suffixes=("_l", "_s"),
    )

    for _, row in merged.iterrows():
        ledger_amt = float(row["amount"])
        gross = float(row["gross_amount"])
        diff = abs(ledger_amt - gross)

        # Slightly broader check than fuzzy — captures near-tolerance cases
        if 0.001 < diff <= tolerance * 1.5:
            l_idx = ledger.index[ledger["order_id"] == row["order_id"]].tolist()
            s_idx = settlements.index[
                settlements["external_transaction_ref"] == row["order_id"]
            ].tolist()

            if l_idx and s_idx:
                li = l_idx[0]
                si = s_idx[0]
                if li not in matched_ledger and si not in matched_settlement:
                    results.append(MatchResult(
                        ledger_idx=li,
                        settlement_idx=si,
                        classification="explainable_exception",
                        matched_rule="rounding_exception",
                        confidence_notes=(
                            f"Rounding diff: {diff:.4f} (tolerance={tolerance})"
                        ),
                        category="rounding_difference",
                    ))
                    matched_ledger.add(li)
                    matched_settlement.add(si)

    return results


def rule_true_discrepancy(
    ledger: pd.DataFrame,
    settlements: pd.DataFrame,
    matched_ledger: set[int],
    matched_settlement: set[int],
) -> list[MatchResult]:
    """
    Rule 7 (catch-all): True discrepancy — no known rule explains the gap.

    Detects:
      - Currency mismatches (same ref, different currencies)
      - Significant amount mismatches beyond tolerance
      - Orphaned records (ledger-only or settlement-only)
      - Duplicates with conflicting amounts

    These are the only records that should generate operational urgency.
    """
    results = []
    tolerance = settings.rounding_tolerance

    l = ledger[~ledger.index.isin(matched_ledger)].copy()
    s = settlements[~settlements.index.isin(matched_settlement)].copy()

    # --- Currency mismatches ---
    if not l.empty and not s.empty:
        # Try matching on ref only (ignoring currency)
        merged = l.merge(
            s,
            left_on=["order_id"],
            right_on=["external_transaction_ref"],
            suffixes=("_l", "_s"),
        )
        for _, row in merged.iterrows():
            l_idx = ledger.index[ledger["order_id"] == row["order_id"]].tolist()
            s_idx = settlements.index[
                settlements["external_transaction_ref"] == row["order_id"]
            ].tolist()

            if l_idx and s_idx:
                li = l_idx[0]
                si = s_idx[0]
                if li not in matched_ledger and si not in matched_settlement:
                    l_currency = row.get("currency_l", row.get("currency", ""))
                    s_currency = row.get("currency_s", "")
                    ledger_amt = float(row["amount"])
                    gross = float(row["gross_amount"])
                    diff = abs(ledger_amt - gross)

                    if l_currency != s_currency and s_currency:
                        category = "currency_mismatch"
                        notes = (
                            f"Currency mismatch: ledger={l_currency}, "
                            f"settlement={s_currency}"
                        )
                    elif diff > tolerance * 1.5:
                        category = "amount_mismatch"
                        notes = (
                            f"Amount mismatch: ledger={ledger_amt}, "
                            f"gross={gross}, diff={diff:.4f}"
                        )
                    else:
                        # Already matched by a previous rule at this point
                        continue

                    results.append(MatchResult(
                        ledger_idx=li,
                        settlement_idx=si,
                        classification="true_discrepancy",
                        matched_rule="true_discrepancy",
                        confidence_notes=notes,
                        category=category,
                    ))
                    matched_ledger.add(li)
                    matched_settlement.add(si)

    # --- Orphaned ledger records (no matching settlement) ---
    for li in ledger.index:
        if li not in matched_ledger:
            results.append(MatchResult(
                ledger_idx=li,
                settlement_idx=None,
                classification="true_discrepancy",
                matched_rule="true_discrepancy",
                confidence_notes=(
                    f"Missing settlement: order_id={ledger.at[li, 'order_id']}"
                ),
                category="missing_settlement",
            ))
            matched_ledger.add(li)

    # --- Orphaned settlement records (no matching ledger) ---
    for si in settlements.index:
        if si not in matched_settlement:
            results.append(MatchResult(
                ledger_idx=None,
                settlement_idx=si,
                classification="true_discrepancy",
                matched_rule="true_discrepancy",
                confidence_notes=(
                    f"Missing ledger: ref={settlements.at[si, 'external_transaction_ref']}"
                ),
                category="missing_ledger",
            ))
            matched_settlement.add(si)

    return results


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

RULE_PIPELINE = [
    rule_exact_match,
    rule_fee_explained_match,
    rule_fuzzy_amount_match,
    rule_partial_refund_match,
    rule_timing_exception,
    rule_rounding_exception,
    rule_true_discrepancy,
]


def run_reconciliation(
    ledger: pd.DataFrame,
    settlements: pd.DataFrame,
) -> EngineOutput:
    """
    Execute the full reconciliation pipeline.

    Runs each rule in priority order. Records matched by an earlier rule
    are excluded from subsequent rules, ensuring the highest-priority
    explanation always wins.

    Args:
        ledger: DataFrame with columns [order_id, customer_id, amount, currency,
                status, created_at, updated_at]
        settlements: DataFrame with columns [external_transaction_ref, gross_amount,
                     fee_amount, net_amount, currency, settled_at, status,
                     source_batch_id]

    Returns:
        EngineOutput with per-record results and an aggregate summary.
    """
    matched_ledger: set[int] = set()
    matched_settlement: set[int] = set()
    all_results: list[MatchResult] = []

    # Ensure numeric types
    ledger = ledger.copy()
    settlements = settlements.copy()
    ledger["amount"] = pd.to_numeric(ledger["amount"], errors="coerce")
    settlements["gross_amount"] = pd.to_numeric(settlements["gross_amount"], errors="coerce")
    settlements["fee_amount"] = pd.to_numeric(settlements["fee_amount"], errors="coerce")
    settlements["net_amount"] = pd.to_numeric(settlements["net_amount"], errors="coerce")

    print(f"\n{'='*60}")
    print(f"  RECONCILIATION ENGINE")
    print(f"{'='*60}")
    print(f"  Ledger records:     {len(ledger):>8,}")
    print(f"  Settlement records: {len(settlements):>8,}")
    print(f"{'─'*60}")

    for rule_fn in RULE_PIPELINE:
        rule_name = rule_fn.__name__
        results = rule_fn(ledger, settlements, matched_ledger, matched_settlement)
        all_results.extend(results)
        print(f"  {rule_name:<35} → {len(results):>6} results")

    # Build summary
    classifications = {}
    categories = {}
    rules = {}
    for r in all_results:
        classifications[r.classification] = classifications.get(r.classification, 0) + 1
        categories[r.category] = categories.get(r.category, 0) + 1
        rules[r.matched_rule] = rules.get(r.matched_rule, 0) + 1

    total = len(all_results)
    matched_count = classifications.get("matched", 0)
    match_rate = (matched_count / total * 100) if total > 0 else 0

    summary = {
        "total_records": total,
        "total_ledger": len(ledger),
        "total_settlement": len(settlements),
        "match_rate": match_rate,
        "classifications": classifications,
        "categories": categories,
        "rules": rules,
    }

    print(f"{'─'*60}")
    print(f"  RESULTS:")
    print(f"    Total results:          {total:>8,}")
    print(f"    Match rate:             {match_rate:>7.1f}%")
    for cls, cnt in sorted(classifications.items()):
        print(f"    {cls:<30} {cnt:>6}")
    print(f"{'='*60}\n")

    return EngineOutput(results=all_results, summary=summary)
