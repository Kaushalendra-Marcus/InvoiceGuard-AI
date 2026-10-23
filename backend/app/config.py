"""Application configuration.

All values can be overridden with environment variables prefixed with
``INVOICEGUARD_`` (e.g. ``INVOICEGUARD_DAILY_LIMIT=5``) or via a local
``.env`` file.
"""

from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INVOICEGUARD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Usage limits -------------------------------------------------
    daily_limit: int = 10
    timezone_name: str = "Asia/Kolkata"
    # A reserved-but-failed/abandoned validation older than this is ignored
    # when counting the allowance (protects against leaked reservations).
    reservation_ttl_seconds: int = 900

    # --- Storage ------------------------------------------------------
    db_path: Path = Path("data/invoiceguard.db")

    # --- Document processing ------------------------------------------
    max_upload_bytes: int = 10 * 1024 * 1024  # 10 MB
    allowed_invoice_extensions: tuple[str, ...] = ("pdf", "png", "jpg", "jpeg", "webp")
    ocr_language: str = "eng"
    default_currency: str = "INR"

    # --- Matching ------------------------------------------------------
    fuzzy_threshold: float = 85.0            # RapidFuzz token similarity 0-100
    semantic_threshold: float = 0.72         # cosine similarity 0..1
    ambiguous_score_gap: float = 0.05        # top-2 candidates within this gap -> ambiguous
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # Optional ONNX model cache directory: in container deployments this is
    # prefetched during the image build so every cold start is instant.
    embedding_cache: str | None = None

    # --- Financial tolerances ------------------------------------------
    # Price differences within this percentage (or the absolute floor below)
    # are considered acceptable rounding differences.
    price_tolerance_pct: Decimal = Decimal("0.005")   # 0.5% as a fraction
    absolute_price_tolerance: Decimal = Decimal("0.01")
    quantity_tolerance: Decimal = Decimal("0.0001")   # numeric noise only
    # Invoice < PO quantity = likely a partial shipment, not a discrepancy.
    shortfall_as_discrepancy: bool = False

    # --- Anonymous session validation ----------------------------------
    session_id_pattern: str = r"^[A-Za-z0-9_-]{8,64}$"


settings = Settings()
