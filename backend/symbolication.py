"""
symbolication.py

Wraps Apple's `atos` command-line tool to do real crash symbolication.
Supports both the legacy plain-text .crash format and the modern .ips
JSON format (the default since macOS 12 / iOS 15).
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
    address: int
    load_address: int
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
# Legacy plain-text .crash format parsing
# ---------------------------------------------------------------------

_FRAME_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(\S+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s*\+\s*(\d+)"
)
_THREAD_HEADER_RE = re.compile(r"^Thread\s+(\d+)(\s+Crashed)?(?:\s*\([^)]*\))?:")
_EXCEPTION_TYPE_RE = re.compile(r"^Exception Type:\s*(.+)$")
_EXCEPTION_SUBTYPE_RE = re.compile(r"^Exception Subtype:\s*(.+)$")
_TERMINATION_DESCRIPTION_RE = re.compile(r"^Termination Description:\s*(.+)$")


def parse_crash_log_text(text: str) -> RawCrashLog:
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
# ---------------------------------------------------------------------

def _looks_like_ips_json(text: str) -> bool:
    return text.lstrip().startswith("{")


def parse_ips_json(text: str) -> RawCrashLog:
    import json

    lines = text.split("\n", 1)
    report: dict | None = None

    if len(lines) == 2:
        try:
            report = json.loads(lines[1])
        except json.JSONDecodeError:
            report = None

    if report is None:
        try:
            report = json.loads(text)
        except json.JSONDecodeError as e:
            raise SymbolicationError(
                f"input looks like JSON but failed to parse as a .ips crash report: {e}"
            ) from e

    if "threads" not in report:
        raise SymbolicationError(
            "parsed JSON successfully but no 'threads' field found. "
            f"Got top-level keys: {sorted(report.keys())}"
        )

    used_images = report.get("usedImages", [])
    if not used_images:
        raise SymbolicationError(
            "ips report has no 'usedImages' array -- cannot resolve binary names"
        )

    log = RawCrashLog()

    exception = report.get("exception", {})
    exc_type = exception.get("type", "")
    exc_signal = exception.get("signal")
    log.exception_type = f"{exc_type} ({exc_signal})" if exc_signal else exc_type

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
                continue
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
    """Detects .ips JSON vs legacy .crash text and parses accordingly."""
    if _looks_like_ips_json(text):
        return parse_ips_json(text)
    return parse_crash_log_text(text)


# ---------------------------------------------------------------------
# atos invocation
# ---------------------------------------------------------------------

_ATOS_RESULT_RE = re.compile(
    r"^(?P<symbol>.+?)\s+\(in\s+(?P<binary>[^)]+)\)(?:\s+\((?P<file>[^:]+):(?P<line>\d+)\))?$"
)


def atos_available() -> bool:
    return shutil.which("atos") is not None


def run_atos(binary_path: str, load_address: int, addresses: list[int]) -> list[str]:
    if not atos_available():
        raise SymbolicationError(
            "`atos` was not found on PATH. It ships with Xcode Command Line "
            "Tools -- install with `xcode-select --install`, or open Xcode "
            "once to trigger the install. atos is macOS-only."
        )

    cmd = ["atos", "-o", binary_path, "-l", hex(load_address)] + [hex(a) for a in addresses]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as e:
        raise SymbolicationError(f"atos timed out after 30s") from e
    except FileNotFoundError as e:
        raise SymbolicationError(f"could not execute atos: {e}") from e

    if result.returncode != 0:
        raise SymbolicationError(
            f"atos exited with code {result.returncode}.\n"
            f"stderr: {result.stderr.strip()}\n"
            f"(common cause: binary has no debug symbols -- use an unstripped Debug build)"
        )

    lines = result.stdout.splitlines()
    if len(lines) != len(addresses):
        raise SymbolicationError(
            f"atos returned {len(lines)} lines for {len(addresses)} addresses"
        )
    return lines


def parse_atos_line(raw_line: str) -> tuple[str | None, str | None, int | None]:
    raw_line = raw_line.strip()
    if raw_line.startswith("0x"):
        return None, None, None
    m = _ATOS_RESULT_RE.match(raw_line)
    if not m:
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
    log = parse_any_format(crash_log_text)
    thread = crashed_thread(log)

    binary_basename = target_binary_name or os.path.basename(binary_path)

    own_frames = [f for f in thread.frames if f.binary_name == binary_basename]
    other_frames = {f.index: f for f in thread.frames if f.binary_name != binary_basename}

    if not own_frames:
        all_binary_names = sorted({f.binary_name for f in thread.frames})
        raise SymbolicationError(
            f"no frames matched binary name '{binary_basename}'. "
            f"Binary names in this crash log: {all_binary_names}. "
            f"Pass target_binary_name explicitly if the binary filename differs."
        )

    symbolicated: dict[int, SymbolicatedFrame] = {}

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
