"""
tests/test_agent_loop.py - Run: python3 tests/test_agent_loop.py

Tests the agent loop's control flow using a fake Groq client so no
real API calls are made.
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_loop import diagnose, MAX_STEPS
from agent_tools import AgentToolRunner
from groq_client import GroqMessage, GroqClientError
from symbolication import SymbolicatedCrash, SymbolicatedFrame

FIXTURE_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "sample_codebase")


class FakeGroqClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat(self, messages, tools=None):
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("FakeGroqClient ran out of scripted responses")
        return self._responses.pop(0)


class RaisingGroqClient:
    def __init__(self, error):
        self._error = error

    async def chat(self, messages, tools=None):
        raise self._error


def make_crash():
    return SymbolicatedCrash(
        thread_label="Thread 0 Crashed",
        exception_type="EXC_BREAKPOINT (SIGTRAP)",
        exception_subtype="Fatal error: nil unwrap",
        frames=[SymbolicatedFrame(index=0, address=0x104a3c200, binary_name="MyApp",
                                  symbol_name="ViewController.loadData()",
                                  file_path="Sources/ViewController.swift", line_number=11)],
    )


def make_tool_call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


async def run_diagnose(groq_client, tool_runner=None):
    tool_runner = tool_runner or AgentToolRunner(FIXTURE_ROOT)
    steps = []
    async for step in diagnose(make_crash(), groq_client, tool_runner):
        steps.append(step)
    return steps


async def test_single_tool_call_then_final_answer():
    fake = FakeGroqClient([
        GroqMessage.assistant(content="Let me check.", tool_calls=[
            make_tool_call("c1", "read_file", {"path": "Sources/ViewController.swift"})]),
        GroqMessage.assistant(content="ROOT CAUSE: force-unwrap.", tool_calls=None),
    ])
    steps = await run_diagnose(fake)
    types = [s.type for s in steps]
    assert types == ["thinking", "tool_call", "tool_result", "final_answer"], types
    assert steps[1].tool_name == "read_file"
    assert "ViewController.swift" in steps[2].tool_result
    tool_msgs = [m for m in fake.calls[1] if m.role == "tool"]
    assert len(tool_msgs) == 1
    print("PASS: test_single_tool_call_then_final_answer")


async def test_multi_step_investigation():
    fake = FakeGroqClient([
        GroqMessage.assistant(content=None, tool_calls=[make_tool_call("c1", "read_file", {"path": "Sources/ViewController.swift"})]),
        GroqMessage.assistant(content="Now searching.", tool_calls=[make_tool_call("c2", "search_codebase", {"query": "self?.data ="})]),
        GroqMessage.assistant(content="ROOT CAUSE: race condition.", tool_calls=None),
    ])
    steps = await run_diagnose(fake)
    types = [s.type for s in steps]
    assert types == ["tool_call", "tool_result", "thinking", "tool_call", "tool_result", "final_answer"], types
    tool_msgs = [m for m in fake.calls[2] if m.role == "tool"]
    assert len(tool_msgs) == 2
    print("PASS: test_multi_step_investigation")


async def test_immediate_final_answer():
    fake = FakeGroqClient([GroqMessage.assistant(content="ROOT CAUSE: obvious.", tool_calls=None)])
    steps = await run_diagnose(fake)
    assert len(steps) == 1 and steps[0].type == "final_answer"
    print("PASS: test_immediate_final_answer")


async def test_max_steps_cap():
    responses = [
        GroqMessage.assistant(content=None, tool_calls=[make_tool_call(f"c{i}", "read_file", {"path": "Sources/ViewController.swift"})])
        for i in range(MAX_STEPS + 2)
    ]
    steps = await run_diagnose(FakeGroqClient(responses))
    error_steps = [s for s in steps if s.type == "error"]
    assert len(error_steps) == 1
    assert "maximum" in error_steps[0].text
    assert len([s for s in steps if s.type == "tool_call"]) == MAX_STEPS
    print("PASS: test_max_steps_cap")


async def test_groq_error_surfaces_as_error_step():
    steps = await run_diagnose(RaisingGroqClient(GroqClientError("simulated failure")))
    assert len(steps) == 1 and steps[0].type == "error"
    assert "simulated failure" in steps[0].text
    print("PASS: test_groq_error_surfaces_as_error_step")


async def test_unknown_tool_does_not_crash_loop():
    fake = FakeGroqClient([
        GroqMessage.assistant(content=None, tool_calls=[make_tool_call("c1", "delete_everything", {})]),
        GroqMessage.assistant(content="ROOT CAUSE: doesn't matter.", tool_calls=None),
    ])
    steps = await run_diagnose(fake)
    tool_result = next(s for s in steps if s.type == "tool_result")
    assert "unknown tool" in tool_result.tool_result
    assert steps[-1].type == "final_answer"
    print("PASS: test_unknown_tool_does_not_crash_loop")


async def test_propose_fix_emits_fix_proposed_step():
    original = open(os.path.join(FIXTURE_ROOT, "Sources/ViewController.swift")).read()
    new_content = original.replace("data!.first", "data?.first")
    fake = FakeGroqClient([
        GroqMessage.assistant(content=None, tool_calls=[make_tool_call("c1", "propose_fix", {
            "path": "Sources/ViewController.swift",
            "new_content": new_content,
            "explanation": "Use optional chaining.",
        })]),
        GroqMessage.assistant(content="ROOT CAUSE: force-unwrap. Fix proposed.", tool_calls=None),
    ])
    steps = await run_diagnose(fake)
    types = [s.type for s in steps]
    assert types == ["tool_call", "tool_result", "fix_proposed", "final_answer"], types
    fix = steps[2]
    assert fix.fix_path == "Sources/ViewController.swift"
    assert "data?.first" in fix.fix_diff
    assert fix.fix_new_content == new_content
    print("PASS: test_propose_fix_emits_fix_proposed_step")


async def run_all():
    tests = [
        test_single_tool_call_then_final_answer,
        test_multi_step_investigation,
        test_immediate_final_answer,
        test_max_steps_cap,
        test_groq_error_surfaces_as_error_step,
        test_unknown_tool_does_not_crash_loop,
        test_propose_fix_emits_fix_proposed_step,
    ]
    failed = 0
    for t in tests:
        try:
            await t()
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'='*40}\n{len(tests)-failed} passed, {failed} failed\n{'='*40}")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run_all()) else 1)
