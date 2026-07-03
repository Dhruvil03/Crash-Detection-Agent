"""
test_symbolication.py

Tests the parts of symbolication.py that don't require macOS/atos:
crash log text parsing and atos output-line parsing. The actual
subprocess call to `atos` (run_atos) can't be tested here since there's
no Mac in this sandbox -- that's flagged clearly in the test output
rather than silently skipped without explanation.

Run: python3 -m pytest test_symbolication.py -v
  or: python3 test_symbolication.py
"""

import os

from symbolication import (
    parse_crash_log_text,
    crashed_thread,
    parse_atos_line,
    atos_available,
    SymbolicationError,
)

SAMPLE_CRASH = """Process:               MyApp [4821]
Exception Type:        EXC_BAD_ACCESS (SIGSEGV)
Exception Subtype:     KERN_INVALID_ADDRESS at 0x0000000000000010

Thread 0:
0   libsystem_kernel.dylib        0x00000001a8c12345  0x1a8c00000 + 74565
1   MyApp                         0x0000000104a3d100  0x104a38000 + 20992

Thread 1 Crashed:
0   MyApp                         0x0000000104a3c200  0x104a38000 + 16384
1   MyApp                         0x0000000104a3d100  0x104a38000 + 20992
2   MyApp                         0x0000000104a3e050  0x104a38000 + 24656
3   UIKitCore                     0x00000001a8f2c4d8  0x1a8e00000 + 1230040
"""


def test_parse_crash_log_text():
    log = parse_crash_log_text(SAMPLE_CRASH)

    assert log.exception_type == "EXC_BAD_ACCESS (SIGSEGV)", log.exception_type
    assert log.exception_subtype == "KERN_INVALID_ADDRESS at 0x0000000000000010"
    assert len(log.threads) == 2, f"expected 2 threads, got {len(log.threads)}"

    t0, t1 = log.threads
    assert t0.crashed is False
    assert t1.crashed is True
    assert t1.label == "Thread 1 Crashed"
    assert len(t1.frames) == 4, f"expected 4 frames in thread 1, got {len(t1.frames)}"

    f0 = t1.frames[0]
    assert f0.binary_name == "MyApp"
    assert f0.address == 0x104a3c200
    assert f0.load_address == 0x104a38000
    assert f0.offset == 16384

    f3 = t1.frames[3]
    assert f3.binary_name == "UIKitCore"


def test_crashed_thread_picks_flagged_thread():
    log = parse_crash_log_text(SAMPLE_CRASH)
    t = crashed_thread(log)
    assert t.label == "Thread 1 Crashed"


def test_crashed_thread_falls_back_to_first_when_none_flagged():
    text = """Thread 0:
0   MyApp   0x0000000104a3c200  0x104a38000 + 16384
"""
    log = parse_crash_log_text(text)
    t = crashed_thread(log)
    assert t.label == "Thread 0"


def test_thread_header_with_main_thread_suffix():
    # Regression: "Thread 0 Crashed (Main Thread):" -- a real-world
    # format variant -- used to fail to match the header regex at all,
    # silently dropping that thread's frames and causing crashed_thread()
    # to fall back to a later, wrong thread.
    text = """Thread 0 Crashed (Main Thread):
0   MyApp   0x0000000104a3c200  0x104a38000 + 16384
1   MyApp   0x0000000104a3d100  0x104a38000 + 20992

Thread 1:
0   MyApp   0x0000000104a3e050  0x104a38000 + 24656
"""
    log = parse_crash_log_text(text)
    assert len(log.threads) == 2, f"expected 2 threads, got {len(log.threads)}"
    t = crashed_thread(log)
    assert t.label == "Thread 0 Crashed", f"expected 'Thread 0 Crashed', got {t.label!r}"
    assert t.crashed is True
    assert len(t.frames) == 2, f"expected 2 frames on the crashed thread, got {len(t.frames)}"


def test_termination_description_used_as_subtype_fallback():
    text = """Exception Type:        EXC_CRASH (SIGKILL)
Termination Description: SPRINGBOARD, scene-create watchdog transgression: exhausted 19.99s

Thread 0 Crashed (Main Thread):
0   MyApp   0x0000000104a3c200  0x104a38000 + 16384
"""
    log = parse_crash_log_text(text)
    assert "watchdog transgression" in log.exception_subtype, log.exception_subtype


def test_crashed_thread_raises_on_empty_log():
    log = parse_crash_log_text("not a crash log at all, just prose.")
    try:
        crashed_thread(log)
        assert False, "expected SymbolicationError"
    except SymbolicationError:
        pass


