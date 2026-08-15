"""Terminal rendering of structured runtime progress events."""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Literal, TextIO

from .events import RuntimeEvent
from .policies import format_byte_size

RenderMode = Literal["tty", "plain", "quiet"]

_BAR_WIDTH = 20
_TTY_REDRAW_SECONDS = 0.1
_PLAIN_CHECKPOINT_PERCENT = 25


def select_mode(*, quiet: bool = False, stream: TextIO | None = None) -> RenderMode:
    if quiet:
        return "quiet"
    stream = stream if stream is not None else sys.stderr
    return "tty" if stream.isatty() else "plain"


class Styler:
    """Minimal ANSI styling that degrades to plain text when disabled."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)


def styler_for(stream: TextIO | None = None) -> Styler:
    """Style only real terminals, honoring the NO_COLOR convention."""
    stream = stream if stream is not None else sys.stderr
    isatty = getattr(stream, "isatty", None)
    enabled = bool(isatty and isatty()) and "NO_COLOR" not in os.environ
    return Styler(enabled)


class ConsoleRenderer:
    """Render runtime events as terse, single-purpose progress output.

    Warm runs stay silent; cold acquisitions get one context line and one
    aggregate progress line. Everything else is behind ``verbose``.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        mode: RenderMode | None = None,
        verbose: bool = False,
        color: bool | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.mode: RenderMode = mode if mode is not None else select_mode(stream=self.stream)
        self.verbose = verbose
        self.style = Styler(color) if color is not None else styler_for(self.stream)
        self._clock = clock
        self._download_total: int | None = None
        self._layer_bytes: dict[str, int] = {}
        self._line_length = 0
        self._last_redraw: float | None = None
        self._next_checkpoint = _PLAIN_CHECKPOINT_PERCENT
        self._download_finished = False

    def __call__(self, event: RuntimeEvent) -> None:
        if self.mode == "quiet":
            return
        if self.verbose:
            self._verbose_line(event)
            return
        handler = getattr(self, "_render_" + event.kind.replace(".", "_"), None)
        if handler is not None:
            handler(event)

    def note(self, message: str) -> None:
        """Print one renderer-owned status line."""
        if self.mode == "quiet":
            return
        self._print(message)

    def close(self) -> None:
        """Terminate any in-place progress line before final output."""
        self._end_progress_line()

    # -- individual event renderings ------------------------------------

    def _render_package_reference_started(self, event: RuntimeEvent) -> None:
        reference = event.data.get("reference", "")
        self._print(f"Resolving {reference}".rstrip())

    def _render_discovery_candidate_started(self, event: RuntimeEvent) -> None:
        candidate = event.data.get("candidate", "candidate")
        toolchain = event.data.get("toolchain")
        suffix = f" · {toolchain}" if toolchain else ""
        self._print(f"Trying {candidate}{suffix}")

    def _render_toolchain_install_started(self, event: RuntimeEvent) -> None:
        toolchain = event.data.get("toolchain", "toolchain")
        self._print(f"Installing Lean toolchain {toolchain} (one-time, can take minutes)")

    def _render_acquisition_planned(self, event: RuntimeEvent) -> None:
        download = event.data.get("download_bytes")
        cached = event.data.get("cached_bytes")
        if not isinstance(download, int) or download <= 0:
            return
        self._download_total = download
        self._layer_bytes.clear()
        self._next_checkpoint = _PLAIN_CHECKPOINT_PERCENT
        self._download_finished = False
        self._last_redraw = None
        suffix = (
            f" ({format_byte_size(cached)} already cached)"
            if isinstance(cached, int) and cached > 0
            else ""
        )
        self._print(f"Downloading environment: {format_byte_size(download)}{suffix}")

    def _render_library_layer_progress(self, event: RuntimeEvent) -> None:
        digest = str(event.data.get("digest", ""))
        if event.current_bytes is None or self._download_total is None:
            return
        frame_current = event.data.get("frame_current")
        frame_total = event.data.get("frame_total")
        aggregate_frames = isinstance(frame_current, int) and isinstance(frame_total, int)
        if aggregate_frames and event.total_bytes is not None:
            done = event.current_bytes
            total = event.total_bytes
        else:
            self._layer_bytes[digest] = event.current_bytes
            done = min(sum(self._layer_bytes.values()), self._download_total)
            total = self._download_total
        if self.mode == "tty":
            now = self._clock()
            if (
                self._last_redraw is not None
                and done < total
                and now - self._last_redraw < _TTY_REDRAW_SECONDS
            ):
                return
            self._last_redraw = now
            self._draw_bar(done, total, frame_progress=(frame_current, frame_total))
        else:
            percent = done * 100 // total
            while self._next_checkpoint <= percent:
                frames = f" · frames {frame_current}/{frame_total}" if aggregate_frames else ""
                self._print(f"Downloaded {self._next_checkpoint}%{frames}")
                self._next_checkpoint += _PLAIN_CHECKPOINT_PERCENT

    def _render_library_layer_download_started(self, event: RuntimeEvent) -> None:
        resumed = event.data.get("resumed_bytes")
        if isinstance(resumed, int) and resumed > 0:
            self._print(f"Resuming interrupted download from {format_byte_size(resumed)}")

    def _render_library_layer_download_retry(self, event: RuntimeEvent) -> None:
        self._print("Retrying download after integrity verification failed")

    def _render_library_verified(self, event: RuntimeEvent) -> None:
        if self._download_total is None or self._download_finished:
            return
        self._download_finished = True
        if self.mode == "tty":
            self._draw_bar(self._download_total, self._download_total)
        self._end_progress_line()
        self._print(self.style.green("Downloaded and verified environment"))

    def _render_source_fetch_started(self, event: RuntimeEvent) -> None:
        package = event.data.get("package", "sources")
        self._print(f"Fetching {package} source")

    def _render_artifact_hydration_started(self, event: RuntimeEvent) -> None:
        package = event.data.get("package", "artifacts")
        self._print(f"Hydrating build artifacts for {package}")

    def _render_availability_fallback(self, event: RuntimeEvent) -> None:
        library = event.data.get("library", "environment library")
        reason = event.data.get("reason_code") or event.data.get("reason") or "unavailable"
        self._print(
            self.style.yellow(
                f"WARNING: downloadable environment unavailable from {library} ({reason})"
            )
        )

    def _render_environment_build_started(self, event: RuntimeEvent) -> None:
        self._print(
            "Building environment from source (large dependencies such as Mathlib can take "
            "30+ minutes; pass --no-source-build to fail fast instead)"
        )

    def _render_capability_required(self, event: RuntimeEvent) -> None:
        self._print(event.message)

    # -- low-level output -----------------------------------------------

    def _verbose_line(self, event: RuntimeEvent) -> None:
        parts = [f"{event.kind}: {event.message}"]
        if event.current_bytes is not None and event.total_bytes is not None:
            parts.append(
                f"[{format_byte_size(event.current_bytes)}/{format_byte_size(event.total_bytes)}]"
            )
        frame_current = event.data.get("frame_current")
        frame_total = event.data.get("frame_total")
        if isinstance(frame_current, int) and isinstance(frame_total, int):
            parts.append(f"[frames {frame_current}/{frame_total}]")
        if event.data:
            details = " ".join(
                f"{key}={value}"
                for key, value in sorted(event.data.items())
                if key not in {"frame_current", "frame_total"}
            )
            if details:
                parts.append(f"({details})")
        self._print(" ".join(parts))

    def _draw_bar(
        self, done: int, total: int, *, frame_progress: tuple[object, object] | None = None
    ) -> None:
        filled = _BAR_WIDTH * done // total if total else _BAR_WIDTH
        bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
        percent = done * 100 // total if total else 100
        sizes = f"{format_byte_size(done)}/{format_byte_size(total)}"
        frames = f" · frames {frame_progress[0]}/{frame_progress[1]}" if frame_progress else ""
        plain = f"Downloading [{bar}] {percent}% · {sizes}{frames}"
        line = (
            f"Downloading [{self.style.cyan(bar)}] "
            f"{self.style.bold(f'{percent}%')} · {self.style.dim(sizes + frames)}"
        )
        padding = " " * max(0, self._line_length - len(plain))
        self.stream.write("\r" + line + padding)
        self.stream.flush()
        self._line_length = len(plain)

    def _end_progress_line(self) -> None:
        if self._line_length:
            self.stream.write("\n")
            self.stream.flush()
            self._line_length = 0

    def _print(self, message: str) -> None:
        self._end_progress_line()
        self.stream.write(message + "\n")
        self.stream.flush()
