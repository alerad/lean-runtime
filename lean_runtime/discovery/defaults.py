"""Built-in discovery resources shipped with Lean Runtime."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .catalog import Catalog

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "catalog.json"


@lru_cache(maxsize=1)
def default_catalog() -> Catalog:
    """Load the immutable bootstrap catalog bundled in this Runtime release."""

    return Catalog.from_file(DEFAULT_CATALOG_PATH)
