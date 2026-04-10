"""One-shot entrypoint for GitHub Actions or cron-style runs."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from monitor import async_run_once  # noqa: E402


def main() -> int:
    """Run one monitoring cycle and exit."""

    return asyncio.run(async_run_once())


if __name__ == "__main__":
    raise SystemExit(main())
