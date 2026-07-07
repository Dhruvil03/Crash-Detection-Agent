"""
test_symbolication.py - Run: python3 test_symbolication.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symbolication import (
    parse_crash_log_text, crashed_thread, parse_atos_line,
    parse_ips_json, parse_any_format, _looks_like_ips_json,
    atos_available, SymbolicationError,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures")

SAMPLE_CRASH = """Process:               MyApp [4821]
Exception Type:        EXC_BAD_ACCESS (SIGSEGV)
Exception Subtype:     KERN_INVALID_ADDRESS at 0x0000000000000010

Thread 0:
0   libsystem_kernel.dylib        0x00000001a8c12345  0x1a8c00000 + 74565
1   MyApp                         0x0000000104a3d100  0x104a38000 + 20992

Thread 1 Crashed:
0   MyApp                         0x0000000104a3c200  0x104a38000 + 16384
1   MyApp                         0x0000000104a3d100  0x104a38000 + 20992
2   UIKitCore                     0x00000001a8f2c4d8  0x1a8e00000 + 1230040
"""


def test_parse_crash_log_text():
    log = parse_crash_log_text(SAMPLE_CRASH)
    assert log.exception_type == "EXC_BAD_ACCESS (SIGSEGV)"
    assert log.exception_subtype == "KERN_INVALID_ADDRESS at 0x0000000000000010"
    assert len(log.threads) == 2
    assert log.threads[1].crashed is True
    assert len(log.threads[1].frames) == 3
    f0 = log.threads[1].frames[0]
    assert f0.binary_name == "MyApp"
    assert f0.address == 0x104a3c200
    assert f0.load_address == 0x104a38000
    print("PASS: test_parse_crash_log_text")


def test_crashed_thread_picks_flagged():
    log = parse_crash_log_text(SAMPLE_CRASH)
    t = crashed_thread(log)
    assert t.label == "Thread 1 Crashed"
    print("PASS: test_crashed_thread_picks_flagged")


def test_crashed_thread_falls_back_to_first():
    text = "Thread 0:\n0   MyApp   0x0000000104a3c200  0x104a38000 + 16384\n"
    log = parse_crash_log_text(text)
    t = crashed_thread(log)
    assert t.label == "Thread 0"
    print("PASS: test_crashed_thread_falls_back_to_first")


def test_thread_header_with_main_thread_suffix():
    text = "Thread 0 Crashed (Main Thread):\n0   MyApp   0x0000000104a3c200  0x104a38000 + 16384\n\nThread 1:\n0   MyApp   0x0000000104a3e050  0x104a38000 + 24656\n"
    log = parse_crash_log_text(text)
    assert len(log.threads) == 2
    t = crashed_thread(log)
    assert t.crashed is True
    assert len(t.frames) == 1
    print("PASS: test_thread_header_with_main_thread_suffix")


def test_termination_description_fallback():
    text = "Exception Type:        EXC_CRASH (SIGKILL)\nTermination Description: SPRINGBOARD, watchdog transgression: exhausted 19.99s\n\nThread 0 Crashed (Main Thread):\n0   MyApp   0x0000000104a3c200  0x104a38000 + 16384\n"
    log = parse_crash_log_text(text)
    assert "watchdog transgression" in log.exception_subtype
    print("PASS: test_termination_description_fallback")


def test_crashed_thread_raises_on_empty():
    log = parse_crash_log_text("not a crash log at all")
    try:
        crashed_thread(log)
        assert False
    except SymbolicationError:
        pass
    print("PASS: test_crashed_thread_raises_on_empty")


def test_parse_atos_line_with_file_and_line():
    s, f, l = parse_atos_line("-[ViewController loadData] (in MyApp) (ViewController.swift:42)")
    assert s == "-[ViewController loadData]"
    assert f == "ViewController.swift"
    assert l == 42
    print("PASS: test_parse_atos_line_with_file_and_line")


def test_parse_atos_line_unresolved():
    s, f, l = parse_atos_line("0x0000000104a3c200")
    assert s is None and f is None and l is None
    print("PASS: test_parse_atos_line_unresolved")


def test_parse_atos_line_swift_mangled():
    line = "closure #1 in ViewController.loadData() (in MyApp) (ViewController.swift:45)"
    s, f, l = parse_atos_line(line)
    assert "closure #1" in s
    assert f == "ViewController.swift"
    assert l == 45
    print("PASS: test_parse_atos_line_swift_mangled")


def test_atos_available_returns_bool():
    assert isinstance(atos_available(), bool)
    print(f"  (atos_available on this host: {atos_available()})")
    print("PASS: test_atos_available_returns_bool")


def test_symbolicate_errors_on_binary_name_mismatch():
    from symbolication import symbolicate_crash_log
    crash_text = (
        "Exception Type:        EXC_BAD_ACCESS (SIGSEGV)\n"
        "Exception Subtype:     KERN_INVALID_ADDRESS at 0x10\n\n"
        "Thread 1 Crashed:\n"
        "0   MyApp    0x0000000104a3c200  0x104a38000 + 16384\n"
    )
    try:
        symbolicate_crash_log(crash_text, binary_path="/tmp/fake_binary")
        assert False
    except SymbolicationError as e:
        assert "no frames matched" in str(e)
        assert "MyApp" in str(e)
    print("PASS: test_symbolicate_errors_on_binary_name_mismatch")


# .ips JSON tests

def test_ips_json_detection():
    assert _looks_like_ips_json('{"app_name":"x"}\n{"threads":[]}') is True
    assert _looks_like_ips_json("Process: MyApp\n") is False
    assert _looks_like_ips_json("   \n  {\"a\":1}") is True
    print("PASS: test_ips_json_detection")


def test_parse_ips_json_real_sample():
    text = open(os.path.join(FIXTURE_DIR, "sample_crashes", "modern_format.ips")).read()
    log = parse_any_format(text)
    assert log.exception_type == "EXC_BREAKPOINT (SIGTRAP)"
    assert "Unexpectedly found nil" in log.exception_subtype
    assert len(log.threads) == 2
    t = crashed_thread(log)
    assert t.crashed is True
    assert "Main Thread" in t.label
    assert len(t.frames) == 4
    assert t.frames[0].binary_name == "MyApp"
    assert t.frames[0].offset == 28672
    assert t.frames[0].address == t.frames[0].load_address + t.frames[0].offset
    print("PASS: test_parse_ips_json_real_sample")


def test_parse_ips_json_bare_single_object():
    full = open(os.path.join(FIXTURE_DIR, "sample_crashes", "modern_format.ips")).read()
    _, report_only = full.split("\n", 1)
    log = parse_any_format(report_only)
    assert len(log.threads) == 2
    print("PASS: test_parse_ips_json_bare_single_object")


def test_parse_ips_json_missing_threads():
    try:
        parse_ips_json('{"app_name": "x", "usedImages": [{"base":0,"name":"x"}]}')
        assert False
    except SymbolicationError as e:
        assert "threads" in str(e)
    print("PASS: test_parse_ips_json_missing_threads")


def test_parse_ips_json_invalid_json():
    try:
        parse_ips_json("{not valid json")
        assert False
    except SymbolicationError as e:
        assert "failed to parse" in str(e)
    print("PASS: test_parse_ips_json_invalid_json")


def test_parse_ips_json_malformed_frame_skipped():
    import json
    report = {
        "threads": [{"triggered": True, "frames": [
            {"imageOffset": 100, "imageIndex": 0},
            {"symbol": "no_imageOffset_here"},
            {"imageOffset": 300, "imageIndex": 0},
        ]}],
        "usedImages": [{"base": 1000, "name": "MyApp"}],
    }
    log = parse_ips_json(json.dumps(report))
    assert len(log.threads[0].frames) == 2
    print("PASS: test_parse_ips_json_malformed_frame_skipped")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'='*40}\n{len(tests)-failed} passed, {failed} failed\n{'='*40}")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
