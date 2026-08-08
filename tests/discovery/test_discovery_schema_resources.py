import json

from lean_runtime.discovery import SCHEMA_NAMES, schema_path


def test_all_bundled_schemas_are_readable() -> None:
    for name in SCHEMA_NAMES:
        payload = json.loads(schema_path(name).read_text(encoding="utf-8"))
        assert payload["$schema"].endswith("2020-12/schema")
