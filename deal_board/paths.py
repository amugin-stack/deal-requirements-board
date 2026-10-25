"""Repository locations shared by the package, the CLI and the tests.

Data and results live at the repository root, outside the package, because they are evidence
(labeled sets, the permanent results log) rather than code. So the package is meant to be
installed in editable mode from a checkout (`pip install -e .`), and these paths point back
into that checkout.

A non-editable `pip install .` copies only `deal_board/` into site-packages, so `REPO_ROOT`
would point at site-packages and the first request would die with a FileNotFoundError deep in
the broker loader. `require_checkout` turns that into an ImportError naming the fix, raised
when this module is first imported.
"""

from pathlib import Path
from typing import Final


def require_checkout(root: Path) -> Path:
    """Return `root` if it is a repository checkout (it has a `data/` directory), else raise.

    A function of the root path only, so tests can call it with any directory; the module
    calls it once on its own location.
    """
    data = root / "data"
    if not data.is_dir():
        raise ImportError(
            f"deal_board: data directory not found at {data}. deal_board must be installed "
            "editable from a checkout (`pip install -e .`), because data/ and results/ live "
            "in the repository, not in the package."
        )
    return root


REPO_ROOT: Final = require_checkout(Path(__file__).resolve().parent.parent)
DATA_DIR: Final = REPO_ROOT / "data"
RESULTS_DIR: Final = REPO_ROOT / "results"
