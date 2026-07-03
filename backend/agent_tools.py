"""
agent_tools.py

Tool implementations the agent can call: read_file and search_codebase.
Both are read-only and confined to a single codebase root directory --
path traversal outside the root is rejected.

This is a direct port of the Swift AgentTools.swift logic, with the same
path-boundary-check fix applied (checking for a proper path separator
after the root prefix, not just a raw string prefix match, which would
let a sibling directory like /root2 slip past a check meant for /root).
"""

from __future__ import annotations

import datetime
import difflib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


class AgentToolError(Exception):
    pass


@dataclass
class ProposedFix:
    """A diff the agent proposed via propose_fix, awaiting user approval.
    `old_content`/`new_content` are kept in full (not just the diff text)
    so /apply_fix can do an exact-match safety check before writing --
    see AppliedFixError.STALE_CONTENT below for why that matters."""
    path: str               # relative to codebase root, as the agent specified it
    old_content: str        # the file's content at proposal time
    new_content: str        # the agent's proposed replacement content
    explanation: str
    diff_text: str          # unified diff, ready to display


@dataclass
class ToolDefinition:
    """OpenAI/Groq-compatible function-calling tool schema."""
    name: str
    description: str
    parameters: dict

    def to_groq_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


READ_FILE_TOOL = ToolDefinition(
    name="read_file",
    description=(
        "Reads the contents of a source file, optionally restricted to a "
        "line range. Use this to inspect the actual code around a crash "
        "location once you know the file and line number from the "
        "symbolicated stack trace."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the codebase root, e.g. 'Sources/ViewController.swift'",
            },
            "start_line": {
                "type": "integer",
                "description": "Optional 1-indexed start line. Omit to read from the start of the file.",
            },
            "end_line": {
                "type": "integer",
                "description": "Optional 1-indexed end line (inclusive). Omit to read to the end of the file.",
            },
        },
        "required": ["path"],
    },
)

SEARCH_CODEBASE_TOOL = ToolDefinition(
    name="search_codebase",
    description=(
        "Searches all source files in the codebase for a literal text "
        "pattern (case-sensitive substring match, not regex) and returns "
        "matching file paths with line numbers. Use this to find where a "
        "variable is set, where a function is called from, or to trace "
        "related code that isn't directly in the stack trace."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Literal text to search for, e.g. 'self.data ='",
            },
            "file_extension": {
                "type": "string",
                "description": "Optional file extension filter without the dot, e.g. 'swift'. Omit to search all text files.",
            },
        },
        "required": ["query"],
    },
)

PROPOSE_FIX_TOOL = ToolDefinition(
    name="propose_fix",
    description=(
        "Proposes a code fix for a file by providing its complete new "
        "contents. This does NOT modify the file -- it only records a "
        "proposed change that will be shown to the user as a diff for "
        "their review and explicit approval. Use this once you've "
        "identified the root cause and know exactly what the fixed file "
        "should look like. Always provide the COMPLETE new file content, "
        "not just the changed lines -- the diff is computed automatically "
        "by comparing your full content against the current file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the codebase root, same as used with read_file.",
            },
            "new_content": {
                "type": "string",
                "description": "The complete new contents of the file after your fix is applied.",
            },
            "explanation": {
                "type": "string",
                "description": "A one or two sentence explanation of what this change does, shown to the user alongside the diff.",
            },
        },
        "required": ["path", "new_content", "explanation"],
    },
)

ALL_TOOLS = [READ_FILE_TOOL, SEARCH_CODEBASE_TOOL, PROPOSE_FIX_TOOL]

MAX_FILE_BYTES = 200_000  # ~200KB cap per file read
MAX_SEARCH_RESULTS = 30   # cap so a broad query doesn't blow out the LLM's context

# Extensions we'll attempt to read as text during search; anything else
# (images, binaries, archives) is skipped without opening, both for
# correctness and so we don't waste time decoding garbage.
_SEARCHABLE_TEXT_EXTENSIONS = {
    "swift", "m", "mm", "h", "hpp", "c", "cpp", "cc", "py", "js", "ts",
    "json", "md", "txt", "yml", "yaml", "xml", "html", "css", "java",
    "kt", "go", "rs", "rb", "sh",
}


