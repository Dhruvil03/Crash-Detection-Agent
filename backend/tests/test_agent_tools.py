"""
test_agent_tools.py

Tests AgentToolRunner against a real fixture directory on disk --
exercising actual file I/O, not mocks, including the path-traversal
boundary check.
"""

import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_tools import AgentToolRunner, AgentToolError

FIXTURE_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "sample_codebase")


def test_read_file_full():
    runner = AgentToolRunner(FIXTURE_ROOT)
    result = runner.execute("read_file", json.dumps({"path": "Sources/ViewController.swift"}))
    assert "class ViewController" in result, result
    assert "1: import UIKit" in result, result
    print("PASS: test_read_file_full")


def test_read_file_line_range():
    runner = AgentToolRunner(FIXTURE_ROOT)
    result = runner.execute("read_file", json.dumps({
        "path": "Sources/ViewController.swift",
        "start_line": 10,
        "end_line": 13,
    }))
    assert "lines 10-13" in result, result
    assert "let first = data!.first" in result, result
    assert "import UIKit" not in result, "should not include line 1 when range is 10-13"
    print("PASS: test_read_file_line_range")


def test_read_file_not_found():
    runner = AgentToolRunner(FIXTURE_ROOT)
    result = runner.execute("read_file", json.dumps({"path": "Sources/DoesNotExist.swift"}))
    assert result.startswith("Error:"), result
    assert "not found" in result, result
    print("PASS: test_read_file_not_found")


def test_read_file_path_traversal_rejected():
    runner = AgentToolRunner(FIXTURE_ROOT)
    result = runner.execute("read_file", json.dumps({"path": "../../../../etc/passwd"}))
    assert result.startswith("Error:"), result
    assert "outside the allowed codebase root" in result, result
    print("PASS: test_read_file_path_traversal_rejected")


def test_path_boundary_check_rejects_sibling_directory():
    # Regression test for the exact bug we found and fixed in the Swift
    # version: a naive `candidate.startswith(root)` check would let
    # "<root>2/secret.txt" pass when root is "<root>". Build a sibling
    # directory with a name that's a superstring of the fixture root and
    # confirm it's correctly rejected.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "project")
        sibling = os.path.join(tmp, "project2")
        os.makedirs(root)
        os.makedirs(sibling)
        with open(os.path.join(sibling, "secret.txt"), "w") as f:
            f.write("should not be readable")

        runner = AgentToolRunner(root)
        # Relative path that resolves outside root via the sibling
        result = runner.execute("read_file", json.dumps({"path": "../project2/secret.txt"}))
        assert result.startswith("Error:"), f"sibling directory read should be rejected, got: {result}"
        assert "outside the allowed codebase root" in result, result
    print("PASS: test_path_boundary_check_rejects_sibling_directory")


def test_search_codebase_finds_match():
    runner = AgentToolRunner(FIXTURE_ROOT)
    result = runner.execute("search_codebase", json.dumps({"query": "self?.data ="}))
    assert "Found" in result, result
    assert "ViewController.swift" in result, result
    assert ":17:" in result or ":18:" in result, f"expected a line number near the assignment, got: {result}"
    print("PASS: test_search_codebase_finds_match")


def test_search_codebase_no_match():
    runner = AgentToolRunner(FIXTURE_ROOT)
    result = runner.execute("search_codebase", json.dumps({"query": "this_string_does_not_exist_anywhere"}))
    assert "No matches found" in result, result
    print("PASS: test_search_codebase_no_match")


def test_search_codebase_extension_filter():
    runner = AgentToolRunner(FIXTURE_ROOT)
    result = runner.execute("search_codebase", json.dumps({"query": "class", "file_extension": "py"}))
    assert "No matches found" in result, result  # no .py files in fixture
    print("PASS: test_search_codebase_extension_filter")


def test_unknown_tool_returns_error_not_exception():
    runner = AgentToolRunner(FIXTURE_ROOT)
    result = runner.execute("delete_everything", "{}")
    assert result.startswith("Error:"), result
    assert "unknown tool" in result, result
    print("PASS: test_unknown_tool_returns_error_not_exception")


def test_malformed_json_arguments_returns_error_not_exception():
    runner = AgentToolRunner(FIXTURE_ROOT)
    result = runner.execute("read_file", "{not valid json")
    assert result.startswith("Error:"), result
    print("PASS: test_malformed_json_arguments_returns_error_not_exception")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'='*40}\n{len(tests)-failed} passed, {failed} failed\n{'='*40}")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
