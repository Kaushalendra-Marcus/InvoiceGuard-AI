#!/usr/bin/env python
"""Prefetch the fastembed embedding model into a build-time cache.

Run during container image builds (see render.yaml / Render service settings).
When ``INVOICEGUARD_EMBEDDING_CACHE`` points at a path inside the build
context (e.g. ``model_cache``), the downloaded model persists into the
deployed image so runtime cold starts don't re-download ~90 MB.

Failure is non-fatal: the app degrades to fuzzy matching and will retry the
download at runtime.
"""

from __future__ import annotations

import os
import sys
import time


def main() -> int:
    try:
        from fastembed import TextEmbedding  # heavy optional dependency
    except ImportError as exc:
        print(f"fastembed not importable ({exc}); skipping model prefetch", file=sys.stderr)
        return 0

    model_name = os.environ.get("INVOICEGUARD_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    cache_dir = os.environ.get("INVOICEGUARD_EMBEDDING_CACHE") or None

    started = time.monotonic()
    try:
        model_instance = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        # Force the weights through ONNX once so any first-call issues surface now.
        next(model_instance.embed(["warmup"]), None)
    except Exception as exc:  # noqa: BLE001 - report any provider/HF failure
        print(f"WARNING: model prefetch failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 0

    location = cache_dir or "~/.cache/fastembed"
    print(f"model_cache_prefetch model={model_name} cache={location} took={time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
