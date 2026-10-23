"""Stage 1: text/value normalization utilities.

Everything here is deterministic and side-effect free. Original values are
preserved by callers; these helpers produce normalized *representations* used
for matching and comparison only.
"""

import re
import unicodedata
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------
CURRENCY_SYMBOLS = {
    "₹": "INR",
    "rs.": "INR",
    "rs ": "INR",
    "inr": "INR",
    "rupees": "INR",
    "$": "USD",
    "usd": "USD",
    "us ": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
}

CURRENCY_RE = re.compile(
    r"(₹|\brs\.?\b|\binr\b|\brupee[s]?\b|\$|\busd\b|€|\beur\b|£|\bgbp\b)", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Quantity units -> canonical family + note that numeric magnitudes may differ
# ---------------------------------------------------------------------------
UNIT_ALIASES: dict[str, str] = {
    # pieces / count units
    "pcs": "PIECES",
    "pcs.": "PIECES",
    "pc": "PIECES",
    "piece": "PIECES",
    "pieces": "PIECES",
    "nos": "PIECES",
    "no.": "PIECES",
    "no": "PIECES",
    "numbers": "PIECES",
    "ea": "PIECES",
    "each": "PIECES",
    "unit": "PIECES",
    "units": "PIECES",
    "qty": "PIECES",
    "box": "BOX",
    "boxes": "BOX",
    "carton": "BOX",
    "cartons": "BOX",
    # lengths
    "m": "METERS",
    "mtr": "METERS",
    "mtrs": "METERS",
    "meter": "METERS",
    "meters": "METERS",
    "metre": "METERS",
    "metres": "METERS",
    "cm": "CENTIMETERS",
    "centimeter": "CENTIMETERS",
    "centimeters": "CENTIMETERS",
    "centimetre": "CENTIMETERS",
    "centimetres": "CENTIMETERS",
    "mm": "MILLIMETERS",
    "millimeter": "MILLIMETERS",
    "millimeters": "MILLIMETERS",
    "km": "KILOMETERS",
    "kilometer": "KILOMETERS",
    "kilometers": "KILOMETERS",
    "in": "INCHES",
    "inch": "INCHES",
    "inches": "INCHES",
    "ft": "FEET",
    "feet": "FEET",
    "foot": "FEET",
    "yd": "YARDS",
    "yard": "YARDS",
    "yards": "YARDS",
    # weights
    "g": "GRAMS",
    "gm": "GRAMS",
    "gms": "GRAMS",
    "gram": "GRAMS",
    "grams": "GRAMS",
    "kg": "KILOGRAMS",
    "kgs": "KILOGRAMS",
    "kilogram": "KILOGRAMS",
    "kilograms": "KILOGRAMS",
    "mg": "MILLIGRAMS",
    # volumes
    "ml": "MILLILITERS",
    "l": "LITERS",
    "ltr": "LITERS",
    "litre": "LITERS",
    "litres": "LITERS",
    "liter": "LITERS",
    "liters": "LITERS",
}

# Conversion factor to a common base unit *within* each family.
_LENGTH_FACTORS: dict[str, float] = {
    "MILLIMETERS": 1.0, "CENTIMETERS": 10.0, "METERS": 1000.0, "KILOMETERS": 1_000_000.0,
    "INCHES": 25.4, "FEET": 304.8, "YARDS": 914.4,
}
_WEIGHT_FACTORS: dict[str, float] = {
    "MILLIGRAMS": 1.0, "GRAMS": 1000.0, "KILOGRAMS": 1_000_000.0,
}
_VOLUME_FACTORS: dict[str, float] = {"MILLILITERS": 1.0, "LITERS": 1000.0}

_LENGTH_UNIT_FAMILIES = set(_LENGTH_FACTORS)
_WEIGHT_UNIT_FAMILIES = set(_WEIGHT_FACTORS)
_VOLUME_UNIT_FAMILIES = set(_VOLUME_FACTORS)

def canonical_unit(raw: str | None) -> str | None:
    """Map a free-text unit like 'No(s).', 'Pcs', 'metre' to a canonical family."""
    if not raw:
        return None
    token = raw.strip().lower().rstrip(".")
    return UNIT_ALIASES.get(token)


def units_compatible(a: str | None, b: str | None) -> bool | None:
    """Compare canonical unit families.

    Returns None when either side is unknown (nothing to compare),
    True when both map to the same family, False when they conflict.
    Count-style fuzzy units (pcs/box) never mix with physical units.
    """
    fa, fb = canonical_unit(a), canonical_unit(b)
    if fa is None or fb is None:
        return None
    if fa in _LENGTH_UNIT_FAMILIES and fb in _LENGTH_UNIT_FAMILIES:
        return True
    if fa in _WEIGHT_UNIT_FAMILIES and fb in _WEIGHT_UNIT_FAMILIES:
        return True
    if fa in _VOLUME_UNIT_FAMILIES and fb in _VOLUME_UNIT_FAMILIES:
        return True
    return fa == fb


# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------
_WS_RE = re.compile(r"\s+")
_KEEP_CHARS_RE = re.compile(r"[^a-z0-9%+\-/\s]")
_UNIT_IN_TEXT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(millimeters?|millimetres?|centimeters?|centimetres?|meters?|metres?|"
    r"millimeter|mm|cm|m\.tr|mtrs?|meters|meter|km|kilometers?|inches|inch|in|feet|foot|ft|"
    r"grams?|gms?|kg?s?|kilograms?)\b\.?",
    re.IGNORECASE,
)


def normalize_text(raw: str | None) -> str:
    """Fast, aggressive normalization: NFKC, lowercase, drop punctuation.

    Keeps digits, letters, %, +, -, / because they carry product meaning
    (sizes, voltages, model numbers such as 'USB-C 3.2 Gen-1').
    """
    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", raw).lower()
    text = text.replace("–", "-").replace("—", "-")
    text = _KEEP_CHARS_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def normalize_description(raw: str | None) -> str:
    """Description normalization that also standardizes unit spellings.

    'USB-C Cable, 1 Meter' -> 'usb-c cable 1 m'
    'Type-C Charging Cable, 100cm' -> 'type-c charging cable 100 cm'
    """
    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", raw).lower()

    def _canon(match: re.Match) -> str:
        value, unit = match.group(1), match.group(2)
        unit_token = unit.strip().lower().rstrip(".")
        family = UNIT_ALIASES.get(unit_token)
        if family is None:
            return f"{value} {unit_token}"
        # Use the shortest human-canonical symbol per family.
        symbol = {
            "MILLIMETERS": "mm", "CENTIMETERS": "cm", "METERS": "m", "KILOMETERS": "km",
            "INCHES": "in", "FEET": "ft", "YARDS": "yd",
            "GRAMS": "g", "KILOGRAMS": "kg", "MILLIGRAMS": "mg",
            "MILLILITERS": "ml", "LITERS": "l",
            "PIECES": "pcs", "BOX": "box",
        }.get(family, unit_token)
        return f"{value} {symbol}"

    text = _UNIT_IN_TEXT_RE.sub(_canon, text)
    text = _KEEP_CHARS_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def normalize_sku(raw: str | None) -> str | None:
    """SKU comparison key: alphanumerics only, uppercased."""
    if not raw:
        return None
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    return key.upper() or None


# ---------------------------------------------------------------------------
# Numbers / amounts
# ---------------------------------------------------------------------------
_NUM_CLEAN_RE = re.compile(r"[^\d.\-]")


def parse_decimal(raw: str | float | int | Decimal | None) -> Decimal | None:
    """Parse a loosely formatted number into Decimal.

    Understands currency symbols, stray spaces and both US (1,234.56) and
    Indian (1,23,456.78) digit grouping. Returns None when the value is
    unusable - callers must never invent a substitute value.
    """
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float)):
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            return None
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    if not text or text in {"-", ".", "-."}:
        return None

    stripped = CURRENCY_RE.sub(" ", text)
    if "," in stripped:
        head, _, tail = stripped.rpartition(",")
        groups = head.split(",")
        # Only ungroup when the commas look like grouping separators, both
        # Indian ('1,50,000' -> 150000) and US ('1,234,567' -> 1234567).
        if all(g.isdigit() for g in groups) and all(g and len(g) <= 3 for g in groups) and tail.isdigit() and len(tail) == 3:
            stripped = "".join(groups) + tail
    cleaned = _NUM_CLEAN_RE.sub("", stripped)
    if cleaned in {"", "-", ".", "-."}:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


