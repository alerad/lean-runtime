"""Internal construction of portable Git commands."""

from __future__ import annotations


def git_command(*arguments: str) -> list[str]:
    """Return a Git command that can materialize Lean's deep source trees.

    ``core.longpaths`` is meaningful on Git for Windows and harmless on other
    platforms. Supplying it per invocation avoids mutable global Git settings.
    """

    return ["git", "-c", "core.longpaths=true", *arguments]
