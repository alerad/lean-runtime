from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
import zstandard

from lean_runtime.declaration_index import (
    DECLARATION_INDEX_SCHEMA,
    DECLARATION_SHARD_SCHEMA,
    DeclarationIndex,
    DeclarationIndexSet,
    DeclarationResolver,
    DeclarationShard,
    declaration_shard_identity,
    valid_declaration_name,
)
from lean_runtime.declaration_index_build import _declarations, _write_shard
from lean_runtime.declaration_index_oci import (
    DECLARATION_INDEX_CONFIG_MEDIA_TYPE,
    DECLARATION_INDEX_LAYER_MEDIA_TYPE,
    OCIDeclarationIndexLibrary,
)
from lean_runtime.errors import EnvironmentError
from lean_runtime.events import EventEmitter
from lean_runtime.models import Diagnostic, ExecutionResult
from lean_runtime.oci import ManifestResponse, OCIRepository
from lean_runtime.oci_protocol import MANIFEST_MEDIA_TYPE, blob_descriptor
from lean_runtime.serialization import canonical_json_bytes
from lean_runtime.store import EnvironmentStore

LOCK_ID = "lock_" + "a" * 64
TOOLCHAIN = "leanprover/lean4:v4.33.0"


def shard_id(source_id: str) -> str:
    return declaration_shard_identity(
        source_id=source_id, toolchain=TOOLCHAIN, subdir=None
    )


def write_index(
    path: Path,
    *,
    package: str,
    source_id: str,
    declarations: dict[str, tuple[str, int]],
) -> str:
    identity = shard_id(source_id)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE decl (
              name TEXT PRIMARY KEY,
              module TEXT NOT NULL,
              kind TEXT NOT NULL,
              weight INTEGER NOT NULL DEFAULT 0
            ) WITHOUT ROWID;
            CREATE TABLE suffix (
              suffix TEXT NOT NULL,
              name TEXT NOT NULL,
              PRIMARY KEY (suffix, name)
            ) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            (("schema", DECLARATION_SHARD_SCHEMA), ("shard_id", identity)),
        )
        connection.executemany(
            "INSERT INTO decl VALUES (?, ?, 'declaration', ?)",
            ((name, module, weight) for name, (module, weight) in declarations.items()),
        )
        connection.executemany(
            "INSERT INTO suffix VALUES (?, ?)",
            ((name.rsplit(".", 1)[-1], name) for name in declarations),
        )
    return identity


def rejection(message: str) -> ExecutionResult:
    return ExecutionResult(
        ok=False,
        exit_code=1,
        toolchain=TOOLCHAIN,
        command=("lean", "Main.lean"),
        cwd="/fixture",
        stdout="",
        stderr="",
        elapsed_seconds=0.1,
        diagnostics=(Diagnostic("error", message),),
    )


def descriptor(
    path: Path,
    *,
    identity: str,
    package: str,
    source_id: str,
    namespaces: tuple[str, ...],
    layer_digest: str = "sha256:" + "0" * 64,
    layer_size: int = 1,
) -> DeclarationShard:
    return DeclarationShard(
        identity,
        package,
        source_id,
        TOOLCHAIN,
        None,
        (package.title(),),
        namespaces,
        "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_size,
        layer_digest,
        layer_size,
    )