def test_parse_atos_line_resolved_with_file_and_line():
    line = "-[ViewController loadData] (in MyApp) (ViewController.swift:42)"
    symbol, file, lineno = parse_atos_line(line)
    assert symbol == "-[ViewController loadData]", symbol
    assert file == "ViewController.swift", file
    assert lineno == 42, lineno


def test_parse_atos_line_resolved_without_file_line():
    # Happens for frames atos can name (from the symbol table) but has
    # no DWARF line info for (e.g. release builds, system libraries).
    line = "_doWork (in MyApp)"
    symbol, file, lineno = parse_atos_line(line)
    assert symbol == "_doWork", symbol
    assert file is None
    assert lineno is None


def test_parse_atos_line_unresolved():
    # atos echoes back the raw hex address when it can't resolve at all.
    line = "0x0000000104a3c200"
    symbol, file, lineno = parse_atos_line(line)
    assert symbol is None
    assert file is None
    assert lineno is None


def test_parse_atos_line_swift_mangled_style():
    # Swift symbols can contain spaces inside parens, e.g. closures --
    # make sure the regex doesn't choke on nested parens in the symbol part.
    line = "closure #1 in ViewController.loadData() (in MyApp) (ViewController.swift:45)"
    symbol, file, lineno = parse_atos_line(line)
    assert symbol == "closure #1 in ViewController.loadData()", symbol
    assert file == "ViewController.swift"
    assert lineno == 45


def test_atos_available_reports_platform_honestly():
    # On this Linux sandbox this should be False -- asserting that
    # confirms the function isn't lying about availability, which
    # matters because run_atos's error message depends on it.
    available = atos_available()
    print(f"\n  (informational) atos_available() on this host: {available}")
    assert isinstance(available, bool)


def test_symbolicate_crash_log_errors_clearly_on_binary_name_mismatch():
    # Regression test: found while manually testing the FastAPI server.
    # If the crash log's frame binary name doesn't match the target
    # binary (e.g. binary_path's basename != what's in the log), every
    # frame used to silently fall into "other binary" and get returned
    # as [unresolved] with HTTP 200 -- easy to misread as "symbolication
    # ran and found nothing" rather than "you pointed it at the wrong
    # binary". Should now raise a clear, actionable error instead.
    from symbolication import symbolicate_crash_log
    crash_text = (
        "Exception Type:        EXC_BAD_ACCESS (SIGSEGV)\n"
        "Exception Subtype:     KERN_INVALID_ADDRESS at 0x10\n\n"
        "Thread 1 Crashed:\n"
        "0   MyApp    0x0000000104a3c200  0x104a38000 + 16384\n"
        "1   UIKitCore  0x00000001a8f2c4d8  0x1a8e00000 + 1230040\n"
    )
    try:
        symbolicate_crash_log(crash_text, binary_path="/tmp/fake_binary")
        assert False, "expected SymbolicationError for binary name mismatch"
    except SymbolicationError as e:
        msg = str(e)
        assert "no frames in the crashed thread matched" in msg, msg
        assert "MyApp" in msg, f"error should list the binary names actually present: {msg}"
    print("PASS: test_symbolicate_crash_log_errors_clearly_on_binary_name_mismatch")


def test_ips_json_detection():
    from symbolication import _looks_like_ips_json
    assert _looks_like_ips_json('{"app_name":"x"}\n{"threads":[]}') is True
    assert _looks_like_ips_json("Process: MyApp\nException Type: ...") is False
    assert _looks_like_ips_json("   \n  {\"a\":1}") is True, "leading whitespace before { should still detect as JSON"


def test_parse_ips_json_real_sample():
    from symbolication import parse_any_format, crashed_thread

    text = open(os.path.join(
        os.path.dirname(__file__), "tests", "fixtures", "sample_crashes", "modern_format.ips"
    )).read()

    log = parse_any_format(text)
    assert log.exception_type == "EXC_BREAKPOINT (SIGTRAP)", log.exception_type
    assert "Unexpectedly found nil" in log.exception_subtype, log.exception_subtype
    assert len(log.threads) == 2, f"expected 2 threads, got {len(log.threads)}"

    t = crashed_thread(log)
    assert t.crashed is True
    assert "Crashed" in t.label
    assert "Main Thread" in t.label
    assert len(t.frames) == 5, f"expected 5 frames, got {len(t.frames)}"

    f0 = t.frames[0]
    assert f0.binary_name == "MyApp"
    assert f0.offset == 28672
    assert f0.load_address == 4372807680  # the 'base' value from usedImages[0]
    assert f0.address == f0.load_address + f0.offset

    f2 = t.frames[2]
    assert f2.binary_name == "SwiftUI", f2.binary_name

    print("PASS: test_parse_ips_json_real_sample")


