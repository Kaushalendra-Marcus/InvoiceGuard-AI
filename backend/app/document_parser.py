"""Invoice document extraction (modular replacement layer).

Supports:
- text-based PDFs (pypdf text layer)
- scanned PDFs and images via OCR **when** the optional dependencies are
  installed (``pytesseract`` + ``pillow`` + the ``tesseract`` binary). When
  they are missing the parser fails with an explicit, actionable error
  instead of inventing content.

Design rules:
- Never invents a missing financial value; missing fields stay ``None`` and
  surface as extraction warnings.
- Original line text is preserved for display and auditing.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from io import BytesIO

from .config import settings
from .normalizer import UNIT_ALIASES, detect_currency, parse_decimal

logger = logging.getLogger(__name__)


class DocumentError(Exception):
    """Raised when a document cannot be parsed at all (client error)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RawLineItem:
    """One extracted invoice line, with values only when actually extracted."""

    description: str
    sku: str | None = None
    quantity: Decimal | None = None
    quantity_unit: str | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    raw_text: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class InvoiceParseResult:
    vendor_name: str | None = None
    invoice_number: str | None = None
    po_number: str | None = None
    currency: str | None = None
    items: list[RawLineItem] = field(default_factory=list)
    subtotal: Decimal | None = None
    total: Decimal | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------
_CURRENCY_PREFIX = r"(?:₹|rs\.?|inr|\$|usd|€|eur|£|gbp)"
# Pure numeric cell: optional currency prefix, digits with grouping, decimals.
_PRICE_CELL_RE = re.compile(
    r"^(?:%s)?\s*(\d{1,12}(?:,\d{2,3})*(?:\.\d{1,3})?|\d+(?:\.\d{1,3})?)[%%]?$" % _CURRENCY_PREFIX,
    re.IGNORECASE,
)
# Quantity cell: optional trailing count-unit only (physical units belong in
# descriptions, e.g. '100cm' must stay part of the product description).
_COUNT_UNITS_RE = r"(?:pcs?|nos?\.?|no\.?|numbers?|units?|ea|each|qty)"
_QTY_CELL_RE = re.compile(r"^(\d{1,9}(?:\.\d{1,3})?)\s*%s?\.?$" % _COUNT_UNITS_RE, re.IGNORECASE)

