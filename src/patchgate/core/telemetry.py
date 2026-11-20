"""JSONL trace under <repo>/.patchgate/traces/<session_id>.jsonl plus in-memory latency stats."""
from __future__ import annotations

import json
import math
import time
import uuid
from pathlib import Path
from typing import Any

EVENTS = {"session_start", "read_file", "read_denied", "batch_verified", "batch_committed",
          "circuit_open", "lsp_restart", "lsp_timeout", "error"}


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile: rank = ceil(p/100 * N), 1-based, over the sorted values."""
    if not values:
        return None
    s = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(s)))
    return s[rank - 1]


class Telemetry:
    def __init__(self, repo_root: Path, session_id: str | None = None):
        self.root = Path(repo_root).resolve()
        self.session_id = session_id or time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.path = self.root / ".patchgate" / "traces" / f"{self.session_id}.jsonl"
        self._events: list[dict[str, Any]] = []
        self._lsp_total: list[float] = []
        self._total: list[float] = []

    def emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        # Callers must never pass denied-read contents or matched secrets (see verifier redaction).
        rec = {"ts": time.time(), "session_id": self.session_id, "event": event, "payload": payload or {}}
        self._events.append(rec)
        if event == "batch_verified":
            t = rec["payload"].get("timings", {})
            if t.get("total_ms") is not None:
                self._total.append(float(t["total_ms"]))
            if t.get("lsp_total_ms"):
                self._lsp_total.append(float(t["lsp_total_ms"]))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # telemetry must never break the gate

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._events[-limit:] if limit > 0 else []

    def latency_stats(self) -> dict[str, Any]:
        def stats(vals: list[float]) -> dict[str, Any]:
            return {"count": len(vals), "p50": percentile(vals, 50),
                    "p95": percentile(vals, 95), "p99": percentile(vals, 99)}
        return {"method": "nearest-rank percentile over this session's batch_verified events",
                "lsp_total_ms": stats(self._lsp_total), "total_ms": stats(self._total)}
