"""Small CI reporting helpers shared by the test suite."""

from __future__ import annotations

import os

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]):
    """Expose test tracebacks as GitHub annotations without a CI-only plugin."""
    outcome = yield
    report = outcome.get_result()
    if os.environ.get("GITHUB_ACTIONS") == "true" and report.failed and call.excinfo is not None:
        path, line, _ = item.location
        message = str(call.excinfo.getrepr(style="short")).replace("%", "%25")
        message = message.replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error file={path},line={line + 1},title=pytest failure::{message}")
