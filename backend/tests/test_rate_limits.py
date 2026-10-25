"""IP-based rate limiting: 8 analyses/minute and 100 lifetime per client IP.

Covers the API 429 responses (minute and lifetime scopes), the fact that
rotating anonymous session ids does NOT bypass the cap, per-IP isolation via
X-Forwarded-For, and (unit level) minute-bucket rollover plus the property
that denied attempts never consume lifetime allowance.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.matching import SemanticMatcher, set_semantic_matcher
from app.rate_limiter import RateLimiter

# Production defaults mirrored explicitly so the tests document the contract.
PER_MINUTE = 8
LIFETIME = 100


@pytest.fixture(autouse=True)
def semantic_disabled_by_default():
    set_semantic_matcher(SemanticMatcher(enabled=False))
    yield
    set_semantic_matcher(None)


@pytest.fixture()
def rate_client(app_env, monkeypatch):
    """Test client with the real production caps (8/minute, 100 lifetime).

    ``app_env`` first raises the caps to effectively unlimited; this fixture
    restores production values *before* the TestClient startup triggers the
    lifespan, which is where the RateLimiter reads its configuration.
    """
    from app.config import settings as base_settings

    monkeypatch.setattr(base_settings, "rate_limit_per_minute", PER_MINUTE)
    monkeypatch.setattr(base_settings, "rate_limit_lifetime", LIFETIME)
    app, limiter, engine = app_env
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def session() -> str:
    return "anon-" + uuid.uuid4().hex[:12]


def analyze(client, sid: str, invoice_pdf: bytes, po_payload: str | None = None,
            po_filename: str = "po.json", headers: dict | None = None):
    data = {"session_id": sid}
    files = {"invoice": ("invoice.pdf", invoice_pdf, "application/pdf")}
    if po_payload is not None:
        files["po_file"] = (po_filename, po_payload, "application/octet-stream")
    return client.post("/api/v1/analyze", data=data, files=files, headers=headers or {})


# ---------------------------------------------------------------------------
# API-level behavior
# ---------------------------------------------------------------------------
def test_ninth_analysis_within_a_minute_is_rejected(rate_client, exact_invoice_pdf,
                                                    baseline_po_json):
    sid = session()
    responses = [analyze(rate_client, sid, exact_invoice_pdf, baseline_po_json)
                 for _ in range(PER_MINUTE)]
    assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]

    ninth = analyze(rate_client, sid, exact_invoice_pdf, baseline_po_json)
    assert ninth.status_code == 429
    detail = ninth.json()["detail"]
    assert detail["code"] == "RATE_LIMIT_MINUTE"
    assert "per minute" in detail["message"].lower()
    assert detail["rate"]["minute_requests"] == PER_MINUTE
    assert detail["rate"]["lifetime_requests"] == PER_MINUTE
    retry_after = ninth.headers.get("Retry-After")
    assert retry_after is not None and 1 <= int(retry_after) <= 60


def test_rotating_session_ids_does_not_bypass_the_cap(rate_client, exact_invoice_pdf,
                                                      baseline_po_json):
    """The IP limiter exists precisely because session ids are client-supplied."""
    for _ in range(PER_MINUTE):
        assert analyze(rate_client, session(), exact_invoice_pdf,
                       baseline_po_json).status_code == 200
    rotated = analyze(rate_client, session(), exact_invoice_pdf, baseline_po_json)
    assert rotated.status_code == 429
    assert rotated.json()["detail"]["code"] == "RATE_LIMIT_MINUTE"


def test_other_ips_keep_their_own_allowance(rate_client, exact_invoice_pdf, baseline_po_json):
    headers_a = {"X-Forwarded-For": "203.0.113.10"}
    headers_b = {"X-Forwarded-For": "203.0.113.11"}
    for _ in range(PER_MINUTE):
        assert analyze(rate_client, session(), exact_invoice_pdf, baseline_po_json,
                       headers=headers_a).status_code == 200
    blocked = analyze(rate_client, session(), exact_invoice_pdf, baseline_po_json,
                      headers=headers_a)
    assert blocked.status_code == 429
    fresh_ip = analyze(rate_client, session(), exact_invoice_pdf, baseline_po_json,
                       headers=headers_b)
    assert fresh_ip.status_code == 200


@pytest.fixture()
def lifetime_client(app_env, monkeypatch):
    """Client with a small lifetime cap so the exhaustion path is reachable."""
    from app.config import settings as base_settings

    monkeypatch.setattr(base_settings, "rate_limit_per_minute", 1000)
    monkeypatch.setattr(base_settings, "rate_limit_lifetime", 5)
    app, limiter, engine = app_env
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_lifetime_limit_blocks_dense_but_slow_clients(lifetime_client, exact_invoice_pdf,
                                                      baseline_po_json):
    sid = session()
    for _ in range(5):
        assert analyze(lifetime_client, sid, exact_invoice_pdf, baseline_po_json).status_code == 200

    sixth = analyze(lifetime_client, session(), exact_invoice_pdf, baseline_po_json)
    assert sixth.status_code == 429
    detail = sixth.json()["detail"]
    assert detail["code"] == "RATE_LIMIT_LIFETIME"
    assert "lifetime" in detail["message"].lower()
    assert detail["rate"]["lifetime_requests"] == 5
    assert detail["rate"]["minute_requests"] == 5


def test_regular_client_never_hits_the_new_caps(client, exact_invoice_pdf, baseline_po_json):
    """Sanity: default test env leaves enough headroom for the daily limit tests."""
    sid = session()
    for _ in range(5):
        response = analyze(client, sid, exact_invoice_pdf, baseline_po_json)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Unit-level behavior (fake clock)
# ---------------------------------------------------------------------------
def test_minute_window_resets_and_denials_are_not_billed(app_env):
    app, limiter, engine = app_env
    now = {"t": datetime(2026, 10, 25, 12, 0, 30, tzinfo=timezone.utc)}
    rl = RateLimiter(engine, per_minute=3, lifetime=100, clock=lambda: now["t"])

    for expected_minute, expected_lifetime in [(1, 1), (2, 2), (3, 3)]:
        decision = rl.hit("9.9.9.9")
        assert decision.allowed
        assert decision.minute_count == expected_minute
        assert decision.lifetime_count == expected_lifetime

    denied = rl.hit("9.9.9.9")
    assert not denied.allowed and denied.scope == "minute"
    # Denied attempts do not consume lifetime allowance.
    assert denied.lifetime_count == 3

    # Rollover into the next minute (also crossing the hour boundary).
    now["t"] = datetime(2026, 10, 25, 13, 0, 10, tzinfo=timezone.utc)
    fresh = rl.hit("9.9.9.9")
    assert fresh.allowed
    assert fresh.minute_count == 1
    assert fresh.lifetime_count == 4
    assert 1 <= fresh.seconds_until_minute_reset <= 60


def test_lifetime_scope_only_changes_when_lifetime_is_exhausted(app_env):
    app, limiter, engine = app_env
    now = {"t": datetime(2026, 10, 25, 9, 0, 0, tzinfo=timezone.utc)}
    rl = RateLimiter(engine, per_minute=2, lifetime=3, clock=lambda: now["t"])

    # Burn minute allowance first: deny should be minute-scoped.
    rl.hit("1.1.1.1")
    rl.hit("1.1.1.1")
    minute_denial = rl.hit("1.1.1.1")
    assert minute_denial.scope == "minute"

    # Advance a minute, use up the lifetime: deny becomes lifetime-scoped.
    now["t"] = datetime(2026, 10, 25, 9, 1, 30, tzinfo=timezone.utc)
    assert rl.hit("1.1.1.1").allowed  # lifetime 3/3

    now["t"] = datetime(2026, 10, 25, 9, 2, 45, tzinfo=timezone.utc)
    lifetime_denial = rl.hit("1.1.1.1")
    assert not lifetime_denial.allowed
    assert lifetime_denial.scope == "lifetime"