_CURRENCY_PRIORITY: list[tuple[str, str]] = [
    ("₹", "INR"), (r"\brs\.?\b", "INR"), (r"\binr\b", "INR"), (r"rupee[s]?", "INR"),
    (r"\$", "USD"), (r"\busd\b", "USD"),
    ("€", "EUR"), (r"\beur\b", "EUR"),
    ("£", "GBP"), (r"\bgbp\b", "GBP"),
]


def detect_currency(*texts: str | None) -> str | None:
    """Pick the first recognizable currency token across the given texts.

    Symbols (₹/$/...) take priority over ISO words, then earlier arguments
    win, so callers pass higher-confidence sources first.
    """
    for text in texts:
        if not text:
            continue
        haystack = unicodedata.normalize("NFKC", text).lower()
        for pattern, code in _CURRENCY_PRIORITY:
            if re.search(pattern, haystack):
                return code
    return None


def format_money(value: Decimal, currency: str | None = None) -> str:
    """Human display for monetary values, e.g. '₹3,000.00'."""
    q = value.quantize(Decimal("0.01"))
    symbol = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}.get((currency or "").upper(), "")
    return f"{symbol}{q:,.2f}"


# ---------------------------------------------------------------------------
# Spec extraction (for contradiction checks between matched descriptions)
# ---------------------------------------------------------------------------
_LENGTH_SPEC_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(mm|cm|km|meters?|metres?|m|inches|inch|in|feet|foot|ft|yards?|yd|"
    r"millimeters?|millimetres?|centimeters?|centimetres?)\b\.?", re.IGNORECASE
)
_WEIGHT_SPEC_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(mg|g|gm|gms|kg|kgs|kilograms?)\b\.?", re.IGNORECASE)


