"""
agent_tools.py

Tool implementations the agent can call: read_file, search_codebase,
and propose_fix. All are confined to a codebase root directory.

apply_fix() is a standalone function (not an agent tool) called only
from the /apply_fix HTTP endpoint after the user explicitly approves.
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


class AppliedFixError(Exception):
    pass


@dataclass
class ProposedFix:
    path: str
    old_content: str
    new_content: str
    explanation: str
    diff_text: str


@dataclass
class ToolDefinition:
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
        "Reads the contents of a source file, optionally restricted to a line range. "
        "Use this to inspect the actual code around a crash location."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the codebase root."},
            "start_line": {"type": "integer", "description": "Optional 1-indexed start line."},
            "end_line": {"type": "integer", "description": "Optional 1-indexed end line (inclusive)."},
        },
        "required": ["path"],
    },
)

SEARCH_CODEBASE_TOOL = ToolDefinition(
    name="search_codebase",
    description=(
        "Searches all source files for a literal text pattern and returns matching "
        "file paths with line numbers. Use this to find where a variable is set or "
        "a function is called."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Literal text to search for."},
            "file_extension": {"type": "string", "description": "Optional extension filter, e.g. 'swift'."},
        },
        "required": ["query"],
    },
)

PROPOSE_FIX_TOOL = ToolDefinition(
    name="propose_fix",
    description=(
        "Proposes a code fix by providing a file's complete new contents. "
        "Does NOT modify the file -- records a diff for the user to review and approve. "
        "Always provide the COMPLETE new file content, not just the changed lines."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the codebase root."},
            "new_content": {"type": "string", "description": "The complete new contents of the file."},
            "explanation": {"type": "string", "description": "One or two sentence explanation of the change."},
        },
        "required": ["path", "new_content", "explanation"],
    },
)

ALL_TOOLS = [READ_FILE_TOOL, SEARCH_CODEBASE_TOOL, PROPOSE_FIX_TOOL]

MAX_FILE_BYTES = 200_000
MAX_SEARCH_RESULTS = 30

_SEARCHABLE_TEXT_EXTENSIONS = {
    "swift", "m", "mm", "h", "hpp", "c", "cpp", "cc", "py", "js", "ts",
    "json", "md", "txt", "yml", "yaml", "xml", "html", "css", "java",
    "kt", "go", "rs", "rb", "sh",
}


def apply_fix(codebase_root: str, path: str, expected_old_content: str, new_content: str) -> str:
    """Writes new_content to path after backup and staleness check. Returns backup path."""
    root = Path(codebase_root).resolve()
    if not root.is_dir():
        raise AppliedFixError(f"codebase_root does not exist: {codebase_root}")

    candidate = (root / path).resolve()
    root_str = str(root)
    candidate_str = str(candidate)
    is_within = candidate_str == root_str or candidate_str.startswith(root_str + os.sep)
    if not is_within:
        raise AppliedFixError(f"path '{path}' is outside the allowed codebase root")

    if not candidate.is_file():
        raise AppliedFixError(f"file not found: {path}")

    current = candidate.read_text(encoding="utf-8")
    if current != expected_old_content:
        raise AppliedFixError(
            f"the file '{path}' has changed on disk since this fix was proposed "
            f"-- refusing to overwrite. Re-run Diagnose to get a fresh proposal."
        )

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = candidate.with_name(candidate.name + f".bak.{timestamp}")
    shutil.copy2(candidate, backup_path)
    candidate.write_text(new_content, encoding="utf-8")

    return str(backup_path)


class AgentToolRunner:
    def __init__(self, codebase_root: str):
        self.codebase_root = Path(codebase_root).resolve()
        if not self.codebase_root.is_dir():
            raise AgentToolError(f"codebase_root does not exist: {codebase_root}")
        self.proposed_fixes: list[ProposedFix] = []

    def execute(self, tool_name: str, arguments_json: str) -> str:
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
        except Exception as e:  # noqa: BLE001
            return f"Error: unexpected failure running '{tool_name}': {e}"

    def _resolve_and_validate(self, relative_path: str) -> Path:
        candidate = (self.codebase_root / relative_path).resolve()
        root_str = str(self.codebase_root)
        candidate_str = str(candidate)
        is_within = candidate_str == root_str or candidate_str.startswith(root_str + os.sep)
        if not is_within:
            raise AgentToolError(f"path '{relative_path}' is outside the allowed codebase root")
        return candidate

    def _read_file(self, args: dict) -> str:
        path = args.get("path")
        if not path:
            raise AgentToolError("'path' argument is required")

        file_path = self._resolve_and_validate(path)
        if not file_path.is_file():
            raise AgentToolError(f"file not found: {path}")

        if file_path.stat().st_size > MAX_FILE_BYTES:
            raise AgentToolError(f"file too large: {path}")

        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise AgentToolError(f"file is not valid UTF-8: {path}") from e

        lines = text.splitlines()
        start_line = max(1, int(args.get("start_line") or 1))
        end_line = min(len(lines), int(args.get("end_line") or len(lines)))

        if start_line > end_line:
            return f"(requested range out of bounds; file has {len(lines)} lines)"

        numbered = "\n".join(f"{i}: {lines[i-1]}" for i in range(start_line, end_line + 1))
        return f"File: {path} (lines {start_line}-{end_line} of {len(lines)})\n\n{numbered}"

    def _search_codebase(self, args: dict) -> str:
        query = args.get("query")
        if not query:
            raise AgentToolError("'query' argument is required")
        ext_filter = args.get("file_extension")

        results: list[str] = []

        for dirpath, dirnames, filenames in os.walk(self.codebase_root):
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
            raise AgentToolError(f"file not found: {path}")

        old_content = file_path.read_text(encoding="utf-8")

        if old_content == new_content:
            return "No changes: proposed content is identical to the current file."

        diff_lines = list(difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        ))
        diff_text = "".join(diff_lines)

        self.proposed_fixes.append(ProposedFix(
            path=path,
            old_content=old_content,
            new_content=new_content,
            explanation=explanation,
            diff_text=diff_text,
        ))

        added = sum(1 for l in diff_lines if l.startswith('+') and not l.startswith('+++'))
        removed = sum(1 for l in diff_lines if l.startswith('-') and not l.startswith('---'))

        return (
            f"Fix proposed for {path} ({added} lines added, {removed} lines removed). "
            f"The diff has been recorded for user review."
        )
