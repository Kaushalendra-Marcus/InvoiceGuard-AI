"""Pytest fixtures: isolated settings/database and synthetic invoice builders."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings as base_settings  # noqa: E402
from app.database import get_engine, init_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.schemas import POItem, PurchaseOrderDocument  # noqa: E402
from app.usage_limiter import UsageLimiter  # noqa: E402


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Isolate database and settings per test."""
    db_path = tmp_path / "usage.db"
    monkeypatch.setattr(base_settings, "db_path", db_path)
    monkeypatch.setattr(base_settings, "daily_limit", 10)
    # IP-level rate limiting gets effectively unlimited caps by default so
    # API tests that push many analyses through one test client (e.g. the
    # 11-analyses daily-limit test) are not affected; test_rate_limits
    # re-patches real production values for its fixtures.
    monkeypatch.setattr(base_settings, "rate_limit_per_minute", 10**9)
    monkeypatch.setattr(base_settings, "rate_limit_lifetime", 10**9)
    init_db(db_path)
    engine = get_engine(db_path)
    limiter = UsageLimiter(engine)
    app = create_app()
    app.state.engine = engine
    app.state.limiter = limiter
    return app, limiter, engine


@pytest.fixture()
def client(app_env):
    app, limiter, engine = app_env
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.limiter = limiter  # type: ignore[attr-defined]
        yield test_client


# ---------------------------------------------------------------------------
# Synthetic Purchase Orders
# ---------------------------------------------------------------------------
def make_po(
    items: list[dict],
    po_number: str = "PO-2024-1001",
    currency: str | None = "INR",
) -> PurchaseOrderDocument:
    cleaned: list[POItem] = []
    for raw in items:
        cleaned.append(
            POItem(
                description=raw["description"],
                sku=raw.get("sku"),
                quantity=Decimal(str(raw["quantity"])),
                quantity_unit=raw.get("quantity_unit", "pcs"),
                unit_price=Decimal(str(raw["unit_price"])),
                line_total=(Decimal(str(raw["line_total"])) if raw.get("line_total") else None),
            )
        )
    return PurchaseOrderDocument(po_number=po_number, currency=currency, items=cleaned)


BASELINE_PO = {
    "po_number": "PO-2024-1001",
    "currency": "INR",
    "items": [
        {
            "description": "USB-C Cable, 1 Meter",
            "sku": "CBL-USB-C-1M",
            "quantity": 100,
            "unit_price": 150.00,
        },
        {
            "description": "USB-C Adapter 30W, White",
            "sku": "ADP-30W-WHT",
            "quantity": 5,
            "unit_price": 300.00,
        },
    ],
}


@pytest.fixture()
def baseline_po_json() -> str:
    return json.dumps(BASELINE_PO)


@pytest.fixture()
def baseline_po_csv() -> str:
    return (
        "po_number,currency,sku,description,quantity,unit,unit_price\n"
        "PO-2024-1001,INR,CBL-USB-C-1M,USB-C Cable 1 Meter,100,pcs,150.00\n"
        "PO-2024-1001,INR,ADP-30W-WHT,USB-C Adapter 30W White,5,pcs,300.00\n"
    )


# ---------------------------------------------------------------------------
# Synthetic PDF invoice builder (reportlab)
# ---------------------------------------------------------------------------
def _build_invoice_pdf(lines: list[tuple[str, float, float, float]], **meta) -> bytes:
    """Build a deterministic, text-layer invoice PDF with the given line items.

    Each line tuple is (description, quantity, unit_price, line_total).
    ``meta`` supports: vendor, invoice_number, po_number, subtotal, total,
    tax_label/tax_percent, currency_note, qty_suffix (e.g. ' pcs').
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from decimal import Decimal

    vendor = meta.get("vendor", "TechNova Components Pvt. Ltd.")
    invoice_number = meta.get("invoice_number", "INV-2024-777")
    po_number = meta.get("po_number", "PO-2024-1001")
    currency_note = meta.get("currency_note", "Currency: INR")
    qty_suffix = meta.get("qty_suffix", "")
    subtotal = meta.get("subtotal", sum(round(q * p, 2) for _, q, p, _ in lines))
    total = meta.get("total", subtotal)

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 50

    def emit(text, size=11, x=50):
        nonlocal y
        pdf.setFont("Helvetica", size)
        pdf.drawString(x, y, text)
        y -= 18

    emit(vendor, 14)
    emit("TAX INVOICE", 12)
    emit(f"Invoice No.: {invoice_number}")
    emit(f"Purchase Order No.: {po_number}")
    emit("Date: 21 Oct 2026")
    emit(currency_note)
    emit(f"GSTIN: 27ABCDE1234F1Z5   |   Payment Terms: Net 30")
    y -= 10
    emit("Description                    Qty        Unit Price        Total", 11)
    for description, quantity, price, line_total in lines:
        emit(
            f"{description:<30} {quantity:>8}{qty_suffix:<6}"
            f"{price:>12,.2f}{line_total:>16,.2f}",
            11,
        )
    y -= 10
    emit(f"Subtotal: {subtotal:,.2f}")
    tax_label = meta.get("tax_label", "Tax (10%)")
    if tax_label:
        emit(tax_label, 11, x=150)
        emit(f"{Decimal(str(total)) - Decimal(str(subtotal)):,.2f}", 11, x=350)
    emit(f"Total: {total:,.2f}")
    emit("Bank: TechNova Bank, A/C 000123456")

    pdf.save()
    return buffer.getvalue()


@pytest.fixture()
def make_invoice_pdf():
    return _build_invoice_pdf


SAMPLE_EXACT_LINES = [
    ("USB-C Cable, 1 Meter", 100, 150.00, 15000.00),
    ("USB-C Adapter 30W, White", 5, 300.00, 1500.00),
]


@pytest.fixture()
def exact_invoice_pdf() -> bytes:
    return _build_invoice_pdf(SAMPLE_EXACT_LINES)
