"""
symbolication.py

Wraps Apple's `atos` command-line tool to do real crash symbolication --
no hand-rolled Mach-O/DWARF parsing. `atos` is the same tool Xcode shells
out to internally, so this gives correct results with a fraction of the
code and zero binary-format bugs to chase.

Only runs on macOS (atos is an Apple-only tool, ships with Xcode Command
Line Tools). On Linux/CI you can still import this module and run the
crash-log *text* parsing in isolation -- see test_symbolication.py --
but actually calling `run_atos` will fail loudly with a clear error
rather than silently returning garbage.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field


class SymbolicationError(Exception):
    pass


@dataclass
class RawFrame:
    index: int
    binary_name: str
    address: int          # absolute runtime address
    load_address: int     # binary's runtime load/base address
    offset: int


@dataclass
class RawThread:
    label: str
    crashed: bool
    frames: list[RawFrame] = field(default_factory=list)


@dataclass
class RawCrashLog:
    exception_type: str = ""
    exception_subtype: str = ""
    threads: list[RawThread] = field(default_factory=list)


@dataclass
class SymbolicatedFrame:
    index: int
    address: int
    binary_name: str
    symbol_name: str | None
    file_path: str | None
    line_number: int | None

    @property
    def display_line(self) -> str:
        addr_hex = f"0x{self.address:016x}"
        if self.symbol_name:
            if self.file_path and self.line_number:
                return f"{self.index}  {self.binary_name}  {self.symbol_name}  ({self.file_path}:{self.line_number})"
            return f"{self.index}  {self.binary_name}  {self.symbol_name}"
        return f"{self.index}  {self.binary_name}  {addr_hex}  [unresolved]"


@dataclass
class SymbolicatedCrash:
    thread_label: str
    exception_type: str
    exception_subtype: str
    frames: list[SymbolicatedFrame]


# ---------------------------------------------------------------------
# Crash log text parsing (binary-format-independent, pure text/regex --
# this part is identical in spirit to the old C++ CrashLogParser, just
# in Python, and is fully unit-testable on any platform)
# ---------------------------------------------------------------------

_FRAME_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(\S+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s*\+\s*(\d+)"
)
_THREAD_HEADER_RE = re.compile(r"^Thread\s+(\d+)(\s+Crashed)?(?:\s*\([^)]*\))?:")
_EXCEPTION_TYPE_RE = re.compile(r"^Exception Type:\s*(.+)$")
_EXCEPTION_SUBTYPE_RE = re.compile(r"^Exception Subtype:\s*(.+)$")
_TERMINATION_DESCRIPTION_RE = re.compile(r"^Termination Description:\s*(.+)$")


def parse_crash_log_text(text: str) -> RawCrashLog:
    """Parses the textual structure of a .crash/.ips log: thread labels,
    exception type, and raw frame lines. Pure parsing, no binary I/O --
    this is what makes it testable without a Mac."""
    log = RawCrashLog()
    current_thread: RawThread | None = None

    for line in text.splitlines():
        if m := _EXCEPTION_TYPE_RE.match(line):
            log.exception_type = m.group(1)
            continue
        if m := _EXCEPTION_SUBTYPE_RE.match(line):
            log.exception_subtype = m.group(1)
            continue
        if m := _TERMINATION_DESCRIPTION_RE.match(line):
            # Some crash types (watchdog kills, jetsam, etc.) carry their
            # explanation here instead of in Exception Subtype. Only use
            # it as a fallback so we don't clobber a real subtype if both
            # happen to be present.
            if not log.exception_subtype:
                log.exception_subtype = m.group(1)
            continue
        if m := _THREAD_HEADER_RE.match(line):
            crashed = m.group(2) is not None
            current_thread = RawThread(
                label=f"Thread {m.group(1)}" + (" Crashed" if crashed else ""),
                crashed=crashed,
            )
            log.threads.append(current_thread)
            continue
        if current_thread is not None and (m := _FRAME_LINE_RE.match(line)):
            current_thread.frames.append(RawFrame(
                index=int(m.group(1)),
                binary_name=m.group(2),
                address=int(m.group(3), 16),
                load_address=int(m.group(4), 16),
                offset=int(m.group(5)),
            ))
            continue

    return log


def crashed_thread(log: RawCrashLog) -> RawThread:
    """Returns the thread flagged as crashed, or the first thread if none
    is explicitly flagged."""
    for t in log.threads:
        if t.crashed:
            return t
    if not log.threads:
        raise SymbolicationError(
            "no threads found -- crash log format not recognized "
            "(expected 'Thread N Crashed:' style headers)"
        )
    return log.threads[0]


# ---------------------------------------------------------------------
# Modern .ips (JSON) crash report parsing
#
# Real .ips files (the format macOS/iOS have generated by default since
# roughly macOS 12 / iOS 15) are two JSON objects separated by a
# newline: a small metadata header line, then the full report. The
# fields used here (threads[].frames[].imageOffset/imageIndex,
# threads[].triggered, usedImages[].base/name, exception.type/signal)
# match Apple's documented schema and real-world samples -- see
# developer.apple.com/documentation/xcode/interpreting-the-json-format-of-a-crash-report.
#
# Unlike the legacy text format, .ips frames don't carry an absolute
# runtime address directly -- they give `imageOffset` (offset from that
# binary's load address) plus `imageIndex` (which entry in usedImages
# this frame belongs to). We reconstruct the equivalent of the legacy
# format's (address, load_address, offset) trio so the rest of the
# pipeline -- symbolicate_crash_log, atos invocation -- doesn't need to
# know which input format was used.
# ---------------------------------------------------------------------

def _looks_like_ips_json(text: str) -> bool:
    """Cheap format sniff: real .ips files start with a JSON object
    (possibly after leading whitespace) on the first line. The legacy
    text format always starts with plain text like 'Process:' or
    'Incident Identifier:'."""
    stripped = text.lstrip()
    return stripped.startswith("{")


def parse_ips_json(text: str) -> RawCrashLog:
    """Parses a real .ips crash report (JSON format). Handles both the
    two-line form (metadata header + full report, the on-disk format in
    ~/Library/Logs/DiagnosticReports/) and a bare single-JSON-object form
    (e.g. if someone pastes just the second line)."""
    import json

    lines = [l for l in text.split("\n", 1)]
    report: dict | None = None

    if len(lines) == 2:
        # Two-line form: try parsing the second line as the full report.
        # If that fails, fall through and try the whole text as one
        # object (covers the case where the "header" line we split on
        # was actually still part of a single JSON document, e.g. if it
        # contained an embedded newline inside a string value).
        try:
            report = json.loads(lines[1])
        except json.JSONDecodeError:
            report = None

    if report is None:
        try:
            report = json.loads(text)
        except json.JSONDecodeError as e:
            raise SymbolicationError(
                f"input looks like JSON but failed to parse as a .ips crash "
                f"report: {e}. If this is the legacy text-format .crash file, "
                f"this function shouldn't have been called -- that's a bug in "
                f"format detection, please report it."
            ) from e

    if "threads" not in report:
        raise SymbolicationError(
            "parsed JSON successfully but it doesn't look like a .ips crash "
            "report -- no 'threads' field found. Got top-level keys: "
            f"{sorted(report.keys())}"
        )

    used_images = report.get("usedImages", [])
    if not used_images:
        raise SymbolicationError(
            "ips report has no 'usedImages' array -- cannot resolve which "
            "binary each frame belongs to"
        )

    log = RawCrashLog()

    exception = report.get("exception", {})
    exc_type = exception.get("type", "")
    exc_signal = exception.get("signal")
    log.exception_type = f"{exc_type} ({exc_signal})" if exc_signal else exc_type

    # Subtype: prefer a human-readable fatal-error message if present
    # (found under "asi", keyed by the dylib that logged it -- Swift's
    # runtime logs fatal errors here), falling back to the raw
    # exception subtype/codes, then a termination description.
    asi = report.get("asi", {})
    asi_messages = [msg for msgs in asi.values() for msg in msgs] if isinstance(asi, dict) else []
    if asi_messages:
        log.exception_subtype = asi_messages[0]
    elif exception.get("subtype"):
        log.exception_subtype = exception["subtype"]
    elif exception.get("codes"):
        log.exception_subtype = exception["codes"]
    else:
        termination = report.get("termination", {})
        if termination.get("indicator"):
            log.exception_subtype = termination["indicator"]

    faulting_thread_index = report.get("faultingThread")

    for i, thread in enumerate(report["threads"]):
        is_crashed = bool(thread.get("triggered")) or (faulting_thread_index == i)
        name = thread.get("name")
        label = f"Thread {i}" + (f" ({name})" if name else "") + (" Crashed" if is_crashed else "")

        raw_thread = RawThread(label=label, crashed=is_crashed)

        for frame_index, frame in enumerate(thread.get("frames", [])):
            image_index = frame.get("imageIndex")
            image_offset = frame.get("imageOffset")
            if image_index is None or image_offset is None:
                continue  # malformed frame entry; skip rather than crash the whole parse
            if image_index >= len(used_images):
                continue

            image = used_images[image_index]
            load_address = image.get("base", 0)
            binary_name = image.get("name") or os.path.basename(image.get("path", "")) or "<unknown>"

            raw_thread.frames.append(RawFrame(
                index=frame_index,
                binary_name=binary_name,
                address=load_address + image_offset,
                load_address=load_address,
                offset=image_offset,
            ))

        log.threads.append(raw_thread)

    return log


def parse_any_format(text: str) -> RawCrashLog:
    """Detects whether `text` is a modern .ips JSON crash report or the
    legacy plain-text .crash format, and parses accordingly. This is
    the entry point symbolicate_crash_log uses -- callers don't need to
    know or care which format they have."""
    if _looks_like_ips_json(text):
        return parse_ips_json(text)
    return parse_crash_log_text(text)


# ---------------------------------------------------------------------
# atos invocation
# ---------------------------------------------------------------------

# Matches atos output like:
#   -[ViewController loadData] (in MyApp) (ViewController.swift:42)
# or, for unresolved addresses:
#   0x0000000104a3c200
_ATOS_RESULT_RE = re.compile(
    r"^(?P<symbol>.+?)\s+\(in\s+(?P<binary>[^)]+)\)(?:\s+\((?P<file>[^:]+):(?P<line>\d+)\))?$"
)


def atos_available() -> bool:
    return shutil.which("atos") is not None


def run_atos(binary_path: str, load_address: int, addresses: list[int]) -> list[str]:
    """Calls `atos -o <binary> -l <load_address> <addr1> <addr2> ...` and
    returns the raw output lines, one per input address, in order.

    -l is the binary's *runtime load address* for this particular crash
    (taken from the crash log's frame line), which atos uses to compute
    the slide internally -- same ASLR-correction concept as the old C++
    engine's `slide` parameter, just handled for us by Apple's tool.
    """
    if not atos_available():
        raise SymbolicationError(
            "`atos` was not found on PATH. It ships with Xcode Command Line "
            "Tools -- install with `xcode-select --install`, or open Xcode "
            "once to trigger the install. atos is macOS-only; this service "
            "must run on the Mac that has the crashed app's binary."
        )

    cmd = ["atos", "-o", binary_path, "-l", hex(load_address)] + [hex(a) for a in addresses]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired as e:
        raise SymbolicationError(f"atos timed out after 30s running: {' '.join(cmd)}") from e
    except FileNotFoundError as e:
        raise SymbolicationError(f"could not execute atos: {e}") from e

    if result.returncode != 0:
        raise SymbolicationError(
            f"atos exited with code {result.returncode}.\n"
            f"stderr: {result.stderr.strip()}\n"
            f"command: {' '.join(cmd)}\n"
            f"(common cause: binary_path doesn't have debug symbols -- atos "
            f"needs either an unstripped binary or its companion .dSYM "
            f"sitting alongside/spotlight-indexed so it can find it)"
        )

    lines = result.stdout.splitlines()
    if len(lines) != len(addresses):
        raise SymbolicationError(
            f"atos returned {len(lines)} lines for {len(addresses)} addresses -- "
            f"output didn't parse 1:1 as expected. Raw output:\n{result.stdout}"
        )
    return lines


def parse_atos_line(raw_line: str) -> tuple[str | None, str | None, int | None]:
    """Parses one line of atos output into (symbol, file, line).
    Returns (None, None, None) if atos couldn't resolve the address at
    all (it then just echoes the hex address back)."""
    raw_line = raw_line.strip()
    if raw_line.startswith("0x"):
        return None, None, None

    m = _ATOS_RESULT_RE.match(raw_line)
    if not m:
        # atos produced *something* resolved but in a shape we didn't
        # anticipate -- better to surface the symbol name alone than
        # silently drop the line.
        return raw_line, None, None

    symbol = m.group("symbol")
    file_path = m.group("file")
    line_no = int(m.group("line")) if m.group("line") else None
    return symbol, file_path, line_no


def symbolicate_crash_log(
    crash_log_text: str,
    binary_path: str,
    target_binary_name: str | None = None,
) -> SymbolicatedCrash:
    """End-to-end: parse the crash log text, find the crashed thread,
    symbolicate every frame belonging to `target_binary_name` (or the
    binary's own filename if not given) via atos, and leave frames from
    other binaries (system frameworks etc.) as raw name/address -- same
    scoping decision as the original C++ engine: symbolicate *your*
    code, not all of UIKit.
    """
    log = parse_any_format(crash_log_text)
    thread = crashed_thread(log)

    binary_basename = target_binary_name or os.path.basename(binary_path)

    own_frames = [f for f in thread.frames if f.binary_name == binary_basename]
    other_frames = {f.index: f for f in thread.frames if f.binary_name != binary_basename}

    if not own_frames:
        all_binary_names = sorted({f.binary_name for f in thread.frames})
        raise SymbolicationError(
            f"no frames in the crashed thread matched binary name '{binary_basename}' "
            f"(derived from binary_path='{binary_path}'). Binary names actually present "
            f"in this crash log: {all_binary_names}. Pass target_binary_name explicitly "
            f"if the binary on disk has a different name than what appears in the crash "
            f"log (e.g. you renamed the build product)."
        )

    symbolicated: dict[int, SymbolicatedFrame] = {}

    if own_frames:
        # Group by load_address since atos takes one -l per invocation;
        # in practice all frames from the same binary share one load
        # address within a single crash log, but we handle the general
        # case defensively rather than assuming.
        by_load_addr: dict[int, list[RawFrame]] = {}
        for f in own_frames:
            by_load_addr.setdefault(f.load_address, []).append(f)

        for load_addr, frames in by_load_addr.items():
            addresses = [f.address for f in frames]
            atos_lines = run_atos(binary_path, load_addr, addresses)
            for raw_frame, atos_line in zip(frames, atos_lines):
                symbol, file_path, line_no = parse_atos_line(atos_line)
                symbolicated[raw_frame.index] = SymbolicatedFrame(
                    index=raw_frame.index,
                    address=raw_frame.address,
                    binary_name=raw_frame.binary_name,
                    symbol_name=symbol,
                    file_path=file_path,
                    line_number=line_no,
                )

    for idx, f in other_frames.items():
        symbolicated[idx] = SymbolicatedFrame(
            index=f.index,
            address=f.address,
            binary_name=f.binary_name,
            symbol_name=None,
            file_path=None,
            line_number=None,
        )

    ordered_frames = [symbolicated[f.index] for f in thread.frames if f.index in symbolicated]

    return SymbolicatedCrash(
        thread_label=thread.label,
        exception_type=log.exception_type,
        exception_subtype=log.exception_subtype,
        frames=ordered_frames,
    )
