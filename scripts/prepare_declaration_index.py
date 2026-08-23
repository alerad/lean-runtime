#!/usr/bin/env python3
"""Normalize the prototype corpus export into one public-format package shard."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

from lean_runtime import EnvironmentLock
from lean_runtime.declaration_index import (
    DECLARATION_SHARD_SCHEMA,
    DeclarationIndex,
    declaration_shard_identity,
)
from lean_runtime.declaration_index_build import (
    BuiltDeclarationShard,
    DeclarationIndexBuild,
)
from lean_runtime.declaration_index_oci import DeclarationShardSource
from lean_runtime.serialization import canonical_json_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--package", required=True)
    arguments = parser.parse_args()
    source = arguments.source.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    created_output = not output.exists()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"refusing to use nonempty output: {output}")
    lock = EnvironmentLock.load(arguments.lock)
    package = next((item for item in lock.packages if item.name == arguments.package), None)
    if package is None:
        raise SystemExit(f"lock does not contain package {arguments.package!r}")
    shard_id = declaration_shard_identity(
        source_id=package.source_id,
        toolchain=lock.toolchain,
        subdir=package.subdir,
    )
    output.mkdir(parents=True, exist_ok=True)
    shard_path = output / f"{shard_id}.sqlite"
    shutil.copy2(source, shard_path)
    try:
        with sqlite3.connect(shard_path) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
            }
            if not {"decl", "suffix"}.issubset(tables):
                raise SystemExit("prototype must contain decl and suffix tables")
            if "meta" in tables:
                connection.execute("DROP TABLE meta")
            connection.execute(
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID"
            )
            metadata = {
                "schema": DECLARATION_SHARD_SCHEMA,
                "shard_id": shard_id,
                "source_id": package.source_id,
                "subdir": package.subdir or "",
                "toolchain": lock.toolchain,
            }
            connection.executemany("INSERT INTO meta VALUES (?, ?)", sorted(metadata.items()))
            connection.execute("PRAGMA user_version = 1")
            module_roots = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT substr(module, 1, CASE instr(module, '.') "
                    "WHEN 0 THEN length(module) ELSE instr(module, '.') - 1 END) FROM decl "
                    "ORDER BY 1"
                )
            )
            namespace_roots = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT substr(name, 1, CASE instr(name, '.') "
                    "WHEN 0 THEN length(name) ELSE instr(name, '.') - 1 END) FROM decl "
                    "ORDER BY 1"
                )
            )
            declarations = int(connection.execute("SELECT count(*) FROM decl").fetchone()[0])
            connection.commit()
            connection.execute("VACUUM")
        DeclarationIndex(shard_path, expected_shard_id=shard_id)
        manifest_path = output / "declaration-index-build.json"
        shard = DeclarationShardSource(
            shard_id,
            package.name,
            package.source_id,
            lock.toolchain,
            package.subdir,
            module_roots,
            namespace_roots,
            shard_path,
        )
        result = DeclarationIndexBuild(
            lock.lock_id,
            (BuiltDeclarationShard(shard, declarations),),
            manifest_path,
        )
        manifest_path.write_bytes(canonical_json_bytes(result.to_dict()))
    except BaseException:
        shard_path.unlink(missing_ok=True)
        (output / "declaration-index-build.json").unlink(missing_ok=True)
        if created_output:
            output.rmdir()
        raise
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
