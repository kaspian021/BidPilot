"""Allow ``python -m bidpilot`` (useful when ``pip install`` is unavailable).

Example (Windows, from the repository root, without installing anything)::

    set PYTHONPATH=src
    python -m bidpilot setup

On Linux/macOS::

    PYTHONPATH=src python -m bidpilot setup
"""

from __future__ import annotations

from .cli import app

if __name__ == "__main__":
    app(prog_name="bidpilot")
