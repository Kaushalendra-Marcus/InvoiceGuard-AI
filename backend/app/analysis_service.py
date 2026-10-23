"""Stage 6: analysis orchestration and the final decision.

Combines document extraction, matching (Stages 1-4) and deterministic
financial validation (Stage 5) into one structured report with an
explainable status:

- MATCHED            - all matched items and financial checks passed
- DISCREPANCY_FOUND  - confidently detected inconsistencies
- NEEDS_REVIEW       - insufficient or conflicting evidence

A high semantic similarity never overrides a quantity/price discrepancy, and
unreliable matches are routed to review rather than forced. The report never
authorizes payment - that message is part of every result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from .config import settings
from .document_parser import InvoiceParseResult, parse_invoice_file
from .financial_validator import (
    PairValidation,
    Tolerances,
    compare_pair,
    validate_invoice_totals,
    validate_po_line_internal_consistency,
)
from .matching import SemanticMatcher, match_invoice_to_po
from .schemas import (
    AnalysisReport,
    Discrepancy,
    DiscrepancyType,
    InvoiceItemOut,
    LineMatch,
    MatchMethod,
    OverallStatus,
    POItemOut,
    PurchaseOrderDocument,
    Severity,
)

logger = logging.getLogger(__name__)

_REVIEW_TRIGGER_TYPES = (
    DiscrepancyType.MISSING_FINANCIAL_DATA,
    DiscrepancyType.INCOMPATIBLE_UNITS,
)
# Deterministic financial inconsistencies (identifier-confirmed matches only)
_DISCREPANCY_TRIGGER_TYPES = (
    DiscrepancyType.QUANTITY_MISMATCH,
    DiscrepancyType.UNIT_PRICE_MISMATCH,
    DiscrepancyType.LINE_TOTAL_MISMATCH,
    DiscrepancyType.INVOICE_TOTAL_MISMATCH,
)
# Match methods whose evidence alone does NOT prove product identity.
_UNCONFIRMED_METHODS = (MatchMethod.FUZZY, MatchMethod.SEMANTIC)
# Margin below the thresholds within which an unmatched line still counts as
# "possible match we could not confirm" (insufficient evidence) rather than a
# confident "this item is not on the PO".
_NEAR_MISS_MARGIN = 0.10

_CLOSING_LINES = {
    OverallStatus.MATCHED: "All extracted items and financial checks passed.",
    OverallStatus.DISCREPANCY_FOUND: (
        "One or more inconsistencies were detected between the invoice and the PO."
    ),
    OverallStatus.NEEDS_REVIEW: (
        "Confidence is insufficient in at least one case; human review is required."
    ),
}


def _to_invoice_items(parsed: InvoiceParseResult) -> list[InvoiceItemOut]:
    return [
        InvoiceItemOut(
            line_index=index,
            description=raw.description,
            sku=raw.sku,
            quantity=raw.quantity,
            quantity_unit=raw.quantity_unit,
            unit_price=raw.unit_price,
            line_total=raw.line_total,
            raw_line=raw.raw_text or None,
        )
        for index, raw in enumerate(parsed.items)
    ]


def _to_po_items(po_doc: PurchaseOrderDocument) -> list[POItemOut]:
    return [
        POItemOut(
            po_item_index=index,
            description=item.description,
            sku=item.sku,
            quantity=item.quantity,
            quantity_unit=item.quantity_unit,
            unit_price=item.unit_price,
            line_total=item.line_total,
        )
        for index, item in enumerate(po_doc.items)
    ]


@dataclass
class _DecisionState:
    discrepancies: list[Discrepancy] = field(default_factory=list)
    review_triggers: list[str] = field(default_factory=list)
    discrepancy_triggers: list[str] = field(default_factory=list)


def _resolve_currency_context(
    parsed: InvoiceParseResult,
    po_doc: PurchaseOrderDocument,
    warnings: list[str],
) -> tuple[str | None, str | None, str, bool]:
    """Returns (invoice_currency, po_currency, effective_currency, compatible)."""
    invoice_currency = parsed.currency
    po_currency = po_doc.currency
    effective = invoice_currency or po_currency or settings.default_currency

    if invoice_currency is None and po_currency is None:
        warnings.append(
            f"Currency was not found on the invoice or the PO; '{effective}' "
            "(configured default) is assumed for display."
        )
    elif invoice_currency is None and po_currency is not None:
        warnings.append(
            f"Currency not detected on the invoice; PO currency '{po_currency}' is assumed."
        )
    elif po_currency is None and invoice_currency is not None:
        warnings.append(
            "Currency not specified on the Purchase Order; invoice currency "
            f"'{invoice_currency}' is assumed for this comparison."
        )

    compatible = bool(invoice_currency and po_currency and invoice_currency != po_currency) is False
    return invoice_currency, po_currency, effective, compatible


def _crop(text: str | None, size: int = 60) -> str:
    if not text:
        return "(unknown PO line)"
    text = text.strip()
    return text if len(text) <= size else text[: size - 1] + "…"


def _final_status(state: _DecisionState) -> OverallStatus:
    if state.review_triggers:
        return OverallStatus.NEEDS_REVIEW
    if state.discrepancy_triggers:
        return OverallStatus.DISCREPANCY_FOUND
    return OverallStatus.MATCHED


def _recommended_action(status: OverallStatus) -> str:
    if status == OverallStatus.MATCHED:
        return (
            "The relevant items and financial checks passed. Archive this analysis for the "
            "payment workflow. This result is informational support and does NOT authorize payment."
        )
    if status == OverallStatus.DISCREPANCY_FOUND:
        return (
            "Do not approve payment yet: resolve the flagged discrepancies with the supplier "
            "(quantity/price corrections or a credit note) and re-run the analysis."
        )
    return (
        "Do not decide automatically: have a finance reviewer confirm the flagged cases "
        "(product identity, quantities or prices) with the supplier before payment."
    )


def _dedupe_discrepancies(discrepancies: list[Discrepancy]) -> list[Discrepancy]:
    seen: set[tuple] = set()
    unique: list[Discrepancy] = []
    for d in discrepancies:
        key = (d.type, d.severity, d.line_ref, d.message)
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def _semantic_available(semantic: SemanticMatcher | None) -> bool:
    if semantic is None:
        from .matching import get_semantic_matcher

        semantic = get_semantic_matcher()
    return semantic.available


def _build_summary(
    state: _DecisionState,
    status: OverallStatus,
    matches: list[LineMatch],
    po_items_without_invoice: list[POItemOut],
) -> str:
    matched = sum(1 for m in matches if m.matched)
    unmatched = len(matches) - matched
    parts = [f"{matched} of {len(matches)} invoice lines matched to a PO line."]
    if unmatched:
        parts.append(f"{unmatched} invoice line(s) have no matching PO line.")
    if po_items_without_invoice:
        parts.append(
            f"{len(po_items_without_invoice)} PO line(s) were not billed at all "
            "(often fine for partial shipments)."
        )
    if state.discrepancy_triggers:
        parts.append(f"{len(state.discrepancy_triggers)} financial discrepancy(ies) detected.")
    if state.review_triggers:
        parts.append(f"{len(state.review_triggers)} case(s) were routed to manual review.")
    parts.append(_CLOSING_LINES[status])
    return " ".join(parts)


def run_analysis(
    invoice_filename: str,
    invoice_bytes: bytes,
    purchase_order: PurchaseOrderDocument,
    semantic: SemanticMatcher | None = None,
    tolerances: Tolerances | None = None,
) -> AnalysisReport:
    """Run the full validation pipeline for one invoice/PO pair."""
    tolerances = tolerances or Tolerances(
        price_tolerance_pct=settings.price_tolerance_pct,
        absolute_price_tolerance=settings.absolute_price_tolerance,
        quantity_tolerance=settings.quantity_tolerance,
        shortfall_as_discrepancy=settings.shortfall_as_discrepancy,
    )

    # ------------------------------------------------------------- 1. extract
    parsed = parse_invoice_file(invoice_filename, invoice_bytes)
    invoice_items = _to_invoice_items(parsed)
    po_items = _to_po_items(purchase_order)
    state = _DecisionState()

    # ------------------------------------------------------------ 2. currency
    invoice_currency, po_currency, effective_currency, currencies_compatible = (
        _resolve_currency_context(parsed, purchase_order, parsed.warnings)
    )
    if not currencies_compatible:
        state.review_triggers.append("invoice and PO currencies differ")
        state.discrepancies.append(
            Discrepancy(
                type=DiscrepancyType.CURRENCY_MISMATCH,
                severity=Severity.CRITICAL,
                message="Invoice currency does not match the Purchase Order currency.",
                explanation=(
                    f"The invoice is in {invoice_currency} while the PO is in {po_currency}. "
                    "Prices cannot be compared across currencies, so monetary differences are "
                    "suppressed. Reissue one of the documents or convert the PO price first."
                ),
                invoice_value=invoice_currency,
                po_value=po_currency,
            )
        )

    # extraction notes travel both as *findings* and as plain warnings
    for warning in parsed.warnings:
        state.discrepancies.append(
            Discrepancy(
                type=DiscrepancyType.EXTRACTION_INFO,
                severity=Severity.INFO,
                message="Extraction note",
                explanation=warning,
            )
        )

    # ------------------------------------------------------------ 3. matching
    outcomes = match_invoice_to_po(invoice_items, po_items, semantic)
    matches: list[LineMatch] = []
    unmatched_invoice_items: list[InvoiceItemOut] = []
    po_line_invoiced: dict[int, Decimal] = {}

    for i, outcome in enumerate(outcomes):
        inv_line_ref = f"Line {i + 1}: {outcome.invoice_item.description[:60]}"
        pair: PairValidation | None = None
        pair_notes = list(outcome.notes)

        if outcome.matched and outcome.po_item is not None:
            unconfirmed = outcome.method in _UNCONFIRMED_METHODS
            pair = compare_pair(
                invoice_item=outcome.invoice_item,
                po_item=outcome.po_item,
                previously_invoiced_qty=po_line_invoiced.get(outcome.po_index or -1, Decimal("0")),
                currency_compatible=currencies_compatible,
                currency=effective_currency,
                tolerances=tolerances,
            )
            if (
                outcome.invoice_item.quantity is not None
                and outcome.po_index is not None
            ):
                po_line_invoiced[outcome.po_index] = (
                    po_line_invoiced.get(outcome.po_index, Decimal("0"))
                    + outcome.invoice_item.quantity
                )
            for discrepancy in pair.discrepancies:
                discrepancy.line_ref = discrepancy.line_ref or inv_line_ref
                state.discrepancies.append(discrepancy)
                if discrepancy.type in _REVIEW_TRIGGER_TYPES:
                    state.review_triggers.append(discrepancy.message)
                elif (
                    discrepancy.type in _DISCREPANCY_TRIGGER_TYPES
                    and discrepancy.severity != Severity.INFO  # shortfall notes are informational
                ):
                    if unconfirmed:
                        # e.g. the worked example: quantity mismatch on a
                        # semantically matched line - identity is only proposed.
                        state.review_triggers.append(
                            f"{inv_line_ref}: {discrepancy.message} The match evidence is "
                            "semantic/fuzzy similarity (identity not proven), so both the "
                            "product identity and the flagged values need confirmation."
                        )
                    else:
                        state.discrepancy_triggers.append(discrepancy.message)

        if not outcome.matched:
            unmatched_invoice_items.append(outcome.invoice_item)
            best = outcome.competitor_score if outcome.competitor_score is not None else 0.0
            threshold = max(settings.fuzzy_threshold / 100.0, settings.semantic_threshold)
            near_miss = best >= threshold - _NEAR_MISS_MARGIN
            closest = f"{best:.2f}" if outcome.competitor_score is not None else "none"
            if near_miss:
                state.review_triggers.append(
                    f"{inv_line_ref}: no PO line met the matching thresholds; the closest "
                    f"candidate scored {closest} (below threshold but close enough to require review)."
                )
                state.discrepancies.append(
                    Discrepancy(
                        type=DiscrepancyType.UNMATCHED_INVOICE_ITEM,
                        severity=Severity.WARNING,
                        line_ref=inv_line_ref,
                        message="Invoice line could not be matched with sufficient confidence.",
                        explanation=(
                            f"A PO line came close (similarity {closest}) but stayed below the "
                            "matching threshold. Confirming whether it is the same product "
                            "requires human review."
                        ),
                        invoice_value=outcome.invoice_item.description,
                    )
                )
            else:
                state.discrepancy_triggers.append(f"{inv_line_ref} is not on the PO.")
                state.discrepancies.append(
                    Discrepancy(
                        type=DiscrepancyType.UNMATCHED_INVOICE_ITEM,
                        severity=Severity.CRITICAL,
                        line_ref=inv_line_ref,
                        message="Invoice line item not found in the Purchase Order.",
                        explanation=(
                            "The invoice bills an item that is not present on the Purchase Order "
                            f"(closest similarity {closest}). This requires verification with the "
                            "purchasing team before payment."
                        ),
                        invoice_value=outcome.invoice_item.description,
                    )
                )
            matches.append(
                LineMatch(
                    invoice_item_index=i,
                    invoice_description=outcome.invoice_item.description,
                    matched=False,
                    method=outcome.method,
                    fuzzy_score=outcome.fuzzy_score,
                    semantic_score=outcome.semantic_score,
                    notes=pair_notes,
                    invoice_item=outcome.invoice_item,
                )
            )
            continue

        # ------------------------------------------------------------- review
        if outcome.ambiguous:
            state.review_triggers.append(f"{inv_line_ref}: ambiguous match.")
            state.discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.AMBIGUOUS_MATCH,
                    severity=Severity.WARNING,
                    line_ref=inv_line_ref,
                    message="More than one PO line fits this invoice line similarly.",
                    explanation=(
                        f"A second PO line ({_crop(outcome.competitor_description)}) scores "
                        f"{outcome.competitor_score if outcome.competitor_score is not None else 'n/a'} "
                        "against the chosen match, so an exact one-to-one match cannot be "
                        "established without human confirmation."
                    ),
                    invoice_value=outcome.invoice_item.description,
                    po_value=_crop(outcome.competitor_description),
                )
            )
        for contradiction in outcome.contradictions:
            state.review_triggers.append(f"{inv_line_ref}: {contradiction}")
            state.discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.AMBIGUOUS_MATCH,
                    severity=Severity.WARNING,
                    line_ref=inv_line_ref,
                    message="Matched items disagree on a product attribute.",
                    explanation=(
                        f"{contradiction} The description pairing is kept as candidate evidence "
                        "only; human review should confirm the products are actually the same."
                    ),
                )
            )

        matches.append(
            LineMatch(
                invoice_item_index=i,
                invoice_description=outcome.invoice_item.description,
                po_item_index=outcome.po_index,
                po_description=outcome.po_item.description if outcome.po_item else None,
                matched=True,
                method=outcome.method,
                fuzzy_score=outcome.fuzzy_score,
                semantic_score=outcome.semantic_score,
                sku_match=outcome.sku_match,
                ambiguous=outcome.ambiguous,
                competitor_po_index=outcome.competitor_po_index,
                competitor_description=outcome.competitor_description,
                competitor_score=outcome.competitor_score,
                confidence=outcome.confidence,
                unit_compatible=(outcome.unit_compatible is not False),
                contradictions=list(outcome.contradictions),
                notes=pair_notes,
                invoice_item=outcome.invoice_item,
                po_item=outcome.po_item,
            )
        )

    # ------------------------------------------------- 4. invoice totals etc.
    for discrepancy in validate_invoice_totals(
        invoice_items, parsed.subtotal, parsed.total, effective_currency, tolerances
    ):
        state.discrepancies.append(discrepancy)
        state.review_triggers.append(discrepancy.message)

    for po_item in po_items:
        po_discrepancy = validate_po_line_internal_consistency(po_item, po_currency)
        if po_discrepancy is not None:
            state.discrepancies.append(po_discrepancy)

    matched_po_indices = {m.po_item_index for m in matches if m.po_item_index is not None}
    po_items_without_invoice = [
        po_item for po_item in po_items if po_item.po_item_index not in matched_po_indices
    ]

    semantic_available = _semantic_available(semantic)
    if not semantic_available:
        state.discrepancies.append(
            Discrepancy(
                type=DiscrepancyType.EXTRACTION_INFO,
                severity=Severity.INFO,
                message="Semantic matching unavailable",
                explanation=(
                    "The sentence embedding model could not be loaded in this environment, so "
                    "description matching relied on SKU, exact and fuzzy text similarity only. "
                    "Strongly reworded product descriptions may appear unmatched in this run."
                ),
            )
        )

    # --------------------------------------------------------- 5. decision
    status = _final_status(state)
    return AnalysisReport(
        status=status,
        summary=_build_summary(state, status, matches, po_items_without_invoice),
        recommended_action=_recommended_action(status),
        vendor_name=parsed.vendor_name,
        invoice_number=parsed.invoice_number,
        invoice_po_number=parsed.po_number,
        invoice_currency=invoice_currency,
        invoice_subtotal=parsed.subtotal,
        invoice_total=parsed.total,
        po_number=purchase_order.po_number,
        po_currency=po_currency,
        effective_currency=effective_currency if currencies_compatible else None,
        invoice_items=invoice_items,
        po_items=po_items,
        matches=matches,
        unmatched_invoice_items=unmatched_invoice_items,
        po_items_without_invoice=po_items_without_invoice,
        discrepancies=_dedupe_discrepancies(state.discrepancies),
        extraction_warnings=list(parsed.warnings),
        semantic_matching_available=semantic_available,
    )


__all__ = ["run_analysis"]
