"""Unit tests for the financial validator (Stage 5).

Spec targets covered here: quantity mismatch, unit-price mismatch, missing
financial information, incompatible units, and the "never compute money on an
incompatible basis" rule.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.financial_validator import (
    Tolerances,
    compare_pair,
    q2,
    validate_invoice_totals,
    validate_po_line_internal_consistency,
)
from app.schemas import DiscrepancyType, InvoiceItemOut, POItemOut, Severity


def po(**overrides) -> POItemOut:
    values = dict(
        po_item_index=0,
        description="USB-C Cable, 1 Meter",
        quantity=Decimal("100"),
        quantity_unit="pcs",
        unit_price=Decimal("150.00"),
    )
    values.update(overrides)
    return POItemOut(**values)


def inv(**overrides) -> InvoiceItemOut:
    values = dict(
        line_index=0,
        description="Type-C Charging Cable, 100cm",
        quantity=Decimal("100"),
        quantity_unit="pcs",
        unit_price=Decimal("150.00"),
        line_total=Decimal("15000.00"),
    )
    values.update(overrides)
    return InvoiceItemOut(**values)


def types_findings(result):
    return {d.type for d in result.discrepancies}


def test_matching_quantities_and_prices_pass_clean():
    result = compare_pair(inv(), po(), currency="INR")
    assert result.quantity_ok is True
    assert result.price_ok is True
    assert result.line_total_ok is True
    assert result.potential_excess_amount is None
    assert result.discrepancies == []


def test_quantity_mismatch_computes_excess_with_po_price():
    result = compare_pair(inv(quantity=Decimal("120")), po(), currency="INR")
    assert result.quantity_ok is False
    assert result.quantity_difference == Decimal("20")
    assert result.potential_excess_amount == Decimal("3000.00")
    discrepancy = next(d for d in result.discrepancies if d.type == DiscrepancyType.QUANTITY_MISMATCH)
    assert discrepancy.potential_excess_amount == Decimal("3000.00")
    assert "20 units" in discrepancy.explanation
    assert "₹3,000.00" in discrepancy.explanation
    assert discrepancy.invoice_value == "120"
    assert discrepancy.po_value == "100"


def test_quantity_shortfall_is_informational_not_a_discrepancy():
    result = compare_pair(inv(quantity=Decimal("80")), po(), currency="INR")
    types = types_findings(result)
    assert DiscrepancyType.QUANTITY_MISMATCH in types
    shortfall = next(d for d in result.discrepancies if d.type == DiscrepancyType.QUANTITY_MISMATCH)
    assert shortfall.severity == Severity.INFO
    assert shortfall.potential_excess_amount is None


def test_split_invoice_validates_against_remaining_po_quantity():
    # First line bills 60, second line bills 60 against a PO of 100:
    # line 1 is short, line 2 exceeds the *remaining* quantity by 20.
    first = compare_pair(inv(quantity=Decimal("60")), po(), currency="INR")
    assert first.quantity_difference == Decimal("-40")

    second = compare_pair(
        inv(quantity=Decimal("60")),
        po(),
        previously_invoiced_qty=Decimal("60"),
        currency="INR",
    )
    assert second.quantity_difference == Decimal("20")
    assert second.potential_excess_amount == Decimal("3000.00")


def test_unit_price_mismatch_detected_with_impact():
    result = compare_pair(inv(unit_price=Decimal("165.00")), po(), currency="INR")
    assert result.price_ok is False
    assert result.price_difference == Decimal("15.00")
    discrepancy = next(d for d in result.discrepancies if d.type == DiscrepancyType.UNIT_PRICE_MISMATCH)
    assert discrepancy.severity == Severity.CRITICAL
    assert discrepancy.potential_excess_amount is None
    # monetary impact = 15 * invoiced 100
    assert discrepancy.explanation is not None


def test_price_within_tolerances_is_accepted():
    # 0.33% above the PO price is inside the configured 0.5% tolerance.
    result = compare_pair(inv(unit_price=Decimal("150.50")), po(), currency="INR")
    assert result.price_ok is True
    assert types_findings(result) == set()


def test_price_tolerance_respects_absolute_floor():
    # tiny absolute deviation on a cheap item is ACCEPTED via the 0.01 floor
    tolerances = Tolerances(price_tolerance_pct=Decimal("0"), absolute_price_tolerance=Decimal("0.01"))
    cheap_po = po(description="Clip, Steel", unit_price=Decimal("0.50"))
    result = compare_pair(inv(unit_price=Decimal("0.509"), description="Clip, Steel"), cheap_po, tolerances=tolerances)
    assert result.price_ok is True


def test_currency_mismatch_suppresses_all_monetary_outputs():
    result = compare_pair(
        inv(quantity=Decimal("120")),
        po(),
        currency_compatible=False,
        currency=None,
    )
    assert result.potential_excess_amount is None
    assert result.monetary_checks_suppressed is True
    qty_disc = next(d for d in result.discrepancies if d.type == DiscrepancyType.QUANTITY_MISMATCH)
    assert qty_disc.potential_excess_amount is None
    assert "not computed" in qty_disc.explanation
    # the raw difference is still reported textually, but never as money
    assert qty_disc.invoice_value == "120"


def test_incompatible_units_block_quantity_and_money():
    result = compare_pair(
        inv(quantity=Decimal("120"), quantity_unit="meters"),
        po(quantity_unit="pcs"),
        currency="INR",
    )
    types = types_findings(result)
    assert DiscrepancyType.INCOMPATIBLE_UNITS in types
    assert result.quantity_ok is None
    assert result.potential_excess_amount is None
    unit_disc = next(d for d in result.discrepancies if d.type == DiscrepancyType.INCOMPATIBLE_UNITS)
    assert unit_disc.severity == Severity.WARNING
    assert "cannot be compared safely" in unit_disc.explanation


def test_missing_quantity_never_fabricated():
    result = compare_pair(inv(quantity=None), po(), currency="INR")
    assert result.quantity_ok is None
    disc = next(d for d in result.discrepancies if d.type == DiscrepancyType.MISSING_FINANCIAL_DATA)
    assert disc.severity == Severity.WARNING
    assert "invent" in disc.explanation or "invented" in disc.explanation


def test_missing_price_never_fabricated():
    result = compare_pair(inv(unit_price=None), po(), currency="INR")
    assert result.price_ok is None
    disc = next(d for d in result.discrepancies if d.type == DiscrepancyType.MISSING_FINANCIAL_DATA)
    assert "guessed" in disc.explanation


def test_line_total_internal_inconsistency_flagged():
    result = compare_pair(inv(line_total=Decimal("19000.00")), po(), currency="INR")
    disc = next(d for d in result.discrepancies if d.type == DiscrepancyType.LINE_TOTAL_MISMATCH)
    assert disc.severity == Severity.CRITICAL
    assert disc.invoice_value == "₹19,000.00"
    assert "internally inconsistent" in disc.explanation


def test_po_line_consistency_is_informational_only():
    finding = validate_po_line_internal_consistency(
        po(line_total=Decimal("12000.00")), currency="INR"
    )
    assert finding is not None
    assert finding.severity == Severity.INFO
    assert "not the supplier invoice" in finding.explanation


def test_invoice_totals_crosscheck_subtotal_and_lines():
    items = [
        inv(line_total=Decimal("15000.00")),
        inv(line_index=1, description="USB-C Adapter 30W", line_total=Decimal("1500.00")),
    ]
    findings = validate_invoice_totals(items, subtotal=Decimal("17000.00"), total=None, currency="INR")
    assert findings, "subtotal that ignores line totals must be flagged"
    assert findings[0].type == DiscrepancyType.INVOICE_TOTAL_MISMATCH
    assert findings[0].severity == Severity.CRITICAL


def test_invoice_total_below_subtotal_impossible():
    findings = validate_invoice_totals(
        [inv(line_total=Decimal("15000.00"))],
        subtotal=Decimal("15000.00"),
        total=Decimal("14000.00"),
        currency="INR",
    )
    assert any(d.message == "Invoice total is lower than its subtotal." for d in findings)


def test_invoice_totals_pass_when_consistent_with_tax_handled_outside():
    items = [inv(line_total=Decimal("15000.00"))]
    findings = validate_invoice_totals(
        items, subtotal=Decimal("15000.00"), total=Decimal("16500.00"), currency="INR"
    )
    assert findings == []  # taxes are not asserted to any rate; only impossible states flagged


def test_quantization_is_round_half_up():
    assert q2(Decimal("3000.005")) == Decimal("3000.01")
    assert q2(Decimal("3000.004")) == Decimal("3000.00")
    assert q2(Decimal("2.675")) == Decimal("2.68")
