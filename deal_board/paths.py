"""Repository locations shared by the package, the CLI and the tests.

Data and results live at the repository root, outside the package, because they are evidence
(labeled sets, the permanent results log) rather than code. So the package is meant to be
installed in editable mode from a checkout (`pip install -e .`), and these paths point back
into that checkout.
"""

from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
DATA_DIR: Final = REPO_ROOT / "data"
RESULTS_DIR: Final = REPO_ROOT / "results"
