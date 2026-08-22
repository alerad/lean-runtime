#!/usr/bin/env python3
"""Publish and optionally sign one exact composable declaration index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lean_runtime import EnvironmentLock
from lean_runtime.declaration_index_build import load_declaration_index_build
from lean_runtime.declaration_index_oci import OCIDeclarationIndexPublisher
from lean_runtime.oci import OCIRepository
from lean_runtime.publisher_verification import CosignVerifier


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--sign", action="store_true")
    arguments = parser.parse_args()
    lock = EnvironmentLock.load(arguments.lock)
    built = load_declaration_index_build(arguments.build, expected_lock_id=lock.lock_id)
    repository = OCIRepository.parse(arguments.library)
    publication = OCIDeclarationIndexPublisher(repository).publish(
        tuple(item.source for item in built.shards), lock_id=lock.lock_id
    )
    if arguments.sign:
        CosignVerifier().sign(repository, publication.manifest_digest)
    print(json.dumps(publication.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
