"""Stage 5: deterministic financial validation.

All monetary math uses ``Decimal`` with ROUND_HALF_UP quantization. This
module is pure: no I/O, no model calls - unit-testable with plain values.

Money-related outputs (potential excess, price impact) are suppressed when
the comparison basis is not compatible (currency mismatch or incompatible
quantity units), exactly as the specification requires: never report a
monetary difference computed on incompatible units or currencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from .normalizer import format_money, units_compatible
from .schemas import Discrepancy, DiscrepancyType, InvoiceItemOut, POItemOut, Severity

_PICTURE_QTY_TOLERANCE = Decimal("0.0001")  # numeric-noise floor for quantity equality


def q2(value: Decimal) -> Decimal:
    """Quantize a monetary value to 2 decimal places (ROUND_HALF_UP)."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Tolerances:
    price_tolerance_pct: Decimal = Decimal("0.005")   # 0.5% as a fraction
    absolute_price_tolerance: Decimal = Decimal("0.01")
    quantity_tolerance: Decimal = Decimal("0.0001")
    shortfall_as_discrepancy: bool = False

    def price_ok(self, difference: Decimal, base_price: Decimal) -> bool:
        allowance = max(base_price.abs() * self.price_tolerance_pct, self.absolute_price_tolerance)
        return difference.abs() <= allowance

    def line_total_ok(self, difference: Decimal, base_total: Decimal) -> bool:
        allowance = max(base_total.abs() * self.price_tolerance_pct, Decimal("0.05"))
        return difference.abs() <= allowance


@dataclass
class PairValidation:
    """Numeric result of validating one invoice/PO line pair."""

    quantity_ok: bool | None = None      # None = could not check (missing data)
    price_ok: bool | None = None
    line_total_ok: bool | None = None
    quantity_difference: Decimal | None = None
    potential_excess_amount: Decimal | None = None
    price_difference: Decimal | None = None
    price_impact_amount: Decimal | None = None
    line_total_difference: Decimal | None = None
    monetary_checks_suppressed: bool = False
    discrepancies: list[Discrepancy] = field(default_factory=list)


def _money_display(value: Decimal | None, currency: str | None) -> str | None:
    return format_money(value, currency) if value is not None else None