_INVOICE_NO_RE = re.compile(
    r"(?:tax\s+)?invoice\s*(?:no\.?|number|#|id)?\s*[:\-#]?\s*([A-Za-z0-9][A-Za-z0-9/\-]{2,})"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
_PO_NO_RE = re.compile(
    r"\b(?:purchase\s*order|p\.?o\.?)\s*(?:no\.?|number|#|id)?\s*[:\-#]?\s*([A-Za-z0-9][A-Za-z0-9/\-]+)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
_SUBTOTAL_RE = re.compile(
    r"(?:sub\s*-?\s*total|taxable\s*(?:value|amount))\s*[:=]?\s*(?:%s)?\s*([0-9,]+\.[0-9]{2})"
    % _CURRENCY_PREFIX,
    re.IGNORECASE,
)
_GRAND_TOTAL_RE = re.compile(
    r"(?:grand\s*total|net\s*(?:payable|amount)|(?:invoice|final)\s*total|total\s*payable|"
    r"balance\s*due)\s*[:=]?\s*(?:%s)?\s*([0-9,]+\.[0-9]{2})" % _CURRENCY_PREFIX,
    re.IGNORECASE,
)
_PLAIN_TOTAL_RE = re.compile(
    r"(?:^|\s)total\s*[:=]?\s*(?:%s)?\s*([0-9,]+\.[0-9]{2})" % _CURRENCY_PREFIX,
    re.IGNORECASE,
)
_ITEM_ROW_SKIP_RE = re.compile(
    r"(?i)\b(sub\s*-?\s*total|grand\s*total|total|cgst|sgst|igst|cess|tax\b|discount|"
    r"round(?:ing|ed)?\s*off|freight|shipping|balance|due\b|amount\s*in\s*words|"
    r"gstin|pan\s*no|h\.?s\.?n\.?|sac\s*code)\b"
)
_HEADER_SKIP_RE = re.compile(
    r"(?i)\b(tax\s+invoice|invoice|original|duplicate|triplicate|consignee|"
    r"terms|delivery\s*note|mode\s*of\s*payment)\b"
)
_MIN_TEXT_LENGTH_FOR_PDF = 60


# ---------------------------------------------------------------------------
# OCR support (optional)
# ---------------------------------------------------------------------------
def ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return shutil.which("tesseract") is not None


def _ocr_image_bytes(data: bytes) -> str:
    import pytesseract
    from PIL import Image

    image = Image.open(BytesIO(data))
    return pytesseract.image_to_string(image, lang=settings.ocr_language)


def _ocr_text_pdf(pdf_bytes: bytes) -> str:
    """Render the first pages of a scanned PDF and OCR them (pdftoppm path)."""
    if not shutil.which("pdftoppm"):
        raise DocumentError(
            "OCR_UNAVAILABLE",
            "The PDF has no text layer (likely scanned) and rendering tools are missing. "
            "Install poppler-utils plus tesseract-ocr/pytesseract, or upload a text-based "
            "PDF invoice instead.",
        )
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        raise DocumentError(
            "OCR_UNAVAILABLE",
            "OCR dependencies (pytesseract, pillow) are not installed. Install them and the "
            "tesseract binary to process scanned documents, or upload a text-based PDF.",
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = f"{tmp}/invoice.pdf"
        with open(src, "wb") as fh:
            fh.write(pdf_bytes)
        try:
            subprocess.run(
                ["pdftoppm", "-r", "200", "-png", "-f", "1", "-l", "3", src, f"{tmp}/page"],
                check=True,
                timeout=60,
                capture_output=True,
            )
        except Exception as exc:
            raise DocumentError("OCR_RENDER_FAILED", f"Could not rasterize the PDF for OCR: {exc}")
        texts: list[str] = []
        import glob as _glob
        import os

        for page_path in sorted(_glob.glob(f"{tmp}/page*.png")):
            texts.append(
                pytesseract.image_to_string(Image.open(page_path), lang=settings.ocr_language)
            )
            os.remove(page_path)
        return "\n".join(texts)


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - installed with requirements
        raise DocumentError("PARSER_UNAVAILABLE", f"PDF parser unavailable: {exc}")
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise DocumentError("INVALID_PDF", f"The PDF could not be read: {exc}")
    return "\n".join(pages).strip()


# ---------------------------------------------------------------------------
# Numeric cell helpers
# ---------------------------------------------------------------------------
def _price_cell(token: str) -> Decimal | None:
    match = _PRICE_CELL_RE.match(token.strip())
    if not match:
        return None
    return parse_decimal(match.group(1))


def _qty_cell(token: str) -> tuple[Decimal, str | None] | None:
    stripped = token.strip()
    match = _QTY_CELL_RE.match(stripped)
    if not match:
        return None
    value = parse_decimal(match.group(1))
    if value is None:
        return None
    unit_raw = stripped.replace(match.group(1), "").strip().rstrip(".").lower()
    unit_key = unit_raw.rstrip(".") if unit_raw else ""
    family = UNIT_ALIASES.get(unit_key)
    return value, family


# ---------------------------------------------------------------------------
# Table line classification
# ---------------------------------------------------------------------------
_TRIPLE_TOLERANCE_ABS = Decimal("0.05")
_TRIPLE_TOLERANCE_REL = Decimal("0.005")


def _fits_product(a: Decimal, b: Decimal, c: Decimal) -> bool:
    product = a * b
    tolerance = max(_TRIPLE_TOLERANCE_ABS, abs(c) * _TRIPLE_TOLERANCE_REL)
    return (product - c).copy_abs() <= tolerance


def _decimals_of(value: Decimal) -> int:
    tup = value.as_tuple()
    return max(0, -tup.exponent) if isinstance(tup.exponent, int) else 0


def _assign_triple(numbers: list[tuple[int, Decimal]], notes: list[str]) -> tuple[Decimal, str | None, Decimal, Decimal] | None:
    """Choose (qty, unit, unit_price, line_total) from ordered numeric tokens."""
    import itertools

    n = len(numbers)
    for i, j, k in itertools.combinations(range(n), 3):
        i_tok, v_q = numbers[i]
        j_tok, v_p = numbers[j]
        k_tok, v_t = numbers[k]
        for a, b, c in ((v_q, v_p, v_t), (v_p, v_q, v_t)):
            if _fits_product(a, b, c):
                qty, price = _orient_qty_price(a, b, i_tok, j_tok, notes)
                return qty, None, price, c

    # No confident (qty x price = total) fit: fall back to formatting
    # heuristics on the first and last numbers. Missing leg stays None.
    a, b = numbers[0][1], numbers[-1][1]
    a_dec, b_dec = _decimals_of(a), _decimals_of(b)
    if a_dec == 0 and b_dec >= 1 and n == 2:
        # [quantity, money] e.g. '120  18,000.00'
        notes.append(
            "Line layout interpreted as quantity followed by a monetary value; "
            "unit price not stated separately on this line."
        )
        return a, None, None, b
    if a_dec == 0 and b_dec == 0 and n >= 2:
        # [quantity, ordered-quantity?] ambiguous: report quantity of the last
        # integer and leave money fields empty rather than guessing.
        notes.append("Two numbers without decimal formatting could not be classified.")
        return numbers[-1][1], None, None, None
    if a_dec <= 2 and b_dec >= 2 and b > a:
        # [unit price, line total]
        notes.append(
            "Column order inferred from value formatting: first number interpreted "
            "as unit price, second as line total; quantity not stated on this line."
        )
        return None, None, a, b
    return None


def _orient_qty_price(a: Decimal, b: Decimal, ia: int, ib: int, notes: list[str]) -> tuple[Decimal, Decimal]:
    """Decide which factor is quantity vs unit price."""
    a_dec, b_dec = _decimals_of(a), _decimals_of(b)
    if a_dec == 0 and b_dec >= 2:
        return a, b
    if b_dec == 0 and a_dec >= 2:
        return b, a
    if a_dec == 0 and b_dec == 0:
        # Both integers: the smaller value is the likelier count quantity
        # (typical invoice rows bill counts against higher per-unit prices).
        return min(a, b), max(a, b)
    if a_dec > b_dec:
        return b, a
    return a, b


def _classify_line(line: str) -> RawLineItem | None:
    """Turn a raw text line into an invoice item when it looks like a table row."""
    if _ITEM_ROW_SKIP_RE.search(line):
        return None
    tokens = [t for t in line.split() if t]
    if len(tokens) < 2:
        return None

    numbers: list[tuple[int, Decimal]] = []
    qty_unit: str | None = None
    for idx, token in enumerate(tokens):
        price_val = _price_cell(token)
        if price_val is not None:
            numbers.append((idx, price_val))
            continue
        qty_val = _qty_cell(token)
        if qty_val is not None:
            value, family = qty_val
            numbers.append((idx, value))
            if family is not None:
                qty_unit = family

    if len(numbers) < 2:
        return None

    first_numeric_idx = numbers[0][0]
    description = " ".join(tokens[:first_numeric_idx]).strip(" ,:;-")
    notes: list[str] = []
    assigned = _assign_triple(numbers, notes)
    if not description or len(description) < 2:
        # A line whose description is empty is likely a totals row fragment.
        return None

    item = RawLineItem(description=description, raw_text=line, notes=notes)
    if qty_unit is not None:
        item.quantity_unit = qty_unit.lower()
    if assigned is not None:
        qty, unit, price, total = assigned
        item.quantity = qty
        item.unit_price = price
        item.line_total = total
        if unit is not None:
            item.quantity_unit = unit
        if price is not None and total is not None and qty is not None:
            computed = qty * price
            tolerance = max(_TRIPLE_TOLERANCE_ABS, total * _TRIPLE_TOLERANCE_REL)
            if (computed - total).copy_abs() > tolerance:
                item.notes.append(
                    "Stated values on this line are internally inconsistent "
                    "(quantity x unit price differs from the line total)."
                )
    else:
        item.notes.append("Numeric values on this line could not be reliably classified.")
    item.currency = currency_hint  # attached by caller if provided
    return item


# ---------------------------------------------------------------------------
# Text-level extraction
# ---------------------------------------------------------------------------
_VENDOR_STOP_RE = re.compile(
    r"(?i)\b(tax invoice|invoice|gstin|gst|date|original|duplicate|triplicate|"
    r"consignee|details|bill to|ship to|purchase order|p\.?o\.?\s*no|description)\b"
)


def _find_vendor(lines: list[str]) -> str | None:
    for line in lines[:10]:
        candidate = line.strip()
        if not candidate or not re.search(r"[A-Za-z]{3,}", candidate):
            continue
        if _VENDOR_STOP_RE.search(candidate):
            continue
        return candidate[:200]
    return None


def _search_per_line(lines: list[str], pattern: re.Pattern[str], limit: int | None = None) -> str | None:
    """Match a labeled pattern line-by-line so captures cannot cross lines."""
    candidates = lines if limit is None else lines[:limit]
    for line in candidates:
        match = pattern.search(line)
        if match:
            return match.group(1)
    return None


def _extract_invoice(text: str) -> InvoiceParseResult:
    lines = [ln for ln in text.splitlines()]
    result = InvoiceParseResult()

    result.invoice_number = _search_per_line(lines, _INVOICE_NO_RE, limit=8)
    result.po_number = _search_per_line(lines, _PO_NO_RE, limit=8)
    result.vendor_name = _find_vendor([ln for ln in lines if ln.strip()])

    result.currency = detect_currency(text[:2000])

    subtotal_match = _SUBTOTAL_RE.search(text)
    if subtotal_match:
        result.subtotal = parse_decimal(subtotal_match.group(1))
    grand_match = _GRAND_TOTAL_RE.search(text)
    if grand_match:
        result.total = parse_decimal(grand_match.group(1))
    if result.total is None:
        candidates = [parse_decimal(m.group(1)) for m in _PLAIN_TOTAL_RE.finditer(text)]
        candidates = [c for c in candidates if c is not None]
        if result.subtotal is not None:
            differing = [c for c in candidates if c != result.subtotal]
            result.total = differing[-1] if differing else (candidates[-1] if candidates else None)
        elif candidates:
            result.total = candidates[-1]

    for line in lines:
        item = _classify_line(line)
        if item is None:
            continue
        result.items.append(item)
        for note in item.notes:
            result.warnings.append(f"Line '{item.description[:60]}': {note}")

    if not result.items:
        raise DocumentError(
            "NO_LINE_ITEMS",
            "No invoice line items could be recognized in this document. "
            "Please ensure the invoice contains a line-item table with quantity, "
            "unit price and totals.",
        )
    if result.invoice_number is None:
        result.warnings.append("Invoice number not found.")
    if result.currency is None:
        result.warnings.append(
            "Currency could not be detected; it is assumed from the Purchase Order context."
        )
    return result


def parse_invoice_text(text: str) -> InvoiceParseResult:
    """Parse plain invoice text (used by tests and by the OCR pipeline)."""
    if not text or not text.strip():
        raise DocumentError("EMPTY_DOCUMENT", "The document appears to contain no text.")
    return _extract_invoice(text)


def parse_invoice_file(filename: str, data: bytes) -> InvoiceParseResult:
    """Main entry: parse an uploaded invoice PDF or image."""
    if not data:
        raise DocumentError("EMPTY_FILE", "The uploaded invoice file is empty.")
    if len(data) > settings.max_upload_bytes:
        raise DocumentError("FILE_TOO_LARGE", "The invoice file exceeds the 10 MB upload limit.")

    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
    if ext not in settings.allowed_invoice_extensions:
        raise DocumentError(
            "UNSUPPORTED_FILE_TYPE",
            "The invoice must be a PDF or an image (PNG/JPG/JPEG/WEBP).",
        )

    if ext == "pdf":
        if not data.lstrip()[:5].startswith(b"%PDF"):
            raise DocumentError("INVALID_PDF", "The uploaded file does not look like a valid PDF.")
        text = _extract_pdf_text(data)
        if len(text) < _MIN_TEXT_LENGTH_FOR_PDF:
            # Scanned PDF without a text layer: OCR it.
            if ocr_available():
                text = (_ocr_text_pdf(data) or "").strip()
            else:
                raise DocumentError(
                    "OCR_UNAVAILABLE",
                    "This PDF has no selectable text (likely a scan) and OCR is not "
                    "available in this deployment. Install tesseract-ocr and pytesseract, "
                    "or upload a text-based PDF.",
                )
        if len(text) < _MIN_TEXT_LENGTH_FOR_PDF:
            raise DocumentError(
                "INSUFFICIENT_TEXT",
                "Too little text could be extracted from the PDF to analyze the invoice.",
            )
        return parse_invoice_text(text)

    # Image formats: OCR required.
    if not ocr_available():
        raise DocumentError(
            "OCR_UNAVAILABLE",
            "Image invoices require OCR (tesseract-ocr + pytesseract + pillow), which is "
            "not available in this deployment. Please upload a text-based PDF invoice.",
        )
    try:
        text = _ocr_image_bytes(data).strip()
    except Exception as exc:
        raise DocumentError("OCR_FAILED", f"OCR failed on the uploaded image: {exc}")
    if len(text) < _MIN_TEXT_LENGTH_FOR_PDF:
        raise DocumentError(
            "INSUFFICIENT_TEXT",
            "OCR produced too little text from the image to analyze the invoice.",
        )
    return parse_invoice_text(text)
