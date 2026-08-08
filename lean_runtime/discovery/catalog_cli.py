"""Command-line entry point for deterministic catalog generation."""

from __future__ import annotations

import argparse
import os
import sys

from ..runtime import Runtime
from .catalog_build import build_catalog_file
from .errors import DiscoveryError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lean-runtime-catalog",
        description="Build a canonical catalog from exact Lean Runtime locks.",
    )
    parser.add_argument("command", choices=("build",))
    parser.add_argument("manifest", help="catalog source TOML manifest")
    parser.add_argument("--output", required=True, help="output catalog JSON")
    parser.add_argument("--runtime-home", help="Lean Runtime store used for exact sources")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Mathlib's post-update hook otherwise downloads its multi-gigabyte compiled
    # cache. Catalog generation needs exact source and dependency metadata only.
    os.environ.setdefault("MATHLIB_NO_CACHE_ON_UPDATE", "1")
    try:
        catalog = build_catalog_file(
            args.manifest,
            args.output,
            runtime=Runtime(home=args.runtime_home, libraries=()),
        )
    except DiscoveryError as exc:
        print(f"lean-runtime-catalog: {exc}", file=sys.stderr)
        return 2
    print(
        f"wrote {args.output}: {len(catalog.entries)} environments, "
        f"{sum(len(entry.modules) for entry in catalog.entries)} module records, "
        f"{catalog.digest}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by wheel/CLI smoke tests
    raise SystemExit(main())
