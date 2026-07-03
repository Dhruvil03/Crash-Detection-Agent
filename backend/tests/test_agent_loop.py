"""
test_agent_loop.py

Tests the agent loop's control flow end-to-end using a FAKE GroqClient
that returns scripted responses instead of calling the real API. This
proves the multi-turn tool-calling protocol (assistant message with
tool_calls -> tool result messages -> next turn) is wired correctly,
without spending real API calls or needing network access.

Run: python3 tests/test_agent_loop.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_loop import diagnose, MAX_STEPS
from agent_tools import AgentToolRunner
from groq_client import GroqMessage, GroqClientError
from symbolication import SymbolicatedCrash, SymbolicatedFrame

FIXTURE_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "sample_codebase")


class FakeGroqClient:
    """Returns a pre-scripted sequence of responses, one per call to
    .chat(). Lets us test the agent loop's control flow deterministically
    without hitting the real Groq API."""

    def __init__(self, scripted_responses: list[GroqMessage]):
        self._responses = list(scripted_responses)
        self.calls: list[list[GroqMessage]] = []

    async def chat(self, messages, tools=None):
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("FakeGroqClient ran out of scripted responses -- agent looped more than expected")
        return self._responses.pop(0)


class RaisingGroqClient:
    """Always raises, to test the agent loop's error path."""
    def __init__(self, error: GroqClientError):
        self._error = error

    async def chat(self, messages, tools=None):
        raise self._error


def make_test_crash() -> SymbolicatedCrash:
    return SymbolicatedCrash(
        thread_label="Thread 1 Crashed",
        exception_type="EXC_BAD_ACCESS (SIGSEGV)",
        exception_subtype="KERN_INVALID_ADDRESS at 0x10",
        frames=[
            SymbolicatedFrame(
                index=0, address=0x104a3c200, binary_name="MyApp",
                symbol_name="ViewController.loadData()",
                file_path="Sources/ViewController.swift", line_number=11,
            ),
        ],
    )


def make_tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


async def run_diagnose(groq_client, tool_runner=None) -> list:
    tool_runner = tool_runner or AgentToolRunner(FIXTURE_ROOT)
    steps = []
    async for step in diagnose(make_test_crash(), groq_client, tool_runner):
        steps.append(step)
    return steps


async def test_single_tool_call_then_final_answer():
    fake = FakeGroqClient([
        GroqMessage.assistant(
            content="Let me look at the crash site.",
            tool_calls=[make_tool_call("call_1", "read_file", {
                "path": "Sources/ViewController.swift", "start_line": 1, "end_line": 20,
            })],
        ),
        GroqMessage.assistant(
            content="ROOT CAUSE: force-unwrap of nil optional.\nEVIDENCE: data is nil at line 11.\nSUGGESTED FIX: use guard let.",
            tool_calls=None,
        ),
    ])

    steps = await run_diagnose(fake)

    types = [s.type for s in steps]
    assert types == ["thinking", "tool_call", "tool_result", "final_answer"], types

    assert steps[1].tool_name == "read_file"
    assert "ViewController.swift" in steps[2].tool_result
    assert "ROOT CAUSE" in steps[3].text

    second_call_messages = fake.calls[1]
    tool_messages = [m for m in second_call_messages if m.role == "tool"]
    assert len(tool_messages) == 1, "expected exactly one tool result message in the second call's history"
    assert tool_messages[0].tool_call_id == "call_1"
    assert "ViewController.swift" in tool_messages[0].content

    print("PASS: test_single_tool_call_then_final_answer")


async def test_multi_step_investigation():
    fake = FakeGroqClient([
        GroqMessage.assistant(
            content=None,
            tool_calls=[make_tool_call("call_1", "read_file", {"path": "Sources/ViewController.swift"})],
        ),
        GroqMessage.assistant(
            content="Now let me check where data is set.",
            tool_calls=[make_tool_call("call_2", "search_codebase", {"query": "self?.data ="})],
        ),
        GroqMessage.assistant(
            content="ROOT CAUSE: race condition between async fetch and sync loadData.",
            tool_calls=None,
        ),
    ])

    steps = await run_diagnose(fake)
    types = [s.type for s in steps]
    assert types == ["tool_call", "tool_result", "thinking", "tool_call", "tool_result", "final_answer"], types

    assert steps[0].tool_name == "read_file"
    assert steps[3].tool_name == "search_codebase"
    assert "race condition" in steps[5].text

    third_call_messages = fake.calls[2]
    tool_messages = [m for m in third_call_messages if m.role == "tool"]
    assert len(tool_messages) == 2, f"expected 2 accumulated tool results, got {len(tool_messages)}"

    print("PASS: test_multi_step_investigation")


async def test_immediate_final_answer_no_tools():
    fake = FakeGroqClient([
        GroqMessage.assistant(content="ROOT CAUSE: obvious from the trace alone.", tool_calls=None),
    ])
    steps = await run_diagnose(fake)
    assert len(steps) == 1
    assert steps[0].type == "final_answer"
    print("PASS: test_immediate_final_answer_no_tools")


async def test_max_steps_cap_prevents_infinite_loop():
    responses = [
        GroqMessage.assistant(
            content=None,
            tool_calls=[make_tool_call(f"call_{i}", "read_file", {"path": "Sources/ViewController.swift"})],
        )
        for i in range(MAX_STEPS + 2)
    ]
    fake = FakeGroqClient(responses)

    steps = await run_diagnose(fake)

    error_steps = [s for s in steps if s.type == "error"]
    assert len(error_steps) == 1, f"expected exactly one error step (the cap message), got {len(error_steps)}"
    assert "maximum number of investigation steps" in error_steps[0].text

    tool_call_steps = [s for s in steps if s.type == "tool_call"]
    assert len(tool_call_steps) == MAX_STEPS, f"expected exactly {MAX_STEPS} tool calls before cutoff, got {len(tool_call_steps)}"

    print("PASS: test_max_steps_cap_prevents_infinite_loop")


async def test_groq_error_surfaces_as_error_step():
    raising = RaisingGroqClient(GroqClientError("simulated network failure"))
    steps = await run_diagnose(raising)
    assert len(steps) == 1
    assert steps[0].type == "error"
    assert "simulated network failure" in steps[0].text
    print("PASS: test_groq_error_surfaces_as_error_step")


async def test_unknown_tool_call_does_not_crash_loop():
    fake = FakeGroqClient([
        GroqMessage.assistant(
            content=None,
            tool_calls=[make_tool_call("call_1", "delete_the_codebase", {})],
        ),
        GroqMessage.assistant(content="I don't have that tool; based on the trace alone: ROOT CAUSE: ...", tool_calls=None),
    ])
    steps = await run_diagnose(fake)
    tool_result_step = next(s for s in steps if s.type == "tool_result")
    assert "unknown tool" in tool_result_step.tool_result
    assert steps[-1].type == "final_answer"
    print("PASS: test_unknown_tool_call_does_not_crash_loop")


async def run_all():
    tests = [
        test_single_tool_call_then_final_answer,
        test_multi_step_investigation,
        test_immediate_final_answer_no_tools,
        test_max_steps_cap_prevents_infinite_loop,
        test_groq_error_surfaces_as_error_step,
        test_unknown_tool_call_does_not_crash_loop,
    ]
    failed = 0
    for t in tests:
        try:
            await t()
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'='*40}\n{len(tests)-failed} passed, {failed} failed\n{'='*40}")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run_all()) else 1)
