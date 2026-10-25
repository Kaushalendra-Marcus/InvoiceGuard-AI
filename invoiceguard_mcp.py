"""InvoiceGuard AI — MCP server wrapper.

Exposes the deployed InvoiceGuard REST API to AI agents (Claude Desktop,
Cursor, OpenCode, or any MCP client) as two tools, so an assistant can run
an invoice-vs-PO check directly from a conversation:

    analyze_invoice(invoice_path, purchase_order_path, session_id?)
        Compare an invoice document (PDF or image) against its Purchase
        Order (JSON or CSV file). Returns the full verdict JSON:
        status, matches, discrepancies with amounts, usage.

    usage(session_id)
        Remaining validations for the anonymous session today (IST day).

All platform limits apply unchanged — the agent reaches the same public API
a browser uses (daily anonymous-session limit, per-IP per-minute rate limit,
lifetime cap), and 429 responses are surfaced verbatim so the agent can
report them instead of retrying blindly.

Setup:

    pip install fastmcp httpx
    python invoiceguard_mcp.py            # stdio transport (default)

Client registration (Claude Desktop / claude_desktop_config.json):

    {
      "mcpServers": {
        "invoiceguard": {
          "command": "python",
          "args": ["/absolute/path/to/InvoiceGuard AI/invoiceguard_mcp.py"]
        }
      }
    }

Optional environment overrides:
    INVOICEGUARD_API_URL   target API (default: the deployed Render URL)
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

import httpx
from fastmcp import FastMCP

API_URL = os.environ.get(
    "INVOICEGUARD_API_URL", "https://invoiceguard-ai-15a1.onrender.com"
).rstrip("/")

mcp = FastMCP("invoiceguard")


def _result(resp: httpx.Response) -> dict:
    """Normalize any API response into an agent-readable dict."""
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if resp.status_code < 400:
        return payload
    detail = payload.get("detail") if isinstance(payload, dict) else None
    error = {
        "error": (detail.get("message") if isinstance(detail, dict) else None)
        or payload.get("message")
        or f"Request failed (HTTP {resp.status_code})."
    }
    if isinstance(detail, dict) and detail.get("code"):
        error["code"] = detail["code"]
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            error["retry_after_seconds"] = int(retry_after)
        if isinstance(detail, dict) and detail.get("usage"):
            error["usage"] = detail["usage"]
        if isinstance(detail, dict) and detail.get("rate"):
            error["rate"] = detail["rate"]
    return error


@mcp.tool
def analyze_invoice(
    invoice_path: str,
    purchase_order_path: str,
    session_id: str = "mcp-agent",
) -> dict:
    """Compare an invoice document against its Purchase Order and return the verdict.

    Args:
        invoice_path: Path to the invoice file (PDF or image: png/jpg/jpeg/webp).
        purchase_order_path: Path to the Purchase Order file (.json or .csv —
            CSV columns: po_number, currency, description, sku, quantity, unit, unit_price).
        session_id: Anonymous session id (8-64 chars of A-Z a-z 0-9 _ -).
            Each id gets 10 successful validations per day (IST reset).
    """
    invoice = Path(invoice_path)
    po = Path(purchase_order_path)
    missing = [
        label
        for label, path in (("invoice", invoice), ("purchase order", po))
        if not path.is_file()
    ]
    if missing:
        return {"error": f"File not found: {', '.join(missing)}"}

    po_mime = {
        ".json": "application/json",
        ".csv": "text/csv",
    }.get(po.suffix.lower())
    if po_mime is None:
        return {"error": "The Purchase Order file must be .json or .csv."}

    invoice_mime = mimetypes.guess_type(invoice.name)[0] or "application/pdf"
    try:
        resp = httpx.post(
            f"{API_URL}/api/v1/analyze",
            data={"session_id": session_id},
            files={
                "invoice": (invoice.name, invoice.read_bytes(), invoice_mime),
                "po_file": (po.name, po.read_bytes(), po_mime),
            },
            timeout=180,
        )
    except httpx.HTTPError as exc:
        return {"error": f"Could not reach {API_URL}: {exc}"}
    return _result(resp)


@mcp.tool
def usage(session_id: str = "mcp-agent") -> dict:
    """Remaining validations for this anonymous session today (resets midnight IST).

    Args:
        session_id: The anonymous session id to inspect (8-64 chars of A-Z a-z 0-9 _ -).
    """
    try:
        resp = httpx.get(
            f"{API_URL}/api/v1/usage",
            params={"session_id": session_id},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        return {"error": f"Could not reach {API_URL}: {exc}"}
    return _result(resp)


if __name__ == "__main__":
    mcp.run()  # stdio transport; INVOICEGUARD_API_URL overrides the target
