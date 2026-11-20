"""Position-independent diagnostic signatures and the baseline/post-edit multiset diff."""
from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

from .models import DiagnosticItem

SEVERITY_NAMES = {1: "error", 2: "warning", 3: "information", 4: "hint"}


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def _code(raw: dict[str, Any]) -> str | None:
    c = raw.get("code")
    return None if c is None else str(c)


def signatures(raw: dict[str, Any], text_lines: list[str]) -> tuple[str, str]:
    """Return (strict, loose) signatures for a raw LSP diagnostic against `text_lines`."""
    line = raw["range"]["start"]["line"]
    n = len(text_lines)
    L = min(max(line, 0), n - 1) if n else 0
    window = "\n".join(s.strip() for s in text_lines[max(0, L - 1): L + 2]) if n else ""
    line_txt = text_lines[L].strip() if n else ""
    msg = " ".join(str(raw.get("message", "")).split())
    sev = raw.get("severity", 1)
    base = f"{_code(raw)}|{sev}|{msg}|"
    return base + _h(window), base + _h(line_txt)


def to_items(rel_path: str, raws: list[dict[str, Any]], text: str) -> list[tuple[DiagnosticItem, str]]:
    """Convert raw diagnostics to (item, loose_signature)."""
    lines = text.split("\n")
    out = []
    for r in raws:
        strict, loose = signatures(r, lines)
        start = r["range"]["start"]
        out.append((DiagnosticItem(
            rel_path=rel_path, line=start["line"], character=start["character"],
            severity=r.get("severity", 1), code=_code(r),
            message=str(r.get("message", "")), signature=strict), loose))
    return out


def diff_file(
    rel_path: str,
    baseline_raw: list[dict[str, Any]], baseline_text: str,
    post_raw: list[dict[str, Any]], post_text: str,
    block_on: list[str], loose_fallback: bool = True,
) -> tuple[list[DiagnosticItem], list[DiagnosticItem]]:
    """Per-file net-new diff. Returns (blocking, warnings)."""
    strict_c: Counter[str] = Counter()
    loose_c: Counter[str] = Counter()
    for item, loose in to_items(rel_path, baseline_raw, baseline_text):
        strict_c[item.signature] += 1
        loose_c[loose] += 1
    blocking: list[DiagnosticItem] = []
    warnings: list[DiagnosticItem] = []
    for item, loose in to_items(rel_path, post_raw, post_text):
        if strict_c[item.signature] > 0:
            strict_c[item.signature] -= 1
            if loose_c[loose] > 0:  # the same baseline diagnostic must not be matched twice
                loose_c[loose] -= 1
            continue
        if loose_fallback and loose_c[loose] > 0:
            loose_c[loose] -= 1
            continue
        (blocking if SEVERITY_NAMES.get(item.severity, "error") in block_on else warnings).append(item)
    return blocking, warnings