class AppliedFixError(Exception):
    """Raised by apply_fix specifically -- kept distinct from
    AgentToolError since this is called from the API layer (main.py),
    not from the agent's tool-dispatch loop, and callers may want to
    handle it differently (e.g. map staleness to a specific HTTP status
    so the UI can prompt the user to re-diagnose)."""
    pass


def apply_fix(codebase_root: str, path: str, expected_old_content: str, new_content: str) -> str:
    """Writes `new_content` to `path` (relative to codebase_root), after:
    1. Re-validating the path is within codebase_root (same boundary
       check as the read-only tools -- this function is reachable
       directly from an API endpoint, not just through the agent's
       sandboxed tool dispatch, so it re-checks independently rather
       than trusting the caller).
    2. Confirming the file's CURRENT content on disk still matches
       `expected_old_content` (the content captured at proposal time).
       If it doesn't match, the file changed between proposal and
       approval -- e.g. the user edited it manually, or a different fix
       was already applied -- and writing blindly could silently
       discard those changes. Refuses in that case rather than guessing.
    3. Writing a timestamped `.bak` copy of the current file before
       overwriting it.

    Returns the backup file's path on success.
    """
    root = Path(codebase_root).resolve()
    if not root.is_dir():
        raise AppliedFixError(f"codebase_root does not exist or is not a directory: {codebase_root}")

    candidate = (root / path).resolve()
    root_str = str(root)
    candidate_str = str(candidate)
    is_within_root = candidate_str == root_str or candidate_str.startswith(root_str + os.sep)
    if not is_within_root:
        raise AppliedFixError(f"path '{path}' is outside the allowed codebase root")

    if not candidate.is_file():
        raise AppliedFixError(f"file not found: {path}")

    current_content = candidate.read_text(encoding="utf-8")
    if current_content != expected_old_content:
        raise AppliedFixError(
            f"the file '{path}' has changed on disk since this fix was proposed "
            f"-- refusing to overwrite to avoid silently discarding those changes. "
            f"Re-run Diagnose to get a fresh proposal against the current file content."
        )

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = candidate.with_name(candidate.name + f".bak.{timestamp}")
    shutil.copy2(candidate, backup_path)

    candidate.write_text(new_content, encoding="utf-8")

    return str(backup_path)


