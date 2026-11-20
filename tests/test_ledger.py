from patchgate.core.ledger import Ledger
from patchgate.core.models import Status


def test_trips_at_limit_and_resets_on_success():
    lg = Ledger(4)
    for _ in range(3):
        lg.record_failure(["a.py"], Status.REJECTED_LSP, "boom")
    assert not lg.is_tripped(["a.py"])
    lg.record_failure(["a.py"], Status.REJECTED_LSP, "boom")
    assert lg.is_tripped(["a.py", "b.py"]) and lg.tripped_files(["a.py", "b.py"]) == ["a.py"]
    assert not lg.is_tripped(["b.py"])
    lg.reset_files(["a.py"])
    assert not lg.is_tripped(["a.py"])


def test_consecutive_reset_between_failures():
    lg = Ledger(2)
    lg.record_failure(["a.py"], Status.REJECTED_AST, "x")
    lg.reset_files(["a.py"])
    lg.record_failure(["a.py"], Status.REJECTED_AST, "x")
    assert not lg.is_tripped(["a.py"])


def test_recent_for_filters_and_limits():
    lg = Ledger(10)
    for i in range(5):
        lg.record_failure(["a.py"], Status.REJECTED_LSP, f"a{i}")
    lg.record_failure(["b.py"], Status.REJECTED_LSP, "b0")
    assert [f.summary for f in lg.recent_for(["a.py"], 3)] == ["a2", "a3", "a4"]
    assert [f.summary for f in lg.recent_for(["b.py"], 3)] == ["b0"]


def test_tripped_state_has_no_public_reset_other_than_success_path():
    # The ledger exposes reset_files only; the MCP adapter (tested in the e2e test) must never call it
    # except after a verified commit. Guard against accidental extra public mutators.
    public = {n for n in dir(Ledger) if not n.startswith("_")}
    assert public == {"record_failure", "reset_files", "tripped_files", "is_tripped", "recent_for"}
