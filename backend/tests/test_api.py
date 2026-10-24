"""API-level tests: analyze flow, usage accounting, error handling.

The semantic matcher is disabled for most tests (fast + deterministic); the
worked-spec-example test briefly enables it to assert the full semantic path.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.matching import SemanticMatcher, set_semantic_matcher


@pytest.fixture(autouse=True)
def semantic_disabled_by_default():
    set_semantic_matcher(SemanticMatcher(enabled=False))
    yield
    set_semantic_matcher(None)


def session() -> str:
    return "anon-" + uuid.uuid4().hex[:12]


def analyze(client, sid: str, invoice_pdf: bytes, po_payload: str | None = None, po_filename: str = "po.json"):
    data = {"session_id": sid}
    files = {"invoice": ("invoice.pdf", invoice_pdf, "application/pdf")}
    if po_payload is not None:
        files["po_file"] = (po_filename, po_payload, "application/octet-stream")
    return client.post("/api/v1/analyze", data=data, files=files)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_analyze_exact_match_flow(client, exact_invoice_pdf, baseline_po_json):
    sid = session()
    response = analyze(client, sid, exact_invoice_pdf, baseline_po_json)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "MATCHED"
    assert payload["invoice_number"] == "INV-2024-777"
    assert payload["po_number"] == "PO-2024-1001"
    assert payload["effective_currency"] == "INR"
    matched = payload["matches"]
    assert len(matched) == 2
    assert all(m["matched"] for m in matched)
    assert payload["unmatched_invoice_items"] == []
    usage = payload["usage"]
    assert usage["daily_limit"] == 10
    assert usage["successful_validations"] == 1
    assert usage["remaining_validations"] == 9


def test_analyze_semantic_example_routes_to_review(client, make_invoice_pdf, real_semantic_fixture):
    """The spec's worked example: differently-worded line + quantity excess."""
    set_semantic_matcher(real_semantic_fixture)
    invoice = make_invoice_pdf(
        [("Type-C Charging Cable, 100cm", 120, 150.00, 18000.00)],
        subtotal=18000,
        total=18900,
        tax_label="GST",
    )
    po_payload = json.dumps(
        {
            "po_number": "PO-2024-1001",
            "currency": "INR",
            "items": [
                {"description": "USB-C Cable, 1 Meter", "sku": "CBL-USB-C-1M",
                 "quantity": 100, "unit_price": 150.00}
            ],
        }
    )
    response = analyze(client, session(), invoice, po_payload)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "NEEDS_REVIEW"
    match = payload["matches"][0]
    assert match["matched"] is True
    assert match["method"] == "SEMANTIC"
    discrepancy = next(
        d for d in payload["discrepancies"] if d["type"] == "QUANTITY_MISMATCH"
    )
    assert float(discrepancy["potential_excess_amount"]) == 3000.0
    assert discrepancy["invoice_value"] == "120"
    assert discrepancy["po_value"] == "100"
    assert "₹3,000.00" in discrepancy["explanation"]


@pytest.fixture(scope="module")
def real_semantic_fixture():
    matcher = SemanticMatcher(enabled=True)
    if not matcher.available:
        pytest.skip("sentence embedding model unavailable in this environment")
    return matcher


def test_invalid_po_json_is_422_and_does_not_consume_allowance(client, exact_invoice_pdf):
    sid = session()
    response = analyze(client, sid, exact_invoice_pdf, "{not json")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_PO_JSON"

    usage = client.get("/api/v1/usage", params={"session_id": sid}).json()
    assert usage["successful_validations"] == 0
    assert usage["remaining_validations"] == 10


def test_invalid_pdf_is_rejected_without_consuming_allowance(client, baseline_po_json):
    sid = session()
    response = analyze(client, sid, b"this is not a pdf", baseline_po_json)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_PDF"

    usage = client.get("/api/v1/usage", params={"session_id": sid}).json()
    assert usage["successful_validations"] == 0
    assert usage["remaining_validations"] == 10


def test_missing_po_returns_422(client, exact_invoice_pdf):
    response = analyze(client, session(), exact_invoice_pdf, None)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PO_REQUIRED"


def test_invalid_session_id_rejected(client, exact_invoice_pdf, baseline_po_json):
    response = analyze(client, "bad id!!", exact_invoice_pdf, baseline_po_json)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_SESSION_ID"


def test_eleventh_successful_analysis_rejected_with_429(client, exact_invoice_pdf, baseline_po_json):
    sid = session()
    responses = [analyze(client, sid, exact_invoice_pdf, baseline_po_json) for _ in range(10)]
    assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]

    eleventh = analyze(client, sid, exact_invoice_pdf, baseline_po_json)
    assert eleventh.status_code == 429
    detail = eleventh.json()["detail"]
    assert detail["code"] == "DAILY_LIMIT_REACHED"
    assert detail["usage"]["remaining_validations"] == 0
    assert detail["usage"]["successful_validations"] == 10
    assert "midnight" in detail["message"].lower()


def test_usage_endpoint_reports_allowance(client, exact_invoice_pdf, baseline_po_json):
    sid = session()
    usage = client.get("/api/v1/usage", params={"session_id": sid}).json()
    assert usage["remaining_validations"] == 10

    respond = analyze(client, sid, exact_invoice_pdf, baseline_po_json)
    assert respond.status_code == 200
    usage = client.get("/api/v1/usage", params={"session_id": sid}).json()
    assert usage["successful_validations"] == 1
    assert usage["remaining_validations"] == 9


def test_usage_via_header_works(client):
    sid = session()
    response = client.get("/api/v1/usage", headers={"X-Session-ID": sid})
    assert response.status_code == 200
    assert response.json()["daily_limit"] == 10


def test_po_as_csv(client, exact_invoice_pdf, baseline_po_csv):
    payload = analyze(client, session(), exact_invoice_pdf, baseline_po_csv, po_filename="po.csv")
    assert payload.status_code == 200
    assert payload.json()["status"] == "MATCHED"


def test_po_csv_optional_header_fields(client, exact_invoice_pdf, baseline_po_csv):
    payload = analyze(client, session(), exact_invoice_pdf, baseline_po_csv, po_filename="po.csv")
    body = payload.json()
    assert payload.status_code == 200
    assert body["po_number"] == "PO-2024-1001"
    assert body["po_currency"] == "INR"


def test_po_csv_without_optional_header_fields_stays_null(client, exact_invoice_pdf, baseline_po_csv):
    lines = [line.split(",")[2:] for line in baseline_po_csv.strip().splitlines()]
    bare = "\n".join(",".join(fields) for fields in lines) + "\n"
    payload = analyze(client, session(), exact_invoice_pdf, bare, po_filename="po.csv")
    body = payload.json()
    assert payload.status_code == 200
    assert body["po_number"] is None
    assert body["po_currency"] is None


def test_failed_infra_does_not_consume_allowance(
    client, app_env, monkeypatch, baseline_po_json, exact_invoice_pdf
):
    from app import api as api_module

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(api_module, "run_analysis", explode)
    sid = session()
    response = analyze(client, sid, exact_invoice_pdf, baseline_po_json)
    assert response.status_code == 500

    usage = client.get("/api/v1/usage", params={"session_id": sid}).json()
    assert usage["successful_validations"] == 0
    assert usage["remaining_validations"] == 10  # reservation released
