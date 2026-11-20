import shutil
import sys
from pathlib import Path

import pytest


def pyright_available() -> bool:
    here = Path(sys.executable).parent
    candidates = ("pyright-langserver", "pyright-python-langserver")
    return any(shutil.which(n) or (here / n).exists() or (here / f"{n}.exe").exists() for n in candidates)


def pytest_collection_modifyitems(config, items):
    if pyright_available():
        return
    skip = pytest.mark.skip(reason="Pyright language server not available")
    for it in items:
        if "integration" in it.keywords:
            it.add_marker(skip)