def test_composed_exact_and_weighted_suffix_lookup(tmp_path: Path) -> None:
    mathlib_path = tmp_path / "mathlib.sqlite"
    batteries_path = tmp_path / "batteries.sqlite"
    mathlib_id = write_index(
        mathlib_path,
        package="mathlib",
        source_id="source_mathlib",
        declarations={
            "Nat.exists_infinite_primes": ("Mathlib.Data.Nat.Prime.Infinite", 7),
        },
    )
    batteries_id = write_index(
        batteries_path,
        package="batteries",
        source_id="source_batteries",
        declarations={"Other.exists_infinite_primes": ("Batteries.Other", 1)},
    )
    index = DeclarationIndexSet(
        LOCK_ID,
        (
            (
                descriptor(
                    mathlib_path,
                    identity=mathlib_id,
                    package="mathlib",
                    source_id="source_mathlib",
                    namespaces=("Nat",),
                ),
                DeclarationIndex(mathlib_path, expected_shard_id=mathlib_id),
            ),
            (
                descriptor(
                    batteries_path,
                    identity=batteries_id,
                    package="batteries",
                    source_id="source_batteries",
                    namespaces=("Other",),
                ),
                DeclarationIndex(batteries_path, expected_shard_id=batteries_id),
            ),
        ),
    )

    assert index.resolve("Nat.exists_infinite_primes") is not None
    assert [item.name for item in index.resolve_suffix("exists_infinite_primes")] == [
        "Nat.exists_infinite_primes",
        "Other.exists_infinite_primes",
    ]
    enriched = DeclarationResolver().enrich(
        index,
        rejection("Unknown identifier `Nat.exists_infinite_primes`"),
        environment_label="mathlib-v4.33.0",
    )
    assert "Mathlib.Data.Nat.Prime.Infinite" in enriched.hints[0]


def test_unicode_and_quoted_lean_names_are_valid() -> None:
    assert valid_declaration_name("Action.β_hom_hom")
    assert valid_declaration_name("AbstractMeasure.«termD(_,_)»")
    assert not valid_declaration_name("bad\nterminal")
    assert not valid_declaration_name("bad`fence")


def test_deterministic_writer_is_byte_identical(tmp_path: Path) -> None:
    identity = shard_id("source_mathlib")
    kwargs = {
        "shard_id": identity,
        "source_id": "source_mathlib",
        "toolchain": TOOLCHAIN,
        "subdir": None,
        "declarations": {
            "Nat.zeta": "Mathlib.Nat",
            "Nat.alpha": "Mathlib.Nat",
        },
        "weights": {"Nat.alpha": 4},
    }
    first = tmp_path / "first.sqlite"
    second = tmp_path / "second.sqlite"
    _write_shard(first, **kwargs)  # type: ignore[arg-type]
    _write_shard(second, **kwargs)  # type: ignore[arg-type]
    assert first.read_bytes() == second.read_bytes()


def test_public_ilean_export_assigns_names_to_their_package_module(tmp_path: Path) -> None:
    root = tmp_path / "lib" / "lean"
    index = root / "Example" / "Basic.ilean"
    index.parent.mkdir(parents=True)
    index.write_text(
        '{"module":"Example.Basic","decls":'
        '{"Example.answer":{},"Example._auxLemma.1":{}}}',
        encoding="utf-8",
    )

    declarations, module_roots, namespace_roots = _declarations(root)

    assert declarations == {"Example.answer": "Example.Basic"}
    assert module_roots == ("Example",)
    assert namespace_roots == ("Example",)


def test_shard_identity_is_toolchain_conservative() -> None:
    source_id = "source_" + "a" * 64
    first = declaration_shard_identity(
        source_id=source_id,
        toolchain="leanprover/lean4:v4.33.0",
        subdir=None,
    )
    patch = declaration_shard_identity(
        source_id=source_id,
        toolchain="leanprover/lean4:v4.33.1",
        subdir=None,
    )
    assert first != patch


