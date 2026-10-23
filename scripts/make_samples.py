#!/usr/bin/env python
"""Regenerate the demo drop-ins in ``samples/``.

By default the script only creates files that are missing so it never
clobbers hand-made content. Pass ``--force`` to rebuild everything:

    python scripts/make_samples.py [--force]

The generated invoice is a text-layer PDF (reportlab) laid out to exercise
the extractor: header fields, a three-line item table with an Indian-style
"pcs" unit column and grouped totals.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"

PO_ITEMS = [
    {"description": "USB-C Cable, 1 Meter", "sku": "CBL-USB-C-1M", "quantity": 100,
     "quantity_unit": "pcs", "unit_price": 150.00},
    {"description": "Laptop Stand", "sku": "STD-LAP-001", "quantity": 50,
     "quantity_unit": "pcs", "unit_price": 800.00},
    {"description": "HDMI Adapter", "sku": "HDMI-ADP-001", "quantity": 10,
     "quantity_unit": "pcs", "unit_price": 450.00},
]


def write_po_samples(force: bool) -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)

    po_json = {
        "po_number": "PO-2024-0042",
        "currency": "INR",
        "items": PO_ITEMS,
    }
    json_path = SAMPLES / "po_sample.json"
    if force or not json_path.exists():
        json_path.write_text(json.dumps(po_json, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {json_path}")

    csv_path = SAMPLES / "po_sample.csv"
    if force or not csv_path.exists():
        lines = ["po_number,currency,sku,description,quantity,unit,unit_price"]
        for item in PO_ITEMS:
            lines.append(
                f"PO-2024-0042,INR,{item['sku']},{item['description']},"
                f"{item['quantity']},{item['quantity_unit']},{item['unit_price']:.2f}"
            )
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"wrote {csv_path}")


def write_invoice_pdf(force: bool) -> None:
    pdf_path = SAMPLES / "invoice_sample.pdf"
    if not force and pdf_path.exists():
        print(f"{pdf_path} exists; keeping it (use --force to rebuild)")
        return
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:
        print(
            "reportlab is required to generate the PDF invoice: "
            "pip install reportlab (see backend/requirements.txt)",
            file=sys.stderr,
        )
        raise SystemExit(2)

    from decimal import Decimal

    invoice_lines = [
        ("Type-C Charging Cable, 100cm", 120, Decimal("150.00"), Decimal("18000.00"), "pcs"),
        ("Laptop Stand", 50, Decimal("800.00"), Decimal("40000.00"), "pcs"),
        ("HDMI Adapter", 10, Decimal("450.00"), Decimal("4500.00"), "pcs"),
    ]
    subtotal = sum(q * p for _, q, p, _, _ in invoice_lines)

    buffer = pdf_path.parent / (pdf_path.stem + ".tmp")
    c = canvas.Canvas(str(buffer), pagesize=A4)
    width, height = A4
    y = height - 50

    def emit(text: str, size: int = 11, x: float = 50) -> None:
        nonlocal y
        c.setFont("Helvetica", size)
        c.drawString(x, y, text)
        y -= 18

    emit("INVOICE", 13)
    emit("MarcusTech Supplies Pvt. Ltd.", 12)
    emit("101 Tech Park, Baner Road, Pune 411045", 10)
    emit("Invoice No: INV-2026-1187")
    emit("Invoice Date: 24 October 2026")
    emit("PO No: PO-2024-0042")
    emit("Bill To: Insight Retail LLP, Pune")
    emit("Currency: INR")
    y -= 8
    emit("Item                                        Qty    Unit        Unit Price (INR)    Line Total (INR)")
    for description, quantity, price, line_total, unit in invoice_lines:
        emit(
            f"{description:<44}{quantity:>8}    {unit:<6}"
            f"{price:>14,.2f}{line_total:>20,.2f}"
        )
    y -= 8
    emit(f"Subtotal:        {subtotal:,.2f}", 11, x=330)
    emit("Tax:             as applicable", 11, x=330)
    emit(f"Total:           {subtotal:,.2f}", 11, x=330)
    emit("Payment terms: Net 30 days | Bank: TechNova Bank A/C 000123456", 10)

    c.save()
    buffer.replace(pdf_path)
    print(f"wrote {pdf_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="rebuild ALL sample files")
    args = parser.parse_args()
    write_po_samples(args.force)
    write_invoice_pdf(args.force)


if __name__ == "__main__":
    main()
