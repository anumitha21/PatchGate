"""Pydantic v2 models shared across PatchGate core."""
from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, model_validator


class Replace(BaseModel):  # defined BEFORE FileEdit so the annotation resolves
    old_text: str  # must occur EXACTLY ONCE in the current file
    new_text: str


class FileEdit(BaseModel):
    path: str  # relative to repo root, POSIX separators
    new_content: str | None = None  # full-file mode
    replace: Replace | None = None  # exact-match mode

    @model_validator(mode="after")
    def _exactly_one(self) -> "FileEdit":
        if (self.new_content is None) == (self.replace is None):
            raise ValueError("exactly one of new_content / replace must be set")
        return self


class ResolvedEdit(BaseModel):
    path: Path  # absolute, validated inside repo root
    rel_path: str
    old_content: str | None  # None => new file
    new_content: str


class DiagnosticItem(BaseModel):
    rel_path: str
    line: int  # 0-based as in LSP
    character: int
    severity: int  # 1=Error 2=Warning 3=Info 4=Hint (LSP)
    code: str | None
    message: str
    signature: str


class PolicyViolation(BaseModel):
    rule: str
    rel_path: str
    detail: str


class StageTimings(BaseModel):
    policy_ms: float = 0
    ast_ms: float = 0
    lsp_baseline_ms: float = 0
    lsp_apply_ms: float = 0
    lsp_wait_ms: float = 0
    lsp_total_ms: float = 0
    total_ms: float = 0


class Status(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED_POLICY = "REJECTED_POLICY"
    REJECTED_AST = "REJECTED_AST"
    REJECTED_LSP = "REJECTED_LSP"
    LSP_TIMEOUT = "LSP_TIMEOUT"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    ERROR = "ERROR"


class BatchVerificationResult(BaseModel):
    status: Status
    violations: list[PolicyViolation] = []
    net_new_diagnostics: list[DiagnosticItem] = []
    warnings: list[DiagnosticItem] = []  # non-blocking, net-new
    dependents_checked: list[str] = []
    timings: StageTimings
    message: str