def test_index_rejects_the_wrong_shard(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    write_index(
        path,
        package="mathlib",
        source_id="source_mathlib",
        declarations={"Nat.foo": ("Mathlib.Nat", 0)},
    )
    with pytest.raises(EnvironmentError, match="identity"):
        DeclarationIndex(path, expected_shard_id="declshard_" + "b" * 64)


class FakeClient:
    def __init__(self, manifest: bytes, config: bytes, blobs: dict[str, bytes]) -> None:
        self.manifest_data = manifest
        self.config = config
        self.blobs = blobs
        self.manifest_calls = 0
        self.downloaded: list[str] = []

    def manifest(self, _reference: str) -> ManifestResponse:
        self.manifest_calls += 1
        digest = "sha256:" + hashlib.sha256(self.manifest_data).hexdigest()
        return ManifestResponse(self.manifest_data, MANIFEST_MEDIA_TYPE, digest)

    def read_blob(self, _descriptor: dict[str, object], *, limit: int) -> bytes:
        assert len(self.config) <= limit
        return self.config

    def download_blob(
        self,
        descriptor: dict[str, object],
        store: EnvironmentStore,
        _events: EventEmitter,
        *,
        cancel=None,  # type: ignore[no-untyped-def]
    ) -> Path:
        del cancel
        digest = str(descriptor["digest"])
        self.downloaded.append(digest)
        path = store.oci_blobs / digest.removeprefix("sha256:")
        path.write_bytes(self.blobs[digest])
        return path


def oci_fixture(
    tmp_path: Path,
) -> tuple[bytes, bytes, dict[str, bytes], tuple[DeclarationShard, ...]]:
    inputs = (
        (
            "mathlib",
            "source_mathlib",
            {"Nat.foo": ("Mathlib.Nat", 2)},
            ("Nat",),
        ),
        (
            "batteries",
            "source_batteries",
            {"Batteries.bar": ("Batteries.Basic", 1)},
            ("Batteries",),
        ),
    )
    blobs: dict[str, bytes] = {}
    shards: list[DeclarationShard] = []
    layers: list[dict[str, object]] = []
    for package, source_id, declarations, namespaces in inputs:
        path = tmp_path / f"{package}.sqlite"
        identity = write_index(
            path,
            package=package,
            source_id=source_id,
            declarations=declarations,
        )
        compressed = zstandard.ZstdCompressor().compress(path.read_bytes())
        layer = blob_descriptor(compressed, DECLARATION_INDEX_LAYER_MEDIA_TYPE)
        blobs[str(layer["digest"])] = compressed
        layers.append(layer)
        shards.append(
            descriptor(
                path,
                identity=identity,
                package=package,
                source_id=source_id,
                namespaces=namespaces,
                layer_digest=str(layer["digest"]),
                layer_size=len(compressed),
            )
        )
    config = canonical_json_bytes(
        {
            "schema": DECLARATION_INDEX_SCHEMA,
            "lock_id": LOCK_ID,
            "shards": [shard.to_dict() for shard in shards],
        }
    )
    manifest = canonical_json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST_MEDIA_TYPE,
            "config": blob_descriptor(config, DECLARATION_INDEX_CONFIG_MEDIA_TYPE),
            "layers": layers,
        }
    )
    return manifest, config, blobs, tuple(shards)


def test_oci_fetches_only_prefix_routed_shards_then_completes_bare_lookup(
    tmp_path: Path,
) -> None:
    manifest, config, blobs, shards = oci_fixture(tmp_path)
    store = EnvironmentStore(tmp_path / "store")
    library = OCIDeclarationIndexLibrary(
        OCIRepository.parse("oci://registry.example/example/cache"), store, EventEmitter()
    )
    client = FakeClient(manifest, config, blobs)
    library.client = client  # type: ignore[assignment]

    batteries = library.acquire(LOCK_ID, ("Batteries.bar",))
    assert batteries.resolve("Batteries.bar") is not None
    assert client.downloaded == [shards[1].layer_digest]
    records = store.declaration_index_shards(LOCK_ID)
    assert records[0][1] is None
    assert records[1][1] is not None

    complete = library.acquire(LOCK_ID, ("bar",))
    assert complete.resolve_suffix("bar")
    assert client.downloaded == [shards[1].layer_digest, shards[0].layer_digest]
    assert client.manifest_calls == 2
    status = store.status(verify=True)
    assert status.declaration_indexes == 2


def test_oci_rejects_shard_digest_mismatch(tmp_path: Path) -> None:
    manifest, config, blobs, shards = oci_fixture(tmp_path)
    bad = bytearray(blobs[shards[0].layer_digest])
    bad[-1] ^= 1
    blobs[shards[0].layer_digest] = bytes(bad)
    store = EnvironmentStore(tmp_path / "store")
    library = OCIDeclarationIndexLibrary(
        OCIRepository.parse("oci://registry.example/example/cache"), store, EventEmitter()
    )
    library.client = FakeClient(manifest, config, blobs)  # type: ignore[assignment]

    with pytest.raises(EnvironmentError):
        library.acquire(LOCK_ID, ("Nat.foo",))
    assert store.declaration_index_shards(LOCK_ID) == ()