class AgentToolRunner:
    """Executes tools against a fixed codebase root directory."""

    def __init__(self, codebase_root: str):
        self.codebase_root = Path(codebase_root).resolve()
        if not self.codebase_root.is_dir():
            raise AgentToolError(f"codebase_root does not exist or is not a directory: {codebase_root}")
        # Captures the most recent propose_fix call's structured diff data,
        # so the agent loop can surface it to the UI separately from the
        # plain-text string that goes back to the LLM. A list (not just
        # "last one") in case the agent proposes fixes to multiple files
        # in one diagnosis.
        self.proposed_fixes: list["ProposedFix"] = []

    def execute(self, tool_name: str, arguments_json: str) -> str:
        """Dispatches a tool call by name. Errors are caught and returned
        as descriptive strings (not raised) so the agent loop can keep
        running and the LLM can see and react to the failure -- same
        design as the Swift version."""
        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError as e:
            return f"Error: could not parse tool arguments as JSON: {e}"

        try:
            if tool_name == "read_file":
                return self._read_file(args)
            elif tool_name == "search_codebase":
                return self._search_codebase(args)
            elif tool_name == "propose_fix":
                return self._propose_fix(args)
            else:
                return f"Error: unknown tool '{tool_name}'"
        except AgentToolError as e:
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001 -- deliberately broad: any tool failure should be fed back to the LLM, not crash the server
            return f"Error: unexpected failure running '{tool_name}': {e}"

    # -- read_file --

    def _resolve_and_validate(self, relative_path: str) -> Path:
        candidate = (self.codebase_root / relative_path).resolve()
        root_str = str(self.codebase_root)
        candidate_str = str(candidate)

        # Boundary-aware check: require an exact match or a path
        # separator immediately after the root, so a sibling directory
        # with a similar name (e.g. root_str + "2") can't slip through
        # a plain string-prefix check.
        is_within_root = candidate_str == root_str or candidate_str.startswith(root_str + os.sep)
        if not is_within_root:
            raise AgentToolError(f"path '{relative_path}' is outside the allowed codebase root")
        return candidate

    def _read_file(self, args: dict) -> str:
        path = args.get("path")
        if not path:
            raise AgentToolError("'path' argument is required")

        file_path = self._resolve_and_validate(path)
        if not file_path.is_file():
            raise AgentToolError(f"file not found: {path}")

        size = file_path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise AgentToolError(f"file too large to read ({size} bytes): {path}")

        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise AgentToolError(f"file is not valid UTF-8 text: {path} ({e})") from e

        lines = text.splitlines()
        start_line = args.get("start_line") or 1
        end_line = args.get("end_line") or len(lines)

        start_line = max(1, int(start_line))
        end_line = min(len(lines), int(end_line))

        if start_line > end_line:
            return f"(requested line range is empty or out of bounds; file has {len(lines)} lines)"

        numbered = "\n".join(f"{i}: {lines[i-1]}" for i in range(start_line, end_line + 1))
        return f"File: {path} (lines {start_line}-{end_line} of {len(lines)})\n\n{numbered}"

    # -- propose_fix --

    def _propose_fix(self, args: dict) -> str:
        path = args.get("path")
        new_content = args.get("new_content")
        explanation = args.get("explanation", "")

        if not path:
            raise AgentToolError("'path' argument is required")
        if new_content is None:
            raise AgentToolError("'new_content' argument is required")

        file_path = self._resolve_and_validate(path)
        if not file_path.is_file():
            raise AgentToolError(
                f"file not found: {path} -- propose_fix requires the file to "
                f"already exist (read it first with read_file to confirm the path)"
            )

        old_content = file_path.read_text(encoding="utf-8")

        if old_content == new_content:
            return (
                "No changes: the proposed new_content is identical to the "
                "file's current content. Nothing to propose."
            )

        diff_lines = list(difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        ))
        diff_text = "".join(diff_lines)

        fix = ProposedFix(
            path=path,
            old_content=old_content,
            new_content=new_content,
            explanation=explanation,
            diff_text=diff_text,
        )
        self.proposed_fixes.append(fix)

        added = sum(1 for l in diff_lines if l.startswith('+') and not l.startswith('+++'))
        removed = sum(1 for l in diff_lines if l.startswith('-') and not l.startswith('---'))

        # The string returned here goes back to the LLM as the tool
        # result -- keep it short. The full diff is surfaced to the UI
        # separately via self.proposed_fixes, not by dumping the whole
        # diff into the LLM's context again (it already has the content,
        # no need to pay tokens to see it a second time).
        return (
            f"Fix proposed for {path} ({added} lines added, {removed} lines "
            f"removed). The diff has been recorded for user review -- you "
            f"don't need to do anything else with this file. You can "
            f"continue investigating other files, or finish with your final answer."
        )

    # -- search_codebase --

    def _search_codebase(self, args: dict) -> str:
        query = args.get("query")
        if not query:
            raise AgentToolError("'query' argument is required")
        ext_filter = args.get("file_extension")

        results: list[str] = []

        for dirpath, dirnames, filenames in os.walk(self.codebase_root):
            # Skip hidden directories (.git, .build, etc) in place so
            # os.walk doesn't descend into them.
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

            if len(results) >= MAX_SEARCH_RESULTS:
                break

            for filename in filenames:
                if len(results) >= MAX_SEARCH_RESULTS:
                    break
                if filename.startswith("."):
                    continue

                ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
                if ext_filter and ext != ext_filter:
                    continue
                if not ext_filter and ext not in _SEARCHABLE_TEXT_EXTENSIONS:
                    continue

                full_path = Path(dirpath) / filename
                try:
                    if full_path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    text = full_path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue

                rel_path = full_path.relative_to(self.codebase_root)
                for line_num, line in enumerate(text.splitlines(), start=1):
                    if len(results) >= MAX_SEARCH_RESULTS:
                        break
                    if query in line:
                        results.append(f"{rel_path}:{line_num}: {line.strip()}")

        if not results:
            return f"No matches found for '{query}' in the codebase."
        return f"Found {len(results)} match(es) for '{query}':\n\n" + "\n".join(results)