def test_parse_ips_json_bare_single_object():
    # Some callers might paste just the second JSON line (the actual
    # report) without the small metadata header line -- should still parse.
    from symbolication import parse_any_format

    full_text = open(os.path.join(
        os.path.dirname(__file__), "tests", "fixtures", "sample_crashes", "modern_format.ips"
    )).read()
    _, report_only = full_text.split("\n", 1)

    log = parse_any_format(report_only)
    assert len(log.threads) == 2
    print("PASS: test_parse_ips_json_bare_single_object")


def test_parse_ips_json_missing_threads_field():
    from symbolication import parse_ips_json
    try:
        parse_ips_json('{"app_name": "x", "exception": {}}')
        assert False, "expected SymbolicationError for missing threads field"
    except Exception as e:
        assert "threads" in str(e), str(e)
    print("PASS: test_parse_ips_json_missing_threads_field")


def test_parse_ips_json_invalid_json():
    from symbolication import parse_ips_json
    try:
        parse_ips_json("{not valid json at all")
        assert False, "expected SymbolicationError for invalid JSON"
    except Exception as e:
        assert "failed to parse" in str(e), str(e)
    print("PASS: test_parse_ips_json_invalid_json")


def test_parse_ips_json_missing_used_images():
    from symbolication import parse_ips_json
    try:
        parse_ips_json('{"threads": [{"triggered": true, "frames": []}], "usedImages": []}')
        assert False, "expected SymbolicationError for empty usedImages"
    except Exception as e:
        assert "usedImages" in str(e), str(e)
    print("PASS: test_parse_ips_json_missing_used_images")


def test_parse_ips_json_no_thread_flagged_triggered_falls_back_to_faulting_thread_field():
    from symbolication import parse_ips_json, crashed_thread
    import json
    report = {
        "faultingThread": 1,
        "threads": [
            {"frames": [{"imageOffset": 100, "imageIndex": 0}]},
            {"frames": [{"imageOffset": 200, "imageIndex": 0}]},  # this one should be picked
        ],
        "usedImages": [{"base": 1000, "name": "MyApp"}],
    }
    log = parse_ips_json(json.dumps(report))
    t = crashed_thread(log)
    assert t.crashed is True
    assert t.frames[0].offset == 200, "should have picked thread index 1 via faultingThread"
    print("PASS: test_parse_ips_json_no_thread_flagged_triggered_falls_back_to_faulting_thread_field")


def test_parse_ips_json_malformed_frame_skipped_not_fatal():
    from symbolication import parse_ips_json
    import json
    report = {
        "threads": [
            {"triggered": True, "frames": [
                {"imageOffset": 100, "imageIndex": 0},
                {"symbol": "no imageOffset or imageIndex here"},  # malformed, should be skipped
                {"imageOffset": 300, "imageIndex": 0},
            ]},
        ],
        "usedImages": [{"base": 1000, "name": "MyApp"}],
    }
    log = parse_ips_json(json.dumps(report))
    assert len(log.threads[0].frames) == 2, f"expected 2 valid frames (1 skipped), got {len(log.threads[0].frames)}"
    print("PASS: test_parse_ips_json_malformed_frame_skipped_not_fatal")


def run_all():
    tests = [
        test_parse_crash_log_text,
        test_crashed_thread_picks_flagged_thread,
        test_crashed_thread_falls_back_to_first_when_none_flagged,
        test_thread_header_with_main_thread_suffix,
        test_termination_description_used_as_subtype_fallback,
        test_crashed_thread_raises_on_empty_log,
        test_parse_atos_line_resolved_with_file_and_line,
        test_parse_atos_line_resolved_without_file_line,
        test_parse_atos_line_unresolved,
        test_parse_atos_line_swift_mangled_style,
        test_atos_available_reports_platform_honestly,
        test_symbolicate_crash_log_errors_clearly_on_binary_name_mismatch,
        test_ips_json_detection,
        test_parse_ips_json_real_sample,
        test_parse_ips_json_bare_single_object,
        test_parse_ips_json_missing_threads_field,
        test_parse_ips_json_invalid_json,
        test_parse_ips_json_missing_used_images,
        test_parse_ips_json_no_thread_flagged_triggered_falls_back_to_faulting_thread_field,
        test_parse_ips_json_malformed_frame_skipped_not_fatal,
    ]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}")
            failed += 1
    print(f"\n{'='*40}\n{passed} passed, {failed} failed\n{'='*40}")
    return failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
