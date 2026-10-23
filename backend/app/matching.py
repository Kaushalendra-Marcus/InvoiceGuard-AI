"""Stages 2-4: intelligent line-item matching.

The matcher combines, in order of evidential strength:

1. SKU matching       - strong identifier, contradiction-checked
2. Exact matching     - on normalized descriptions
3. Fuzzy matching     - RapidFuzz token similarity
4. Semantic matching  - sentence embeddings (fastembed / ONNX, no torch)

Semantic similarity is treated as *candidate evidence*, never as proof:
units, SKUs and extracted product specs are contradiction-checked before a
match is accepted, and near-tie candidates are marked ambiguous so the final
decision can route the case to human review instead of forcing a match.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from rapidfuzz import fuzz

from .config import settings
from .normalizer import (
    extract_specs,
    normalize_description,
    normalize_sku,
    specs_conflict,
    units_compatible,
)
from .schemas import ConfidenceCategory, InvoiceItemOut, MatchMethod, POItemOut

# Relative similarity below which a SKU-anchored pair counts as "unrelated
# descriptions" (SKU matches, descriptions contradict each other). bge-small
# cosines run high even for related-but-different products (~0.64), while
# true rewordings score >= ~0.8, so the semantic fallback threshold is a
# 'close-to-identical' bar with fuzzy(rapidfuzz) doing the discriminating.
_SKU_DESC_DIVERGENCE_FUZZY = 40.0
_SKU_DESC_DIVERGENCE_SEMANTIC = 0.70


# ---------------------------------------------------------------------------
# Semantic provider (Stage 4)
# ---------------------------------------------------------------------------
class SemanticMatcher:
    """Lazy, fail-soft sentence embedding provider.

    Uses fastembed (ONNX runtime, no torch). If the model cannot be
    downloaded or loaded, the matcher degrades to fuzzy matching and the
    report declares the semantic stage unavailable instead of pretending.
    """

    def __init__(self, model_name: Optional[str] = None, enabled: bool = True):
        self.model_name = model_name or settings.embedding_model
        self.enabled = enabled
        self.unavailable_reason: Optional[str] = None
        self._model = None
        self._load_attempted = False
        self._vectors: dict[str, list[float]] = {}

    @property
    def available(self) -> bool:
        if not self.enabled:
            return False
        self._ensure_loaded()
        return self._model is not None

    def _ensure_loaded(self) -> None:
        if self._load_attempted and self.unavailable_reason:
            return
        self._load_attempted = True
        if self._model is not None:
            return
        try:
            from fastembed import TextEmbedding  # imported lazily: heavy optional dep

            self._model = TextEmbedding(model_name=self.model_name)
        except Exception as exc:  # pragma: no cover - depends on runtime env
            self._model = None
            self.unavailable_reason = f"semantic model unavailable ({type(exc).__name__})"

    def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """Embed (and cache) a batch of texts; None when unavailable."""
        if not self.available:
            return None
        missing = [t for t in texts if t not in self._vectors]
        if missing:
            try:
                vectors = list(
                    self._model.embed(missing)  # type: ignore[union-attr]
                )
                for text, vector in zip(missing, vectors):
                    self._vectors[text] = [float(x) for x in vector]
            except Exception as exc:  # pragma: no cover - runtime dependent
                self.unavailable_reason = f"embedding failed ({type(exc).__name__})"
                self._model = None
                return None
        return [self._vectors[t] for t in texts]

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        # Guard against float overshoot just past 1.0, which the report
        # schema (semantic_score <= 1) would reject with a 500.
        return max(-1.0, min(1.0, dot / (na * nb)))


_module_matcher: Optional[SemanticMatcher] = None


def get_semantic_matcher() -> SemanticMatcher:
    """Process-wide matcher so the ONNX model loads once per run."""
    global _module_matcher
    if _module_matcher is None:
        _module_matcher = SemanticMatcher(enabled=True)
    return _module_matcher


def set_semantic_matcher(matcher: Optional[SemanticMatcher]) -> None:
    global _module_matcher
    _module_matcher = matcher


# ---------------------------------------------------------------------------
# Candidate model
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    po_index: int
    kind: str                    # "SKU" | "EXACT" | "SEMANTIC" | "FUZZY"
    score: float                 # normalized 0..1 for ranking
    fuzzy_score: Optional[float] = None   # 0..100
    semantic_score: Optional[float] = None  # 0..1
    sku_match: bool = False


@dataclass
class MatchOutcome:
    invoice_index: int
    invoice_item: InvoiceItemOut
    po_index: Optional[int] = None
    po_item: Optional[POItemOut] = None
    matched: bool = False
    method: MatchMethod = MatchMethod.NONE
    fuzzy_score: Optional[float] = None
    semantic_score: Optional[float] = None
    sku_match: bool = False
    ambiguous: bool = False
    competitor_po_index: Optional[int] = None
    competitor_description: Optional[str] = None
    competitor_score: Optional[float] = None
    confidence: ConfidenceCategory = ConfidenceCategory.LOW
    unit_compatible: Optional[bool] = None
    contradictions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


_KIND_RANK = {"SKU": 4, "EXACT": 3, "SEMANTIC": 2, "FUZZY": 1}


def _fuzzy_score(a_norm: str, b_norm: str) -> float:
    return max(fuzz.token_sort_ratio(a_norm, b_norm), fuzz.ratio(a_norm, b_norm))


def _confidence_for(outcome: MatchOutcome) -> ConfidenceCategory:
    if outcome.contradictions:
        return ConfidenceCategory.LOW
    if outcome.method in (MatchMethod.SKU, MatchMethod.EXACT):
        return ConfidenceCategory.HIGH
    if outcome.unit_compatible is False:
        return ConfidenceCategory.LOW
    if outcome.ambiguous:
        return ConfidenceCategory.LOW
    if (outcome.semantic_score is not None and outcome.semantic_score >= 0.82) or (
        outcome.fuzzy_score is not None and outcome.fuzzy_score >= 93.0
    ):
        return ConfidenceCategory.HIGH
    return ConfidenceCategory.MEDIUM


def match_invoice_to_po(
    invoice_items: list[InvoiceItemOut],
    po_items: list[POItemOut],
    semantic: SemanticMatcher | None = None,
    consumed_promised_qty: Optional[list[Decimal]] = None,
    *,
    fuzzy_threshold: Optional[float] = None,
    semantic_threshold: Optional[float] = None,
    ambiguous_gap: Optional[float] = None,
) -> list[MatchOutcome]:
    """Match every invoice line to at most one PO line.

    ``consumed_promised_qty[j]`` is the PO-line quantity already covered by
    earlier invoice lines; description-based candidates skip fully consumed
    PO lines (SKU-anchored duplicates are still allowed through so the
    financial validator can flag over-invoicing).
    """
    fuzzy_threshold = settings.fuzzy_threshold if fuzzy_threshold is None else fuzzy_threshold
    semantic_threshold = settings.semantic_threshold if semantic_threshold is None else semantic_threshold
    ambiguous_gap = settings.ambiguous_score_gap if ambiguous_gap is None else ambiguous_gap

    semantic = semantic or get_semantic_matcher()

    inv_n = [normalize_description(it.description) for it in invoice_items]
    po_n = [normalize_description(it.description) for it in po_items]
    inv_skus = [normalize_sku(it.sku) for it in invoice_items]
    po_skus = [normalize_sku(it.sku) for it in po_items]
    inv_specs = [extract_specs(it.description) for it in invoice_items]
    po_specs = [extract_specs(it.description) for it in po_items]

    # Batch-embed once per analysis for efficiency.
    inv_vecs = po_vecs = None
    if semantic.enabled and (inv_n or po_n):
        inv_vecs = semantic.embed_batch(inv_n)
        po_vecs = semantic.embed_batch(po_n) or inv_vecs

    def semantic_pair(i: int, j: int) -> Optional[float]:
        try:
            a, b = inv_vecs[i], po_vecs[j]
        except (TypeError, IndexError):
            return None
        if a is None or b is None:
            return None
        return SemanticMatcher.cosine(a, b)

    consumed = consumed_promised_qty or [Decimal("0")] * len(po_items)

    outcomes: list[MatchOutcome] = []
    for i, inv_item in enumerate(invoice_items):
        outcome = MatchOutcome(invoice_index=i, invoice_item=inv_item)
        candidates: list[Candidate] = []
        best_below: Optional[Candidate] = None
        for j, po_item in enumerate(po_items):
            remaining = Decimal("0")
            if 0 <= j < len(consumed):
                remaining = max(Decimal("0"), po_item.quantity - consumed[j])
            has_capacity = remaining > 0
            sku_both = (inv_skus[i] is not None) and (po_skus[j] is not None)
            sku_equal = bool(sku_both) and inv_skus[i] == po_skus[j]
            exact = inv_n[i] != "" and inv_n[i] == po_n[j]
            fz = _fuzzy_score(inv_n[i], po_n[j])
            sm = semantic_pair(i, j)

            # keep the strongest similarity as "near miss" evidence
            near_miss = max(fz / 100.0, sm if sm is not None else 0.0)
            pair = Candidate(po_index=j, kind="FUZZY", score=near_miss, fuzzy_score=fz, semantic_score=sm)
            if best_below is None or pair.score > best_below.score:
                best_below = pair

            if sku_equal:
                candidates.append(
                    Candidate(j, "SKU", score=1.0, fuzzy_score=fz, semantic_score=sm, sku_match=True)
                )
            if exact:
                candidates.append(
                    Candidate(j, "EXACT", score=max(0.99, pair.score), fuzzy_score=fz, semantic_score=sm)
                )
            if sm is not None and sm >= semantic_threshold and has_capacity:
                candidates.append(
                    Candidate(j, "SEMANTIC", score=sm, fuzzy_score=fz, semantic_score=sm)
                )
            if fz >= fuzzy_threshold and has_capacity:
                candidates.append(
                    Candidate(j, "FUZZY", score=fz / 100.0, fuzzy_score=fz, semantic_score=sm)
                )

        outcome.competitor_score = best_below.score if best_below else None

        if not candidates:
            outcome.method = MatchMethod.NONE
            outcome.notes.append(
                "No PO line reached the matching thresholds. "
                f"Closest candidate similarity: {_fmt_score(best_below.score if best_below else None)}"
            )
            outcomes.append(outcome)
            continue

        candidates.sort(key=lambda c: (_KIND_RANK[c.kind], c.score), reverse=True)
        top = candidates[0]
        # Ambiguity must compare *distinct PO lines* (else the same line's
        # fuzzy + semantic candidates could falsely read as a tie).
        second = next((c for c in candidates if c.po_index != top.po_index), None)

        # Ambiguity: description-only evidence with a near-tie runner-up, or
        # two different PO lines claiming the same SKU without an exact
        # description to break the tie.
        ambiguous = False
        if top.kind == "SKU":
            other_sku_candidates = [c for c in candidates if c.kind == "SKU" and c.po_index != top.po_index]
            exact_candidates = [c for c in candidates if c.kind == "EXACT" and c.po_index == top.po_index]
            if other_sku_candidates and not exact_candidates:
                ambiguous = True
                second = next(
                    (c for c in candidates if c.po_index == other_sku_candidates[0].po_index), second
                )
        elif second is not None:
            if abs(top.score - second.score) <= ambiguous_gap:
                if not (top.kind == "EXACT" and second.kind != "EXACT"):
                    ambiguous = True

        outcome.matched = True
        outcome.po_index = top.po_index
        outcome.po_item = po_items[top.po_index]
        outcome.ambiguous = ambiguous
        outcome.sku_match = top.sku_match
        outcome.fuzzy_score = top.fuzzy_score
        outcome.semantic_score = top.semantic_score
        outcome.method = {
            "SKU": MatchMethod.SKU,
            "EXACT": MatchMethod.EXACT,
            "SEMANTIC": MatchMethod.SEMANTIC,
            "FUZZY": MatchMethod.FUZZY,
        }[top.kind]

        if ambiguous and second is not None:
            outcome.competitor_po_index = second.po_index
            outcome.competitor_description = po_items[second.po_index].description
            outcome.competitor_score = second.score
            outcome.notes.append(
                "Two PO lines score nearly the same for this invoice line "
                f"({top.score:.2f} vs {second.score:.2f}); an exact match cannot be established."
            )

        # ------- contradiction checks (spec: strong identifiers must be checked) -------
        unit_cmp = units_compatible(inv_item.quantity_unit, po_items[top.po_index].quantity_unit)
        outcome.unit_compatible = unit_cmp
        if unit_cmp is False:
            outcome.notes.append(
                "Quantity units differ between invoice and PO "
                f"({inv_item.quantity_unit!r} vs {po_items[top.po_index].quantity_unit!r}); "
                "quantity and monetary comparisons are suppressed."
            )
        elif unit_cmp is None:
            outcome.notes.append("Quantity units are not stated on one side; compatibility assumed.")

        conflicts = specs_conflict(inv_specs[i], po_specs[top.po_index])
        outcome.contradictions.extend(conflicts)

        if (
            inv_skus[i] is not None
            and po_skus[top.po_index] is not None
            and inv_skus[i] != po_skus[top.po_index]
        ):
            outcome.contradictions.append(
                "SKU on the invoice does not match the SKU on the matched PO line "
                f"({inv_item.sku!r} vs {po_items[top.po_index].sku!r})."
            )
        if top.sku_match and top.fuzzy_score is not None:
            too_divergent = (
                (top.semantic_score is None and top.fuzzy_score < _SKU_DESC_DIVERGENCE_FUZZY)
                or (
                    top.semantic_score is not None
                    and top.semantic_score < _SKU_DESC_DIVERGENCE_SEMANTIC
                    and top.fuzzy_score < _SKU_DESC_DIVERGENCE_FUZZY
                )
            )
            if too_divergent:
                outcome.contradictions.append(
                    "SKU matches but the descriptions are barely similar; "
                    "the invoice SKU may belong to a different product."
                )

        outcome.confidence = _confidence_for(outcome)
        outcomes.append(outcome)
    return outcomes


def _fmt_score(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}"