def compare_pair(
    invoice_item: InvoiceItemOut,
    po_item: POItemOut,
    *,
    previously_invoiced_qty: Decimal = Decimal("0"),
    currency_compatible: bool = True,
    currency: str | None = None,
    tolerances: Tolerances | None = None,
) -> PairValidation:
    """Validate quantities and prices for one matched invoice/PO line pair.

    ``previously_invoiced_qty`` is the PO quantity already covered by other
    invoice lines, enabling split invoices; each line is validated against
    the *remaining* promised quantity.
    """
    tolerances = tolerances or Tolerances()
    line_ref = invoice_item.description
    result = PairValidation()
    line_ref = invoice_item.description
    inv_qty = invoice_item.quantity
    inv_price = invoice_item.unit_price
    inv_line_total = invoice_item.line_total
    po_qty = po_item.quantity
    po_price = po_item.unit_price

    units_ok = units_compatible(invoice_item.quantity_unit, po_item.quantity_unit)
    if units_ok is False:
        result.monetary_checks_suppressed = True
        result.discrepancies.append(
            Discrepancy(
                type=DiscrepancyType.INCOMPATIBLE_UNITS,
                severity=Severity.WARNING,
                line_ref=line_ref,
                message="Invoice line quantity unit does not match the PO quantity unit.",
                explanation=(
                    "The invoice uses a different quantity unit than the PO "
                    f"({invoice_item.quantity_unit!r} vs {po_item.quantity_unit!r}). "
                    "Quantities cannot be compared safely, so no monetary impact is computed. "
                    "Verify the conversion manually."
                ),
                invoice_value=str(invoice_item.quantity_unit or "not stated"),
                po_value=str(po_item.quantity_unit or "not stated"),
            )
        )

    # ------------------------------------------------------------------ qty
    if inv_qty is not None and units_ok is not False:
        remaining = max(Decimal("0"), po_qty - previously_invoiced_qty)
        qty_diff = inv_qty - remaining
        result.quantity_difference = qty_diff
        result.quantity_ok = qty_diff.abs() <= tolerances.quantity_tolerance
        if not result.quantity_ok:
            if qty_diff > 0:
                excess: Decimal | None = qty_diff * po_price
                if not currency_compatible:
                    excess = None
                    result.monetary_checks_suppressed = True
                else:
                    excess = q2(excess)
                result.potential_excess_amount = excess
                result.discrepancies.append(
                    Discrepancy(
                        type=DiscrepancyType.QUANTITY_MISMATCH,
                        severity=Severity.WARNING,
                        line_ref=line_ref,
                        message=(
                            f"Invoice bills {inv_qty} units against {remaining} "
                            f"remaining on the PO line ({po_qty} ordered)."
                        ),
                        explanation=(
                            "The invoice quantity exceeds the promised quantity on the matched PO "
                            f"line by {qty_diff} units. Applying the PO unit price "
                            f"({format_money(po_price, currency)}), the potential excess is "
                            f"{_money_display(excess, currency) or 'not computed (currency conflict)'} "
                            "before applicable taxes. Alternative unit prices on the invoice are not "
                            "used because the PO price is the agreed comparison basis."
                        ),
                        invoice_value=str(inv_qty),
                        po_value=str(po_qty),
                        difference=str(qty_diff),
                        potential_excess_amount=excess,
                    )
                )
            else:
                severity = Severity.WARNING if tolerances.shortfall_as_discrepancy else Severity.INFO
                result.discrepancies.append(
                    Discrepancy(
                        type=DiscrepancyType.QUANTITY_MISMATCH,
                        severity=severity,
                        line_ref=line_ref,
                        message=(
                            f"Invoice bills {inv_qty} units against {remaining} "
                            f"remaining on the PO line ({po_qty} ordered)."
                        ),
                        explanation=(
                            f"The invoice quantity is {abs(qty_diff)} units below the PO quantity. "
                            "This pattern usually indicates a partial shipment or consolidated "
                            "invoicing and does not create overpayment risk by itself."
                        ),
                        invoice_value=str(inv_qty),
                        po_value=str(po_qty),
                        difference=str(qty_diff),
                    )
                )
    elif inv_qty is None:
        result.discrepancies.append(
            Discrepancy(
                type=DiscrepancyType.MISSING_FINANCIAL_DATA,
                severity=Severity.WARNING,
                line_ref=line_ref,
                message="Invoice quantity is missing for a matched line.",
                explanation=(
                    "The line matched a PO item, but its quantity could not be extracted. "
                    "Falling back to the PO quantity would invent data, so quantity validation "
                    "is skipped and the line needs review."
                ),
                po_value=str(po_qty),
            )
        )

    # ----------------------------------------------------------------- price
    if inv_price is not None:
        price_diff = inv_price - po_price
        result.price_difference = price_diff
        result.price_ok = tolerances.price_ok(price_diff, po_price)
        if not result.price_ok:
            impact: Decimal | None = None
            if currency_compatible and inv_qty is not None and units_ok is not False:
                impact = q2(price_diff * inv_qty)
                result.price_impact_amount = impact
            result.discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.UNIT_PRICE_MISMATCH,
                    severity=Severity.CRITICAL,
                    line_ref=line_ref,
                    message=(
                        f"Invoice unit price {format_money(inv_price, currency)} differs from the "
                        f"PO unit price {format_money(po_price, currency)}."
                    ),
                    explanation=(
                        f"The invoiced unit price deviates from the agreed PO price by "
                        f"{format_money(price_diff, currency)} per unit"
                        + (
                            f", roughly {format_money(impact, currency)} across the invoiced "
                            f"quantity of {inv_qty}."
                            if impact is not None
                            else ". Monetary impact is not computed because units or currency are incompatible."
                        )
                        + " Confirm whether an approved price change or discount applies."
                    ),
                    invoice_value=format_money(inv_price, currency),
                    po_value=format_money(po_price, currency),
                    difference=format_money(price_diff, currency),
                )
            )
    else:
        result.discrepancies.append(
            Discrepancy(
                type=DiscrepancyType.MISSING_FINANCIAL_DATA,
                severity=Severity.WARNING,
                line_ref=line_ref,
                message="Invoice unit price is missing for a matched line.",
                explanation=(
                    "The line matched a PO item, but its unit price could not be extracted. "
                    "Price validation is skipped rather than guessed and the line needs review."
                ),
                po_value=format_money(po_price, currency),
            )
        )

    # ------------------------------------------------------------ line total
    if inv_line_total is not None and inv_qty is not None and inv_price is not None:
        computed = inv_qty * inv_price
        diff = inv_line_total - computed
        result.line_total_difference = diff
        result.line_total_ok = tolerances.line_total_ok(diff, inv_line_total)
        if not result.line_total_ok:
            result.discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.LINE_TOTAL_MISMATCH,
                    severity=Severity.CRITICAL,
                    line_ref=line_ref,
                    message=(
                        f"Stated line total {format_money(inv_line_total, currency)} does not equal "
                        f"quantity x unit price ({format_money(computed, currency)})."
                    ),
                    explanation=(
                        "The invoice line is internally inconsistent: the stated line total "
                        "differs from quantity multiplied by unit price. This must be resolved "
                        "before any financial comparison on this line is trusted."
                    ),
                    invoice_value=format_money(inv_line_total, currency),
                    difference=format_money(diff, currency),
                )
            )

    return result


