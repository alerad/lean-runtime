"""Terminal rendering of structured runtime progress events."""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Literal, TextIO

from .events import RuntimeEvent
from .policies import format_byte_size

RenderMode = Literal["tty", "plain", "quiet"]

_BAR_WIDTH = 20
_TTY_REDRAW_SECONDS = 0.1
_PLAIN_CHECKPOINT_PERCENT = 25
_LARGE_CLOSURE_BYTES = 1024**3
_HEARTBEAT_POLL_SECONDS = 0.25
_PLAIN_HEARTBEAT_FIRST_SECONDS = 10.0
_PLAIN_HEARTBEAT_REPEAT_SECONDS = 30.0
_DETAIL_WIDTH = 60
_OUTPUT_WIDTH = 100


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


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


def verdict_line(result: Any, *, style: Styler, subject: str | None = None) -> str:
    """One line that attributes Lean's verdict to the exact environment it ran in.

    ``result`` is an ``ExecutionResult``; typed loosely to keep this module
    free of model imports.
    """
    where = result.environment_id or result.toolchain
    if result.verdict == "accepted":
        symbol, status = style.green("✓"), style.green("accepted")
    elif result.verdict == "rejected":
        symbol, status = style.red("✗"), style.red("rejected")
    else:
        detail = "timed out" if result.timed_out else "cancelled"
        symbol, status = style.red("✗"), style.red(f"{detail} (no verdict)")
    timing = style.dim(f"({result.elapsed_seconds:.2f}s)")
    prefix = f"{symbol} {subject} " if subject else f"{symbol} "
    return f"{prefix}{status} in {where} {timing}"


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
        heartbeat_seconds: float | None = None,
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
        self._count_key: tuple[str, int] | None = None
        self._count_checkpoint = _PLAIN_CHECKPOINT_PERCENT
        # Heartbeat: when events stop arriving mid-operation, keep showing the
        # last known activity so a long silent phase never looks hung.
        self._heartbeat_seconds = heartbeat_seconds
        self._lock = threading.RLock()
        self._last_event_at: float | None = None
        self._last_message = ""
        self._next_plain_heartbeat = _PLAIN_HEARTBEAT_FIRST_SECONDS
        self._heartbeat_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def __call__(self, event: RuntimeEvent) -> None:
        if self.mode == "quiet":
            return
        with self._lock:
            self._note_activity(event)
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
        with self._lock:
            self._print(message)

    def close(self) -> None:
        """Terminate any in-place progress line before final output."""
        self._stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        with self._lock:
            self._end_progress_line()

    # -- heartbeat --------------------------------------------------------

    def _note_activity(self, event: RuntimeEvent) -> None:
        self._last_event_at = self._clock()
        self._last_message = event.message
        self._next_plain_heartbeat = _PLAIN_HEARTBEAT_FIRST_SECONDS
        if (
            self._heartbeat_seconds is not None
            and self._heartbeat_thread is None
            and not self._stop.is_set()
        ):
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, name="lean-runtime-heartbeat", daemon=True
            )
            self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        assert self._heartbeat_seconds is not None
        while not self._stop.wait(_HEARTBEAT_POLL_SECONDS):
            with self._lock:
                if self._last_event_at is None or not self._last_message:
                    continue
                idle = self._clock() - self._last_event_at
                if idle < self._heartbeat_seconds:
                    continue
                last = _truncate(self._last_message, _OUTPUT_WIDTH)
                if self.mode == "tty":
                    text = f"⋯ no new events for {int(idle)}s · last: {last}"
                    self._draw_line(text, self.style.dim(text))
                elif idle >= self._next_plain_heartbeat:
                    self._print(f"Still working ({int(idle)}s without events; last: {last})")
                    self._next_plain_heartbeat = idle + _PLAIN_HEARTBEAT_REPEAT_SECONDS

    # -- individual event renderings ------------------------------------

    def _render_package_reference_started(self, event: RuntimeEvent) -> None:
        reference = event.data.get("reference", "")
        self._print(f"Resolving {reference}".rstrip())

    def _render_discovery_candidate_started(self, event: RuntimeEvent) -> None:
        if event.data.get("remembered") is True:
            return
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
        if download >= _LARGE_CLOSURE_BYTES:
            self._print(
                f"Large import closure: {format_byte_size(download)} to download; "
                "narrower imports may fetch less"
            )
        self._start_download("Downloading environment", download, cached)

    def _render_toolchain_download_planned(self, event: RuntimeEvent) -> None:
        download = event.data.get("download_bytes")
        cached = event.data.get("cached_bytes")
        if not isinstance(download, int) or download <= 0:
            return
        self._start_download("Downloading Lean toolchain", download, cached)

    def _start_download(self, label: str, download: int, cached: object) -> None:
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
        self._print(f"{label}: {format_byte_size(download)}{suffix}")

    def _render_library_layer_progress(self, event: RuntimeEvent) -> None:
        digest = str(event.data.get("digest", ""))
        if event.current_bytes is None or self._download_total is None or self._download_finished:
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
        self._finish_download("Downloaded and verified environment")

    def _render_toolchain_ready(self, event: RuntimeEvent) -> None:
        self._finish_download("Downloaded and verified Lean toolchain")

    def _finish_download(self, message: str) -> None:
        if self._download_total is None or self._download_finished:
            return
        self._download_finished = True
        if self.mode == "tty":
            self._draw_bar(self._download_total, self._download_total)
        self._end_progress_line()
        self._print(self.style.green(message))

    def _render_source_fetch_started(self, event: RuntimeEvent) -> None:
        package = event.data.get("package", "sources")
        self._print(f"Fetching {package} source")

    def _render_artifact_hydration_started(self, event: RuntimeEvent) -> None:
        package = event.data.get("package", "artifacts")
        self._print(f"Hydrating build artifacts for {package}")

    def _render_artifact_hydration_failed(self, event: RuntimeEvent) -> None:
        package = event.data.get("package", "dependency")
        self._print(
            self.style.yellow(
                f"WARNING: artifact cache unavailable for {package}; building from source"
            )
        )

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

    def _render_check_started(self, event: RuntimeEvent) -> None:
        if self.mode != "tty":
            return
        subject = str(event.data.get("subject") or "Lean input")
        message = f"Checking {subject}…"
        self.stream.write("\r" + self.style.cyan(message))
        self.stream.flush()
        self._line_length = len(message)

    def _render_check_completed(self, _event: RuntimeEvent) -> None:
        self._end_progress_line()

    def _render_check_header_wait(self, event: RuntimeEvent) -> None:
        module = str(event.data.get("module") or "Lean input")
        self._print(f"Waiting for header snapshot initialization: {module}")

    def _render_project_workspace_lock_wait(self, event: RuntimeEvent) -> None:
        self._print(event.message)

    # -- subprocess output and counted progress ---------------------------

    def _render_process_progress(self, event: RuntimeEvent) -> None:
        self._count_from(event, str(event.data.get("label") or "Working"), "detail")

    def _render_process_output(self, event: RuntimeEvent) -> None:
        if self.mode != "tty":
            return
        line = str(event.data.get("line") or "").strip()
        if line:
            text = _truncate(line, _OUTPUT_WIDTH)
            self._draw_line(text, self.style.dim(text))

    def _render_adopt_inspect_started(self, event: RuntimeEvent) -> None:
        self._count_from(event, "Inspecting projects", "name")

    def _render_adopt_identity_started(self, event: RuntimeEvent) -> None:
        self._count_from(event, "Resolving dependency identities", "name")

    def _render_adopt_attach_started(self, event: RuntimeEvent) -> None:
        self._count_from(event, "Attaching projects", "name")

    def _render_project_detach_package_started(self, event: RuntimeEvent) -> None:
        self._count_from(event, "Materializing packages", "package")

    def _count_from(self, event: RuntimeEvent, label: str, detail_key: str) -> None:
        current = event.data.get("current")
        total = event.data.get("total")
        if not isinstance(current, int) or not isinstance(total, int) or total <= 0:
            return
        self._render_count_progress(label, current, total, str(event.data.get(detail_key) or ""))

    def _render_count_progress(self, label: str, current: int, total: int, detail: str) -> None:
        current = min(current, total)
        if self.mode == "tty":
            now = self._clock()
            if (
                current < total
                and self._last_redraw is not None
                and now - self._last_redraw < _TTY_REDRAW_SECONDS
            ):
                return
            self._last_redraw = now
            filled = _BAR_WIDTH * current // total
            bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
            suffix = f" · {_truncate(detail, _DETAIL_WIDTH)}" if detail else ""
            plain = f"{label} [{bar}] {current}/{total}{suffix}"
            styled = (
                f"{label} [{self.style.cyan(bar)}] "
                f"{self.style.bold(f'{current}/{total}')}{self.style.dim(suffix)}"
            )
            self._draw_line(plain, styled)
            if current >= total:
                self._end_progress_line()
            return
        key = (label, total)
        if self._count_key != key:
            self._count_key = key
            self._count_checkpoint = _PLAIN_CHECKPOINT_PERCENT
        percent = current * 100 // total
        while self._count_checkpoint <= percent:
            self._print(f"{label}: {self._count_checkpoint}% ({current}/{total})")
            self._count_checkpoint += _PLAIN_CHECKPOINT_PERCENT

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

    def _draw_line(self, plain: str, styled: str | None = None) -> None:
        """Redraw the single in-place status line."""
        padding = " " * max(0, self._line_length - len(plain))
        self.stream.write("\r" + (styled if styled is not None else plain) + padding)
        self.stream.flush()
        self._line_length = len(plain)

    def _end_progress_line(self) -> None:
        if self._line_length:
            self.stream.write("\n")
            self.stream.flush()
            self._line_length = 0

    def _print(self, message: str) -> None:
        with self._lock:
            self._end_progress_line()
            self.stream.write(message + "\n")
            self.stream.flush()
