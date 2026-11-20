import json
import sys
from pathlib import Path

from patchgate.core.config import LspConfig
from patchgate.core.lsp_client import PyrightClient

FAKE = Path(__file__).parent / "fakes" / "fake_lsp_server.py"


def make_client(tmp_path: Path, scenario: dict, **cfg) -> tuple[PyrightClient, Path]:
    log = tmp_path / "fake.log"
    scenario = {"log": str(log), **scenario}
    sc = tmp_path / "scenario.json"
    sc.write_text(json.dumps(scenario))
    import os
    os.environ["FAKE_LSP_SCENARIO"] = str(sc)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cfg = {"timeout_sec": 3, "quiescence_ms": 80, **cfg}
    client = PyrightClient(repo, LspConfig(**cfg), command=[sys.executable, str(FAKE)])
    # the fake gets "--stdio" appended by the client; it ignores argv
    return client, log


def read_log(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]


def changes_for(log: Path, uri_suffix: str) -> list[str]:
    """All texts sent for a doc (didOpen text / didChange text), in order."""
    out = []
    for m in read_log(log):
        if m.get("method") == "textDocument/didOpen" and m["params"]["textDocument"]["uri"].endswith(uri_suffix):
            out.append(m["params"]["textDocument"]["text"])
        if m.get("method") == "textDocument/didChange" and m["params"]["textDocument"]["uri"].endswith(uri_suffix):
            out.append(m["params"]["contentChanges"][-1]["text"])
    return out
