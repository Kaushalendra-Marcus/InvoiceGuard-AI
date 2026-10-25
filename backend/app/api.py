"""HTTP API: analysis, usage queries (and health lives in main.py)."""

from __future__ import annotations

import csv
import io
import json
import logging
import zoneinfo
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from .analysis_service import run_analysis
from .config import settings
from .document_parser import DocumentError
from .schemas import AnalyzeResponse, PurchaseOrderDocument, UsageSnapshot
from .usage_limiter import validate_session_id

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# PO input parsing (JSON/CSV text or uploaded files)
# ---------------------------------------------------------------------------
def _po_from_json_text(text: str, source_format: str = "json") -> PurchaseOrderDocument:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "INVALID_PO_JSON", "message": f"Purchase Order JSON is invalid: {exc}"}
        )
    if isinstance(payload, list):  # bare list of items is accepted
        payload = {"items": payload}
    if not isinstance(payload, dict) or "items" not in payload or not isinstance(payload["items"], list):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_PO_STRUCTURE",
                "message": "Purchase Order JSON must contain an 'items' array (or be an array itself).",
            },
        )
    try:
        return PurchaseOrderDocument.model_validate({**payload, "source_format": source_format})
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_PO_FIELDS", "message": f"Purchase Order data is invalid: {exc}"},
        )


def _po_from_csv_text(text: str) -> PurchaseOrderDocument:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(
            status_code=422, detail={"code": "INVALID_PO_CSV", "message": "Purchase Order CSV has no header row."}
        )
    required = {"description", "quantity", "unit_price"}
    normalized_fields = [(f or "").strip().lower() for f in reader.fieldnames]
    missing = required - set(normalized_fields)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_PO_CSV",
                "message": (
                    "Purchase Order CSV is missing required column(s): "
                    f"{', '.join(sorted(missing))}. The PO side must be a purchase "
                    "order document (one line per ordered item) — an invoice or a "
                    "combined invoice/PO test dataset will not parse as a PO."
                ),
            },
        )

    def pick(row: dict[str, Any], key: str) -> Any:
        for actual, normalized in zip(row.keys(), normalized_fields):
            if normalized == key:
                value = row.get(actual)
                if isinstance(value, str):
                    value = value.strip()
                return value or None
        return None

    rows = [row for row in reader]
    items: list[dict[str, Any]] = []
    for position, row in enumerate(rows, start=2):
        try:
            item = {
                "description": pick(row, "description"),
                "sku": pick(row, "sku"),
                "quantity": pick(row, "quantity"),
                "quantity_unit": pick(row, "unit") or pick(row, "quantity_unit"),
                "unit_price": pick(row, "unit_price"),
                "line_total": pick(row, "line_total"),
            }
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail={"code": "INVALID_PO_CSV", "message": f"CSV row {position} failed: {exc}"}
            )
        if item["description"] is None:
            continue
        items.append(item)

    # Optional header-level columns, repeated per row in real-world exports:
    # take the first non-empty value provided anywhere in the file.
    payload: dict[str, Any] = {"items": items, "source_format": "csv"}
    for field in ("po_number", "currency"):
        for row in rows:
            value = pick(row, field)
            if value:
                payload[field] = value.upper() if field == "currency" else value
                break
    return _po_from_json_text(json.dumps(payload), source_format="csv")


def _resolve_purchase_order(
    po_json_text: str | None, po_csv_text: str | None, po_file: UploadFile | None
) -> PurchaseOrderDocument:
    if po_json_text:
        return _po_from_json_text(po_json_text)
    if po_csv_text:
        return _po_from_csv_text(po_csv_text)
    if po_file is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "PO_REQUIRED", "message": "Provide the Purchase Order as JSON/CSV (file or typed data)."},
        )
    filename = po_file.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    raw = po_file.file.read()
    data = (raw or b"").decode("utf-8-sig", errors="replace")
    if ext == "json" or (ext not in ("csv",) and data.lstrip().startswith(("{", "["))):
        return _po_from_json_text(data, source_format="json")
    if ext == "csv":
        return _po_from_csv_text(data)
    raise HTTPException(
        status_code=422,
        detail={"code": "UNSUPPORTED_PO_TYPE", "message": "The Purchase Order file must be JSON or CSV."},
    )


def _session_or_422(session_id: str | None, header_id: str | None) -> str:
    sid = validate_session_id(session_id or header_id)
    if sid is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_SESSION_ID",
                "message": "A valid anonymous session id (8-64 chars of A-Z a-z 0-9 _ -) is required.",
            },
        )
    return sid


def _limiter(request: Request):
    limiter = getattr(request.app.state, "limiter", None)
    if limiter is None:
        raise HTTPException(status_code=500, detail="Usage limiter not initialized.")
    return limiter


