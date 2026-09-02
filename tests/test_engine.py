"""
Unit tests for each matching rule in the reconciliation engine.

Each test creates a small synthetic fixture targeting one specific anomaly
type and verifies the correct rule fires with the correct classification.
"""

import pandas as pd
import pytest
from datetime import datetime, timezone, timedelta

from reconciliation.engine import (
    rule_exact_match,
    rule_fee_explained_match,
    rule_fuzzy_amount_match,
    rule_partial_refund_match,
    rule_timing_exception,
    rule_rounding_exception,
    rule_true_discrepancy,
    run_reconciliation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ledger(rows: list[dict]) -> pd.DataFrame:
    """Create a ledger DataFrame from dicts."""
    defaults = {
        "customer_id": "CUST-1234",
        "status": "completed",
        "created_at": datetime(2024, 6, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2024, 6, 1, tzinfo=timezone.utc),
    }
    for row in rows:
        for k, v in defaults.items():
            row.setdefault(k, v)
    return pd.DataFrame(rows)


def make_settlement(rows: list[dict]) -> pd.DataFrame:
    """Create a settlement DataFrame from dicts."""
    defaults = {
        "fee_amount": 3.00,
        "status": "settled",
        "source_batch_id": "BATCH-001",
        "settled_at": datetime(2024, 6, 3, tzinfo=timezone.utc),
    }
    for row in rows:
        for k, v in defaults.items():
            row.setdefault(k, v)
        if "net_amount" not in row:
            row["net_amount"] = row["gross_amount"] - row["fee_amount"]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Rule 1: Exact Match
# ---------------------------------------------------------------------------

class TestExactMatch:
    def test_perfect_match(self):
        """Exact ref + amount + currency → matched."""
        ledger = make_ledger([
            {"order_id": "ORD-001", "amount": 100.00, "currency": "USD"},
        ])
        settlements = make_settlement([
            {"external_transaction_ref": "ORD-001", "gross_amount": 100.00, "currency": "USD"},
        ])
        matched_l, matched_s = set(), set()
        results = rule_exact_match(ledger, settlements, matched_l, matched_s)

        assert len(results) == 1
        assert results[0].classification == "matched"
        assert results[0].matched_rule == "exact_match"
        assert 0 in matched_l
        assert 0 in matched_s

    def test_no_match_different_amount(self):
        """Different amounts → no exact match."""
        ledger = make_ledger([
            {"order_id": "ORD-001", "amount": 100.00, "currency": "USD"},
        ])
        settlements = make_settlement([
            {"external_transaction_ref": "ORD-001", "gross_amount": 99.00, "currency": "USD"},
        ])
        matched_l, matched_s = set(), set()
        results = rule_exact_match(ledger, settlements, matched_l, matched_s)
        assert len(results) == 0

    def test_no_match_different_currency(self):
        """Different currencies → no exact match."""
        ledger = make_ledger([
            {"order_id": "ORD-001", "amount": 100.00, "currency": "USD"},
        ])
        settlements = make_settlement([
            {"external_transaction_ref": "ORD-001", "gross_amount": 100.00, "currency": "EUR"},
        ])
        matched_l, matched_s = set(), set()
        results = rule_exact_match(ledger, settlements, matched_l, matched_s)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Rule 2: Fee-Explained Match
# ---------------------------------------------------------------------------

class TestFeeExplainedMatch:
    def test_fee_deducted(self):
        """Ledger = gross, settlement shows fee deduction → matched."""
        ledger = make_ledger([
            {"order_id": "ORD-002", "amount": 100.00, "currency": "USD"},
        ])
        settlements = make_settlement([
            {
                "external_transaction_ref": "ORD-002",
                "gross_amount": 100.00,
                "fee_amount": 2.90,
                "net_amount": 97.10,
                "currency": "USD",
            },
        ])
        matched_l, matched_s = set(), set()
        results = rule_fee_explained_match(ledger, settlements, matched_l, matched_s)

        assert len(results) == 1
        assert results[0].classification == "matched"
        assert results[0].matched_rule == "fee_explained_match"


# ---------------------------------------------------------------------------
# Rule 3: Fuzzy Amount Match
# ---------------------------------------------------------------------------

class TestFuzzyAmountMatch:
    def test_small_rounding_diff(self):
        """Amount within rounding tolerance → explainable exception."""
        ledger = make_ledger([
            {"order_id": "ORD-003", "amount": 100.00, "currency": "USD"},
        ])
        settlements = make_settlement([
            {"external_transaction_ref": "ORD-003", "gross_amount": 100.01, "currency": "USD"},
        ])
        matched_l, matched_s = set(), set()
        results = rule_fuzzy_amount_match(ledger, settlements, matched_l, matched_s)

        assert len(results) == 1
        assert results[0].classification == "explainable_exception"
        assert results[0].matched_rule == "fuzzy_amount_match"

    def test_beyond_tolerance(self):
        """Amount difference too large → no fuzzy match."""
        ledger = make_ledger([
            {"order_id": "ORD-003B", "amount": 100.00, "currency": "USD"},
        ])
        settlements = make_settlement([
            {"external_transaction_ref": "ORD-003B", "gross_amount": 100.50, "currency": "USD"},
        ])
        matched_l, matched_s = set(), set()
        results = rule_fuzzy_amount_match(ledger, settlements, matched_l, matched_s)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Rule 4: Partial Refund Match
# ---------------------------------------------------------------------------

class TestPartialRefundMatch:
    def test_multiple_settlement_entries(self):
        """Multiple settlement entries that sum to ledger total → explainable exception."""
        ledger = make_ledger([
            {"order_id": "ORD-004", "amount": 100.00, "currency": "USD"},
        ])
        settlements = make_settlement([
            {"external_transaction_ref": "ORD-004", "gross_amount": 70.00, "currency": "USD"},
            {"external_transaction_ref": "ORD-004", "gross_amount": 30.00, "currency": "USD"},
        ])
        matched_l, matched_s = set(), set()
        results = rule_partial_refund_match(ledger, settlements, matched_l, matched_s)

        assert len(results) == 2
        assert all(r.classification == "explainable_exception" for r in results)
        assert all(r.matched_rule == "partial_refund_match" for r in results)
        assert results[0].ledger_idx == 0
        assert results[0].settlement_idx == 0
        assert results[1].ledger_idx is None
        assert results[1].settlement_idx == 1


# ---------------------------------------------------------------------------
# Rule 5: Timing Exception
# ---------------------------------------------------------------------------

class TestTimingException:
    def test_late_settlement(self):
        """Settlement arrives after delay window → explainable exception."""
        created = datetime(2024, 6, 1, tzinfo=timezone.utc)
        settled = created + timedelta(days=10)  # > 5 day window
        ledger = make_ledger([
            {"order_id": "ORD-005", "amount": 100.00, "currency": "USD", "created_at": created},
        ])
        settlements = make_settlement([
            {
                "external_transaction_ref": "ORD-005",
                "gross_amount": 100.00,
                "currency": "USD",
                "settled_at": settled,
            },
        ])
        matched_l, matched_s = set(), set()
        results = rule_timing_exception(ledger, settlements, matched_l, matched_s)

        assert len(results) == 1
        assert results[0].classification == "explainable_exception"
        assert results[0].matched_rule == "timing_exception"

    def test_out_of_order(self):
        """Settlement before ledger creation → explainable exception."""
        created = datetime(2024, 6, 5, tzinfo=timezone.utc)
        settled = datetime(2024, 6, 3, tzinfo=timezone.utc)  # before creation
        ledger = make_ledger([
            {"order_id": "ORD-005B", "amount": 100.00, "currency": "USD", "created_at": created},
        ])
        settlements = make_settlement([
            {
                "external_transaction_ref": "ORD-005B",
                "gross_amount": 100.00,
                "currency": "USD",
                "settled_at": settled,
            },
        ])
        matched_l, matched_s = set(), set()
        results = rule_timing_exception(ledger, settlements, matched_l, matched_s)

        assert len(results) == 1
        assert "early" in results[0].confidence_notes


# ---------------------------------------------------------------------------
# Rule 6: Rounding Exception
# ---------------------------------------------------------------------------

class TestRoundingException:
    def test_subcent_diff(self):
        """Sub-cent rounding difference → explainable exception."""
        ledger = make_ledger([
            {"order_id": "ORD-006", "amount": 100.00, "currency": "USD"},
        ])
        settlements = make_settlement([
            {"external_transaction_ref": "ORD-006", "gross_amount": 100.02, "currency": "USD"},
        ])
        matched_l, matched_s = set(), set()
        results = rule_rounding_exception(ledger, settlements, matched_l, matched_s)

        assert len(results) == 1
        assert results[0].classification == "explainable_exception"
        assert results[0].matched_rule == "rounding_exception"


# ---------------------------------------------------------------------------
# Rule 7: True Discrepancy (catch-all)
# ---------------------------------------------------------------------------

class TestTrueDiscrepancy:
    def test_missing_settlement(self):
        """Ledger record with no settlement → true discrepancy."""
        ledger = make_ledger([
            {"order_id": "ORD-ORPHAN", "amount": 100.00, "currency": "USD"},
        ])
        settlements = make_settlement([])  # empty
        matched_l, matched_s = set(), set()
        results = rule_true_discrepancy(ledger, settlements, matched_l, matched_s)

        assert len(results) >= 1
        assert any(r.classification == "true_discrepancy" for r in results)
        assert any("Missing settlement" in r.confidence_notes for r in results)

    def test_missing_ledger(self):
        """Settlement record with no ledger → true discrepancy."""
        ledger = make_ledger([])
        settlements = make_settlement([
            {"external_transaction_ref": "EXT-ORPHAN", "gross_amount": 100.00, "currency": "USD"},
        ])
        matched_l, matched_s = set(), set()
        results = rule_true_discrepancy(ledger, settlements, matched_l, matched_s)

        assert len(results) >= 1
        assert any(r.classification == "true_discrepancy" for r in results)
        assert any("Missing ledger" in r.confidence_notes for r in results)

    def test_currency_mismatch(self):
        """Same ref, different currencies → true discrepancy."""
        ledger = make_ledger([
            {"order_id": "ORD-CURR", "amount": 100.00, "currency": "USD"},
        ])
        settlements = make_settlement([
            {"external_transaction_ref": "ORD-CURR", "gross_amount": 100.00, "currency": "EUR"},
        ])
        matched_l, matched_s = set(), set()
        results = rule_true_discrepancy(ledger, settlements, matched_l, matched_s)

        assert len(results) >= 1
        assert any(r.category == "currency_mismatch" for r in results)


# ---------------------------------------------------------------------------
# Rule priority test
# ---------------------------------------------------------------------------

class TestRulePriority:
    def test_exact_match_beats_fee_explained(self):
        """A record matching Rule 1 (exact) should NOT also be classified by Rule 2 (fee)."""
        ledger = make_ledger([
            {"order_id": "ORD-PRIO", "amount": 100.00, "currency": "USD"},
        ])
        settlements = make_settlement([
            {
                "external_transaction_ref": "ORD-PRIO",
                "gross_amount": 100.00,
                "fee_amount": 2.50,
                "net_amount": 97.50,
                "currency": "USD",
            },
        ])
        output = run_reconciliation(ledger, settlements)

        # Should have exactly 1 result, matched by exact_match (highest priority)
        assert len(output.results) == 1
        assert output.results[0].matched_rule == "exact_match"
        assert output.results[0].classification == "matched"


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_full_pipeline_with_generator(self):
        """Run generator → engine and verify classification counts are reasonable."""
        from reconciliation.generator import generate

        _, ledger_df, settlement_df, injected_counts = generate(
            num_records=500,
            anomaly_rate=0.15,
            seed=42,
            output_dir="/tmp/recon_test",
        )

        output = run_reconciliation(ledger_df, settlement_df)

        # Basic sanity checks
        assert len(output.results) > 0
        assert output.summary["match_rate"] > 0

        # Should have all three classification types
        assert "matched" in output.summary["classifications"]

        # Check invariant: every ledger and settlement row is covered exactly once
        l_covered = {r.ledger_idx for r in output.results if r.ledger_idx is not None}
        s_covered = {r.settlement_idx for r in output.results if r.settlement_idx is not None}
        
        assert len(l_covered) == len(ledger_df), f"Expected {len(ledger_df)} ledger rows covered, got {len(l_covered)}"
        assert len(s_covered) == len(settlement_df), f"Expected {len(settlement_df)} settlement rows covered, got {len(s_covered)}"
        
        # Verify no duplicate indices (one result per row)
        l_all = [r.ledger_idx for r in output.results if r.ledger_idx is not None]
        s_all = [r.settlement_idx for r in output.results if r.settlement_idx is not None]
        assert len(l_all) == len(l_covered), "Duplicate ledger_idx found in results"
        assert len(s_all) == len(s_covered), "Duplicate settlement_idx found in results"

        # Total results should account for all records
        total_classified = sum(output.summary["classifications"].values())
        assert total_classified == len(output.results)
        assert total_classified > 0
        print(f"\n  Integration test: {total_classified} records classified")
        print(f"  Injected anomalies: {sum(injected_counts.values())}")
        print(f"  Match rate: {output.summary['match_rate']:.1f}%")
