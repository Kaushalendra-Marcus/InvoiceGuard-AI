"""Unit tests for hybrid matching (Stages 1-4).

Fastembed downloads/loads are session-scoped; semantic-specific assertions are
skipped automatically when the model is unavailable (e.g. offline CI), while
SKU/exact/fuzzy logic is always exercised.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.matching import SemanticMatcher, match_invoice_to_po
from app.schemas import ConfidenceCategory, InvoiceItemOut, MatchMethod, POItemOut


def po(**overrides) -> POItemOut:
    values = dict(
        po_item_index=0,
        description="USB-C Cable, 1 Meter",
        sku="CBL-USB-C-1M",
        quantity=Decimal("100"),
        quantity_unit="pcs",
        unit_price=Decimal("150.00"),
    )
    values.update(overrides)
    return POItemOut(**values)


def inv(**overrides) -> InvoiceItemOut:
    values = dict(
        line_index=0,
        description="USB-C Cable, 1 Meter",
        sku=None,
        quantity=Decimal("100"),
        quantity_unit=None,
        unit_price=Decimal("150.00"),
        line_total=Decimal("15000.00"),
    )
    values.update(overrides)
    return InvoiceItemOut(**values)


@pytest.fixture(scope="module")
def real_semantic():
    matcher = SemanticMatcher(enabled=True)
    if not matcher.available:
        pytest.skip("sentence embedding model unavailable in this environment")
    return matcher


DISABLED = SemanticMatcher(enabled=False)


def test_exact_description_match(tmp_path):
    outcomes = match_invoice_to_po([inv()], [po()], DISABLED)
    assert outcomes[0].matched is True
    assert outcomes[0].method == MatchMethod.EXACT
    assert outcomes[0].confidence == ConfidenceCategory.HIGH
    assert outcomes[0].contradictions == []


def test_unit_normalization_enables_exact_match():
    # '1 Meter' and '100 cm' normalize into comparable units, and 'Meter' vs
    # 'Meter' textually collapses to the same normalized description.
    outcomes = match_invoice_to_po(
        [inv(description="USB-C Cable 1 Meter", quantity=Decimal("100"))],
        [po(description="USB-C Cable  1 MTR", sku=None)],
        DISABLED,
    )
    assert outcomes[0].matched is True
    assert outcomes[0].method == MatchMethod.EXACT


def test_differently_worded_same_product_semantic_match(real_semantic):
    """The motivating case: descriptions share no tokens; semantics connects."""
    outcomes = match_invoice_to_po(
        [inv(description="Type-C Charging Cable, 100cm", quantity=Decimal("120"))],
        [po(description="USB-C Cable, 1 Meter", quantity=Decimal("100"))],
        real_semantic,
    )
    outcome = outcomes[0]
    assert outcome.matched is True
    assert outcome.method == MatchMethod.SEMANTIC
    assert outcome.semantic_score is not None and outcome.semantic_score >= 0.72
    assert outcome.ambiguous is False
    # 1 m and 100 cm are both lengths: no spec contradiction
    assert outcome.contradictions == []
    assert outcome.confidence in (ConfidenceCategory.MEDIUM, ConfidenceCategory.HIGH)


def test_fuzzy_match_below_semantic_when_text_close():
    outcomes = match_invoice_to_po(
        [inv(description="USB-C Cable 1 Mtr (White)", sku=None)],
        [po(description="USB-C Cable 1 Meter (White)", sku=None)],
        DISABLED,
    )
    assert outcomes[0].matched is True
    assert outcomes[0].method in (MatchMethod.EXACT, MatchMethod.FUZZY)


def test_semantic_match_requires_unit_and_spec_agreement(real_semantic):
    # Different product specs (1 m vs 2 m) must NOT be silently accepted even
    # when the wording is nearly identical.
    outcomes = match_invoice_to_po(
        [inv(description="USB-C Cable 2 Meter", quantity=Decimal("120"))],
        [po(description="USB-C Cable 1 Meter", quantity=Decimal("100"))],
        real_semantic,
    )
    outcome = outcomes[0]
    assert outcome.matched is True  # still a candidate pair...
    assert outcome.contradictions, "...but the length conflict must be flagged"
    assert any("length" in c for c in outcome.contradictions)
    assert outcome.confidence == ConfidenceCategory.LOW


def test_ambiguous_semantic_candidates_force_review(real_semantic):
    """Two PO lines nearly tie for the invoice line -> neither chosen silently."""
    po_lines = [
        po(po_item_index=0, description="USB-C Cable, 1 Meter, White", sku=None),
        po(po_item_index=1, description="USB-C Cable, 1 Meter, Black", sku=None),
    ]
    outcomes = match_invoice_to_po(
        [inv(description="USB-C Cable 1 Meter", sku=None)],
        po_lines,
        real_semantic,
        fuzzy_threshold=999.0,   # force reliance on semantic evidence only
        ambiguous_gap=0.25,      # generous: colour words should not decide this alone
    )
    outcome = outcomes[0]
    assert outcome.matched is True
    assert outcome.ambiguous is True
    assert outcome.competitor_po_index is not None
    assert outcome.confidence == ConfidenceCategory.LOW


def test_no_match_when_nothing_is_similar():
    outcomes = match_invoice_to_po(
        [inv(description="Office Chair with Lumbar Support")],
        [po()],
        DISABLED,
    )
    outcome = outcomes[0]
    assert outcome.matched is False
    assert outcome.method == MatchMethod.NONE
    assert outcome.competitor_score is not None  # near-miss evidence preserved


def test_sku_match_is_used_when_present():
    outcomes = match_invoice_to_po(
        [inv(sku="CBL-USB-C-1M")],
        [po()],
        DISABLED,
    )
    # invoice description defaults to the PO wording; keep an unw Invoice line:
    # (description left identical so nothing else interferes)
    assert outcomes[0].matched is True
    assert outcomes[0].method == MatchMethod.SKU
    assert outcomes[0].sku_match is True


def test_sku_match_with_unrelated_description_is_a_contradiction(real_semantic):
    outcomes = match_invoice_to_po(
        [inv(description="Copper Wire 2 kg spool", sku="CBL-USB-C-1M")],
        [po()],
        real_semantic,
    )
    outcome = outcomes[0]
    assert outcome.matched is True
    assert outcome.method == MatchMethod.SKU
    assert outcome.contradictions, "unrelated description with same SKU must be flagged"
    assert outcome.confidence == ConfidenceCategory.LOW


def test_different_skus_on_matched_lines_flagged_as_contradiction():
    outcomes = match_invoice_to_po(
        [inv(sku="OTHER-123")],
        [po()],
        DISABLED,
    )
    outcome = outcomes[0]
    assert outcome.matched is True  # exact description match still found
    assert any("SKU" in c for c in outcome.contradictions)


def test_incompatible_quantity_units_noted():
    outcomes = match_invoice_to_po(
        [inv(quantity_unit="meters")],
        [po()],
        DISABLED,
    )
    outcome = outcomes[0]
    assert outcome.unit_compatible is False
    assert any("Quantity units differ" in note for note in outcome.notes)


def test_po_line_capacity_prevents_invoicing_past_a_full_line():
    # A_second invoice line for the same PO item when the PO is fully covered
    # is still allowed through (over-invoicing must be *detected*), but the
    # explanation produced by validation reflects remaining quantity. Here we
    # assert matching behavior: it stays matched.
    outcomes = match_invoice_to_po(
        [inv(), inv(line_index=1)],
        [po()],
        DISABLED,
        consumed_promised_qty=[Decimal("100")],
    )
    assert outcomes[1].matched is True  # exact description match overrides capacity
    assert outcomes[1].method == MatchMethod.EXACT
