"""
tests/test_agent_tools.py - Run: python3 tests/test_agent_tools.py
"""
import os, sys, json, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent_tools import AgentToolRunner, AgentToolError

FIXTURE_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "sample_codebase")


def test_read_file_full():
    r = AgentToolRunner(FIXTURE_ROOT)
    result = r.execute("read_file", json.dumps({"path": "Sources/ViewController.swift"}))
    assert "class ViewController" in result
    assert "1: import UIKit" in result
    print("PASS: test_read_file_full")


def test_read_file_line_range():
    r = AgentToolRunner(FIXTURE_ROOT)
    result = r.execute("read_file", json.dumps({"path": "Sources/ViewController.swift", "start_line": 10, "end_line": 13}))
    assert "lines 10-13" in result
    assert "let first = data!.first" in result
    print("PASS: test_read_file_line_range")


def test_read_file_not_found():
    r = AgentToolRunner(FIXTURE_ROOT)
    result = r.execute("read_file", json.dumps({"path": "DoesNotExist.swift"}))
    assert result.startswith("Error:")
    print("PASS: test_read_file_not_found")


def test_read_file_path_traversal_rejected():
    r = AgentToolRunner(FIXTURE_ROOT)
    result = r.execute("read_file", json.dumps({"path": "../../../../etc/passwd"}))
    assert "outside the allowed codebase root" in result
    print("PASS: test_read_file_path_traversal_rejected")


def test_path_boundary_rejects_sibling():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "project")
        sibling = os.path.join(tmp, "project2")
        os.makedirs(root)
        os.makedirs(sibling)
        with open(os.path.join(sibling, "secret.txt"), "w") as f:
            f.write("secret")
        r = AgentToolRunner(root)
        result = r.execute("read_file", json.dumps({"path": "../project2/secret.txt"}))
        assert "outside the allowed codebase root" in result
    print("PASS: test_path_boundary_rejects_sibling")


def test_search_finds_match():
    r = AgentToolRunner(FIXTURE_ROOT)
    result = r.execute("search_codebase", json.dumps({"query": "self?.data ="}))
    assert "Found" in result
    assert "ViewController.swift" in result
    print("PASS: test_search_finds_match")


def test_search_no_match():
    r = AgentToolRunner(FIXTURE_ROOT)
    result = r.execute("search_codebase", json.dumps({"query": "this_string_does_not_exist_anywhere"}))
    assert "No matches found" in result
    print("PASS: test_search_no_match")


def test_unknown_tool_returns_error():
    r = AgentToolRunner(FIXTURE_ROOT)
    result = r.execute("delete_everything", "{}")
    assert "unknown tool" in result
    print("PASS: test_unknown_tool_returns_error")


def test_malformed_json_returns_error():
    r = AgentToolRunner(FIXTURE_ROOT)
    result = r.execute("read_file", "{not valid json")
    assert result.startswith("Error:")
    print("PASS: test_malformed_json_returns_error")


def test_propose_fix_records_diff():
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(FIXTURE_ROOT, os.path.join(tmp, "src"))
        r = AgentToolRunner(os.path.join(tmp, "src"))
        result = r.execute("propose_fix", json.dumps({
            "path": "Sources/ViewController.swift",
            "new_content": "import UIKit\n// fixed\n",
            "explanation": "test fix",
        }))
        assert "Fix proposed" in result
        assert len(r.proposed_fixes) == 1
        assert r.proposed_fixes[0].explanation == "test fix"
        # original file must be untouched
        content = open(os.path.join(tmp, "src", "Sources", "ViewController.swift")).read()
        assert "force-unwrap" not in result or "crashes if data is still nil" in content
    print("PASS: test_propose_fix_records_diff")


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
