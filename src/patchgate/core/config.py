"""Load and validate harness.yaml. Unknown keys are errors (typos must not disable rules)."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(_Strict):
    max_file_bytes: int = 1_000_000
    max_batch_files: int = 20


class AstRules(_Strict):
    block_syntax_errors: bool = True
    block_deleted_asserts: bool = True
    block_trivial_asserts: bool = True
    block_test_skip_decorators: bool = True
    block_deleted_test_functions: bool = True
    block_secrets: bool = True


class LspConfig(_Strict):
    server_command: str | None = None
    python_path: str | None = None
    block_on: list[Literal["error", "warning", "information", "hint"]] = ["error"]
    timeout_sec: float = 10
    quiescence_ms: int = 150
    on_timeout: Literal["allow_with_warning", "reject"] = "allow_with_warning"
    max_open_documents: int = 200
    dependents_depth: int = 1
    max_dependents: int = 50
    # Measurement-only switch (scripts/measure_gate.py): disable the loose-signature fallback.
    loose_fallback: bool = True


class CircuitBreaker(_Strict):
    max_consecutive_failures_per_file: int = 4


class Config(_Strict):
    version: int = 1
    read_denied: list[str] = [".env", ".env.*", "**/*.pem", "**/*.key", "**/secrets.*"]
    write_protected: list[str] = [
        "tests/**", "test_*.py", "**/test_*.py", "**/*_test.py",
        "**/conftest.py", ".github/**", "harness.yaml", "pyproject.toml",
    ]
    test_globs: list[str] = ["tests/**", "**/test_*.py", "**/*_test.py"]
    ignore_dirs: list[str] = [
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".patchgate", "build", "dist",
    ]
    limits: Limits = Limits()
    ast_rules: AstRules = AstRules()
    secret_regexes: list[str] = [
        r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"AKIA[0-9A-Z]{16}",
        r"ghp_[A-Za-z0-9]{36}",
        r"sk-[A-Za-z0-9]{20,}",
    ]
    lsp: LspConfig = LspConfig()
    circuit_breaker: CircuitBreaker = CircuitBreaker()


def load_config(path: Path | None) -> Config:
    """Load config; a missing/None path yields built-in defaults."""
    if path is None or not Path(path).is_file():
        return Config()
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)
