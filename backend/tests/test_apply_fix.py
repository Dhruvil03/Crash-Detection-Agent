"""
tests/test_apply_fix.py - Run: python3 tests/test_apply_fix.py
"""
import os, sys, shutil, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent_tools import apply_fix, AppliedFixError


def _make(content: str):
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "Sources"))
    path = os.path.join(tmp, "Sources", "Test.swift")
    open(path, "w").write(content)
    return tmp, "Sources/Test.swift"


def test_writes_new_content_and_creates_backup():
    root, rel = _make("let x = 1\n")
    try:
        bak = apply_fix(root, rel, "let x = 1\n", "let x = 2\n")
        assert open(os.path.join(root, rel)).read() == "let x = 2\n"
        assert os.path.isfile(bak)
        assert open(bak).read() == "let x = 1\n"
        assert ".bak." in bak
    finally:
        shutil.rmtree(root)
    print("PASS: test_writes_new_content_and_creates_backup")


def test_refuses_stale_content():
    root, rel = _make("let x = 1\n")
    try:
        open(os.path.join(root, rel), "w").write("let x = 999\n")
        try:
            apply_fix(root, rel, "let x = 1\n", "let x = 2\n")
            assert False
        except AppliedFixError as e:
            assert "changed on disk" in str(e)
        assert open(os.path.join(root, rel)).read() == "let x = 999\n"
    finally:
        shutil.rmtree(root)
    print("PASS: test_refuses_stale_content")


def test_path_traversal_rejected():
    root, _ = _make("x\n")
    try:
        try:
            apply_fix(root, "../../../etc/passwd", "x\n", "hacked")
            assert False
        except AppliedFixError as e:
            assert "outside the allowed codebase root" in str(e)
    finally:
        shutil.rmtree(root)
    print("PASS: test_path_traversal_rejected")


def test_file_not_found():
    root, _ = _make("x\n")
    try:
        try:
            apply_fix(root, "Sources/DoesNotExist.swift", "x\n", "y\n")
            assert False
        except AppliedFixError as e:
            assert "not found" in str(e)
    finally:
        shutil.rmtree(root)
    print("PASS: test_file_not_found")


def test_distinct_backups_for_rapid_sequential_applies():
    root, rel = _make("v0\n")
    try:
        b1 = apply_fix(root, rel, "v0\n", "v1\n")
        b2 = apply_fix(root, rel, "v1\n", "v2\n")
        assert b1 != b2
        assert open(b1).read() == "v0\n"
        assert open(b2).read() == "v1\n"
        assert open(os.path.join(root, rel)).read() == "v2\n"
    finally:
        shutil.rmtree(root)
    print("PASS: test_distinct_backups_for_rapid_sequential_applies")


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