def validate_po_line_internal_consistency(po_item: POItemOut, currency: str | None) -> Discrepancy | None:
    """Optional sanity check of user-supplied PO data (informational only)."""
    if po_item.line_total is None:
        return None
    computed = po_item.quantity * po_item.unit_price
    diff = po_item.line_total - computed
    if q2(diff) == Decimal("0") or abs(diff) <= max(
        po_item.line_total.abs() * Decimal("0.005"), Decimal("0.05")
    ):
        return None
    return Discrepancy(
        type=DiscrepancyType.LINE_TOTAL_MISMATCH,
        severity=Severity.INFO,
        line_ref=po_item.description,
        message="PO line total does not equal its quantity x unit price.",
        explanation=(
            "This concerns the Purchase Order data you provided, not the supplier invoice. "
            "Matching continues, but check the PO entry itself."
        ),
        po_value=format_money(po_item.line_total, currency),
        difference=format_money(diff, currency),
    )


def validate_invoice_totals(
    invoice_items: list[InvoiceItemOut],
    subtotal: Decimal | None,
    total: Decimal | None,
    currency: str | None,
    tolerances: Tolerances | None = None,
) -> list[Discrepancy]:
    """Cross-check stated subtotal/total against the extracted line totals.

    Only checks whose inputs exist are run; nothing is assumed about taxes.
    """
    tolerances = tolerances or Tolerances()
    findings: list[Discrepancy] = []
    line_sum = sum(
        (item.line_total for item in invoice_items if item.line_total is not None),
        Decimal("0"),
    )

    if subtotal is not None and line_sum > 0:
        diff = subtotal - line_sum
        allowance = max(subtotal.abs() * Decimal("0.001"), Decimal("0.05"))
        if diff.abs() > allowance:
            findings.append(
                Discrepancy(
                    type=DiscrepancyType.INVOICE_TOTAL_MISMATCH,
                    severity=Severity.CRITICAL,
                    message="Stated subtotal does not equal the sum of line totals.",
                    explanation=(
                        f"The extracted line totals add up to {format_money(line_sum, currency)} "
                        f"while the invoice states a subtotal of {format_money(subtotal, currency)}. "
                        "The invoice document appears inconsistent; verify it before payment."
                    ),
                    invoice_value=format_money(subtotal, currency),
                    difference=format_money(diff, currency),
                )
            )

    if total is not None and subtotal is not None and total < subtotal:
        findings.append(
            Discrepancy(
                type=DiscrepancyType.INVOICE_TOTAL_MISMATCH,
                severity=Severity.CRITICAL,
                message="Invoice total is lower than its subtotal.",
                explanation=(
                    "The stated grand total is below the subtotal, which is impossible without "
                    "an unrecognized discount or negative adjustment. The document needs review."
                ),
                invoice_value=f"subtotal {format_money(subtotal, currency)} / total {format_money(total, currency)}",
            )
        )

    return findings