def _rate_limiter(request: Request):
    """IP-level limiter; optional so tests can construct a bare app."""
    return getattr(request.app.state, "rate_limiter", None)


def _client_ip(request: Request) -> str:
    """Client IP as seen through the proxy (Render) or the socket directly."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client and client.host else "unknown"


def _rate_limit_429(decision) -> HTTPException:
    rate = {
        "per_minute": decision.per_minute,
        "lifetime": decision.lifetime,
        "minute_requests": decision.minute_count,
        "lifetime_requests": decision.lifetime_count,
    }
    if decision.scope == "lifetime":
        return HTTPException(
            status_code=429,
            detail={
                "code": "RATE_LIMIT_LIFETIME",
                "message": (
                    f"Lifetime limit of {decision.lifetime} analyses reached for this "
                    "client. The counter persists with the service database and "
                    "resets only when the service is redeployed or restarted."
                ),
                "rate": rate,
            },
        )
    return HTTPException(
        status_code=429,
        headers={"Retry-After": str(decision.seconds_until_minute_reset)},
        detail={
            "code": "RATE_LIMIT_MINUTE",
            "message": (
                f"Rate limit of {decision.per_minute} analyses per minute reached "
                f"for this client. Try again in "
                f"{decision.seconds_until_minute_reset} seconds."
            ),
            "rate": rate,
        },
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/api/v1/analyze", response_model=AnalyzeResponse)
async def analyze_invoice(
    request: Request,
    invoice: Annotated[UploadFile, File(description="Invoice PDF or image")],
    session_id: Annotated[str | None, Form()] = None,
    po_json: Annotated[str | None, Form(description="Purchase Order as JSON text")] = None,
    po_csv: Annotated[str | None, Form(description="Purchase Order as CSV text")] = None,
    po_file: UploadFile | None = File(None, description="Purchase Order as a JSON or CSV file"),
    purchase_order_file: UploadFile | None = File(None, alias="purchase_order_file"),
    x_session_id: Annotated[str | None, Header()] = None,
) -> AnalyzeResponse:
    sid = _session_or_422(session_id, x_session_id)
    limiter = _limiter(request)

    # ---------------------------------------------------------------- IP caps
    rate_limiter = _rate_limiter(request)
    if rate_limiter is not None:
        decision = rate_limiter.hit(_client_ip(request))
        if not decision.allowed:
            raise _rate_limit_429(decision)

    invoice_data = await invoice.read()
    purchase_order = _resolve_purchase_order(po_json, po_csv, po_file or purchase_order_file)

    invoice_name = invoice.filename or "invoice.pdf"
    # Invalid uploads are rejected *before* consuming allowance.
    if not invoice_name:
        invoice_name = "invoice.pdf"

    # ---------------------------------------------------------------- reserve
    usage_date = limiter.try_reserve(sid)
    if usage_date is None:
        snapshot = limiter.snapshot(sid)
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": str(int(_seconds_until(snapshot.reset_at)))},
            detail={
                "code": "DAILY_LIMIT_REACHED",
                "message": (
                    "Daily limit of "
                    f"{snapshot.daily_limit} successful analyses reached for this anonymous session. "
                    "The limit resets at midnight IST."
                ),
                "usage": snapshot.model_dump(mode="json"),
            },
        )

    # -------------------------------------------------------------- analysis
    try:
        report = await run_in_threadpool(
            run_analysis, invoice_name, invoice_data, purchase_order
        )
    except DocumentError as exc:
        limiter.release(sid, usage_date)
        raise HTTPException(
            status_code=400, detail={"code": exc.code, "message": exc.message}
        ) from exc
    except Exception:
        limiter.release(sid, usage_date)
        logger.exception("Analysis failed unexpectedly")
        raise HTTPException(
            status_code=500,
            detail={"code": "ANALYSIS_FAILED", "message": "Something went wrong while analyzing the invoice. Please try again."},
        )

    limiter.commit(sid, usage_date)
    snapshot = limiter.snapshot(sid)

    payload = report.model_dump()
    payload["usage"] = snapshot.model_dump(mode="json")
    return AnalyzeResponse(**payload)


@router.get("/api/v1/usage", response_model=UsageSnapshot)
def get_usage(
    request: Request,
    session_id: str | None = None,
    x_session_id: Annotated[str | None, Header()] = None,
) -> UsageSnapshot:
    sid = _session_or_422(session_id, x_session_id)
    return _limiter(request).snapshot(sid)


def _seconds_until(reset_at: datetime) -> float:
    tz = zoneinfo.ZoneInfo(settings.timezone_name)
    target = reset_at.replace(tzinfo=tz) if reset_at.tzinfo is None else reset_at
    delta = (target - datetime.now(tz)).total_seconds()
    return max(0.0, delta)
