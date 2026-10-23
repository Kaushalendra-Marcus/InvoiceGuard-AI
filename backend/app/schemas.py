"""Pydantic models for API contracts and the analysis report."""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------
# Input contracts
# --------------------------------------------------------------------------
class POItem(BaseModel):
    """One line item of a Purchase Order (from JSON, CSV or manual entry)."""

    description: str = Field(min_length=1, max_length=512)
    sku: Optional[str] = Field(default=None, max_length=128)
    quantity: Decimal = Field(gt=0)
    quantity_unit: Optional[str] = Field(default=None, max_length=32)
    unit_price: Decimal = Field(gt=0)
    line_total: Optional[Decimal] = None

    @field_validator("description")
    @classmethod
    def description_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("description cannot be blank")
        return v


class PurchaseOrderDocument(BaseModel):
    """Structured PO as submitted by the user."""

    po_number: Optional[str] = Field(default=None, max_length=128)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    items: list[POItem] = Field(min_length=1)
    source_format: str = Field(default="json", pattern="^(json|csv|manual)$")

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, v: Optional[str]) -> Optional[str]:
        return v.upper() if v else v


# --------------------------------------------------------------------------
# Report contracts
# --------------------------------------------------------------------------
class OverallStatus(str, Enum):
    MATCHED = "MATCHED"
    DISCREPANCY_FOUND = "DISCREPANCY_FOUND"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class MatchMethod(str, Enum):
    SKU = "SKU"
    EXACT = "EXACT"
    FUZZY = "FUZZY"
    SEMANTIC = "SEMANTIC"
    NONE = "NONE"


class ConfidenceCategory(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


class DiscrepancyType(str, Enum):
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    UNIT_PRICE_MISMATCH = "UNIT_PRICE_MISMATCH"
    LINE_TOTAL_MISMATCH = "LINE_TOTAL_MISMATCH"
    INVOICE_TOTAL_MISMATCH = "INVOICE_TOTAL_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    INCOMPATIBLE_UNITS = "INCOMPATIBLE_UNITS"
    UNMATCHED_INVOICE_ITEM = "UNMATCHED_INVOICE_ITEM"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    MISSING_FINANCIAL_DATA = "MISSING_FINANCIAL_DATA"
    PO_TOTAL_EXCEEDED = "PO_TOTAL_EXCEEDED"
    EXTRACTION_INFO = "EXTRACTION_INFO"


class InvoiceItemOut(BaseModel):
    """An extracted invoice line item (original values preserved)."""

    line_index: int
    description: str
    sku: Optional[str] = None
    quantity: Optional[Decimal] = None
    quantity_unit: Optional[str] = None
    unit_price: Optional[Decimal] = None
    line_total: Optional[Decimal] = None
    raw_line: Optional[str] = None


class POItemOut(BaseModel):
    po_item_index: int
    description: str
    sku: Optional[str] = None
    quantity: Decimal
    quantity_unit: Optional[str] = None
    unit_price: Decimal
    line_total: Optional[Decimal] = None


class LineMatch(BaseModel):
    """Result of matching one invoice line item against the PO."""

    invoice_item_index: int
    invoice_description: str
    po_item_index: Optional[int] = None
    po_description: Optional[str] = None
    matched: bool
    method: MatchMethod = MatchMethod.NONE
    fuzzy_score: Optional[float] = Field(default=None, ge=0, le=100)
    semantic_score: Optional[float] = Field(default=None, ge=0, le=1)
    sku_match: bool = False
    ambiguous: bool = False
    competitor_po_index: Optional[int] = None
    competitor_description: Optional[str] = None
    competitor_score: Optional[float] = None
    confidence: ConfidenceCategory = ConfidenceCategory.LOW
    unit_compatible: bool = True
    contradictions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    invoice_item: InvoiceItemOut
    po_item: Optional[POItemOut] = None


class Discrepancy(BaseModel):
    """A single, explainable finding."""

    type: DiscrepancyType
    severity: Severity
    line_ref: Optional[str] = None  # description/index this refers to, for display
    message: str
    explanation: str
    invoice_value: Optional[str] = None   # display strings, original formatting
    po_value: Optional[str] = None
    difference: Optional[str] = None
    # Monetary impact computed only when the comparison basis is compatible.
    potential_excess_amount: Optional[Decimal] = None


class UsageSnapshot(BaseModel):
    daily_limit: int
    successful_validations: int
    remaining_validations: int
    usage_date: date
    reset_at: datetime
    timezone: str


class AnalysisReport(BaseModel):
    status: OverallStatus
    summary: str
    recommended_action: str
    # Extracted invoice header (missing values stay None - never invented).
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_po_number: Optional[str] = None
    invoice_currency: Optional[str] = None
    invoice_subtotal: Optional[Decimal] = None
    invoice_total: Optional[Decimal] = None
    po_number: Optional[str] = None
    po_currency: Optional[str] = None
    effective_currency: Optional[str] = None
    invoice_items: list[InvoiceItemOut]
    po_items: list[POItemOut]
    matches: list[LineMatch]
    unmatched_invoice_items: list[InvoiceItemOut]
    po_items_without_invoice: list[POItemOut]
    discrepancies: list[Discrepancy]
    extraction_warnings: list[str] = Field(default_factory=list)
    semantic_matching_available: bool = True
    analyzed_at: datetime = Field(default_factory=datetime.now)


class AnalyzeResponse(AnalysisReport):
    usage: UsageSnapshot