def _to_canonical(value: float, unit: str, factors: dict[str, float]) -> float | None:
    family = UNIT_ALIASES.get(unit.lower())
    if family is None:
        return None
    base, table = factors[family], factors
    factor = next((f for fam, f in table.items() if fam == family), None)
    return value * factor


def extract_specs(text: str) -> dict[str, float]:
    """Pull comparable product specs out of a description.

    Length converts to millimeters, weight to milligrams. Colour words etc.
    are deliberately not extracted; 'USB-C Cable 1 Meter' yields length 1000mm.
    """
    specs: dict[str, float] = {}
    normalized = unicodedata.normalize("NFKC", text).lower()
    for value, unit in _LENGTH_SPEC_RE.findall(normalized):
        conv = _to_canonical(float(value), unit, _LENGTH_FACTORS)
        if conv is not None:
            specs.setdefault("length", conv)
    for value, unit in _WEIGHT_SPEC_RE.findall(normalized):
        conv = _to_canonical(float(value), unit, _WEIGHT_FACTORS)
        if conv is not None:
            specs.setdefault("weight", conv)
    return specs


def specs_conflict(specs_a: dict[str, float], specs_b: dict[str, float]) -> list[str]:
    """Return human-readable conflicts between two spec dicts.

    Uses a 1% relative tolerance so '1 m' vs '1000 mm' never conflicts.
    """
    conflicts: list[str] = []
    for key in ("length", "weight"):
        a, b = specs_a.get(key), specs_b.get(key)
        if a is None or b is None:
            continue
        if abs(a - b) / max(abs(a), abs(b)) > 0.01:
            pretty = {"length": "length", "weight": "weight"}[key]
            conflicts.append(
                f"Different product {pretty} stated: {a:g} vs {b:g} (canonical units)"
            )
    return conflicts
