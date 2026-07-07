"""
agent_loop.py

The agent: gives the LLM context + tools, lets it call tools, feeds
results back, and repeats until a final answer. Async generator so
FastAPI can stream steps to the Swift UI as NDJSON.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import AsyncGenerator, Literal

from groq_client import GroqClient, GroqMessage, GroqClientError
from agent_tools import AgentToolRunner, ALL_TOOLS
from symbolication import SymbolicatedCrash

MAX_STEPS = 8

StepType = Literal["thinking", "tool_call", "tool_result", "fix_proposed", "final_answer", "error"]


@dataclass
class AgentStep:
    type: StepType
    text: str | None = None
    tool_name: str | None = None
    tool_arguments: str | None = None
    tool_result: str | None = None
    fix_path: str | None = None
    fix_diff: str | None = None
    fix_explanation: str | None = None
    fix_old_content: str | None = None
    fix_new_content: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


SYSTEM_PROMPT = """You are a senior iOS/macOS engineer diagnosing a crash. You will be \
given a symbolicated stack trace. Your job is to find the root cause and propose a \
concrete fix by actually reading the relevant code.

Investigation approach:
1. Look at the crashed frame -- the file and line number where the crash occurred.
2. Use read_file to look at the actual code around that line.
3. If the crash involves a variable that might be set elsewhere (e.g. a force-unwrap, \
race condition), use search_codebase to find where it's assigned.
4. Form a specific hypothesis about the root cause.
5. Once you know the fix, use propose_fix with the file's COMPLETE new content.

Be efficient: stop investigating once you have enough evidence to state a root cause.

When you give your final answer, structure it as:
ROOT CAUSE: <one or two sentences>
EVIDENCE: <what you found in the code>
SUGGESTED FIX: <brief summary; if you called propose_fix, say so here>
"""


def format_crash_for_prompt(crash: SymbolicatedCrash) -> str:
    lines = [
        "Crash report:",
        f"Thread: {crash.thread_label}",
        f"Exception: {crash.exception_type}" + (f" ({crash.exception_subtype})" if crash.exception_subtype else ""),
        "",
        "Symbolicated stack trace:",
    ]
    lines += [f.display_line for f in crash.frames]
    return "\n".join(lines)


async def diagnose(
    crash: SymbolicatedCrash,
    groq_client: GroqClient,
    tool_runner: AgentToolRunner,
) -> AsyncGenerator[AgentStep, None]:
    messages: list[GroqMessage] = [
        GroqMessage.system(SYSTEM_PROMPT),
        GroqMessage.user(format_crash_for_prompt(crash)),
    ]

    tool_schemas = [t.to_groq_schema() for t in ALL_TOOLS]

    for _ in range(MAX_STEPS):
        try:
            response = await groq_client.chat(messages=messages, tools=tool_schemas)
        except GroqClientError as e:
            yield AgentStep(type="error", text=str(e))
            return

        has_tool_calls = bool(response.tool_calls)

        if response.content:
            if has_tool_calls:
                yield AgentStep(type="thinking", text=response.content)
            else:
                yield AgentStep(type="final_answer", text=response.content)
                return

        if not has_tool_calls:
            if not response.content:
                yield AgentStep(type="error", text="Model returned an empty response.")
            return

        messages.append(GroqMessage.assistant(content=response.content, tool_calls=response.tool_calls))

        for call in response.tool_calls:
            fn = call["function"]
            name = fn["name"]
            args_json = fn["arguments"]

            yield AgentStep(type="tool_call", tool_name=name, tool_arguments=args_json)

            proposals_before = len(tool_runner.proposed_fixes)
            result = tool_runner.execute(tool_name=name, arguments_json=args_json)

            yield AgentStep(type="tool_result", tool_name=name, tool_result=result)

            if name == "propose_fix" and len(tool_runner.proposed_fixes) > proposals_before:
                fix = tool_runner.proposed_fixes[-1]
                yield AgentStep(
                    type="fix_proposed",
                    fix_path=fix.path,
                    fix_diff=fix.diff_text,
                    fix_explanation=fix.explanation,
                    fix_old_content=fix.old_content,
                    fix_new_content=fix.new_content,
                )

            messages.append(GroqMessage.tool_result(
                tool_call_id=call["id"],
                name=name,
                content=result,
            ))

    yield AgentStep(type="error", text=f"Reached the maximum number of investigation steps ({MAX_STEPS}).")
