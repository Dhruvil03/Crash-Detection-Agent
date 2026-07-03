"""
main.py

FastAPI service exposing two endpoints for the (now-thin) SwiftUI client:

  POST /symbolicate   -> runs atos, returns a symbolicated crash as JSON
  POST /diagnose       -> runs the full agent loop, streaming steps back
                          as newline-delimited JSON (NDJSON), one line
                          per AgentStep, so the Swift UI can render the
                          live "thinking out loud" transcript exactly
                          like the original design intended.

Run locally:
  GROQ_API_KEY=sk-... python3 main.py
  (serves on http://127.0.0.1:8000)
"""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from symbolication import (
    symbolicate_crash_log,
    SymbolicationError,
    SymbolicatedCrash,
    SymbolicatedFrame,
    atos_available,
)
from agent_tools import AgentToolRunner, AgentToolError, apply_fix, AppliedFixError
from agent_loop import diagnose
from groq_client import GroqClient, GroqConfig, DEFAULT_MODEL

app = FastAPI(title="CrashAgent Backend")


class SymbolicateRequest(BaseModel):
    crash_log_text: str
    binary_path: str
    target_binary_name: str | None = None


class SymbolicatedFrameResponse(BaseModel):
    index: int
    address: int
    binary_name: str
    symbol_name: str | None = None
    file_path: str | None = None
    line_number: int | None = None
    display_line: str


class SymbolicateResponse(BaseModel):
    thread_label: str
    exception_type: str
    exception_subtype: str
    frames: list[SymbolicatedFrameResponse]


@app.post("/symbolicate", response_model=SymbolicateResponse)
def symbolicate(req: SymbolicateRequest) -> SymbolicateResponse:
    try:
        result = symbolicate_crash_log(
            crash_log_text=req.crash_log_text,
            binary_path=req.binary_path,
            target_binary_name=req.target_binary_name,
        )
    except SymbolicationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return SymbolicateResponse(
        thread_label=result.thread_label,
        exception_type=result.exception_type,
        exception_subtype=result.exception_subtype,
        frames=[
            SymbolicatedFrameResponse(
                index=f.index,
                address=f.address,
                binary_name=f.binary_name,
                symbol_name=f.symbol_name,
                file_path=f.file_path,
                line_number=f.line_number,
                display_line=f.display_line,
            )
            for f in result.frames
        ],
    )


class DiagnoseRequest(BaseModel):
    crash: SymbolicateResponse
    codebase_root: str
    groq_api_key: str
    groq_model: str | None = None


@app.post("/diagnose")
async def diagnose_endpoint(req: DiagnoseRequest):
    """Streams AgentStep events as newline-delimited JSON. Each line is
    a complete JSON object; the Swift client should read the response
    body line by line (URLSession's bytes(for:) async sequence handles
    this naturally)."""

    try:
        tool_runner = AgentToolRunner(req.codebase_root)
    except AgentToolError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    crash = SymbolicatedCrash(
        thread_label=req.crash.thread_label,
        exception_type=req.crash.exception_type,
        exception_subtype=req.crash.exception_subtype,
        frames=[
            SymbolicatedFrame(
                index=f.index, address=f.address, binary_name=f.binary_name,
                symbol_name=f.symbol_name, file_path=f.file_path, line_number=f.line_number,
            )
            for f in req.crash.frames
        ],
    )

    config = GroqConfig(api_key=req.groq_api_key, model=req.groq_model or DEFAULT_MODEL)
    groq_client = GroqClient(config)

    async def event_stream():
        try:
            async for step in diagnose(crash, groq_client, tool_runner):
                yield json.dumps(step.to_dict()) + "\n"
        finally:
            await groq_client.aclose()

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


@app.get("/health")
def health():
    return {"status": "ok", "atos_available": atos_available()}


class ApplyFixRequest(BaseModel):
    codebase_root: str
    path: str                  # relative to codebase_root, as proposed
    expected_old_content: str  # the file's content at proposal time (from the fix_proposed step)
    new_content: str           # the agent's proposed replacement content


class ApplyFixResponse(BaseModel):
    backup_path: str


@app.post("/apply_fix", response_model=ApplyFixResponse)
def apply_fix_endpoint(req: ApplyFixRequest) -> ApplyFixResponse:
    """Writes an approved fix to disk. Only ever called after the user
    has reviewed the diff in the UI and explicitly clicked Approve --
    nothing in the agent loop itself can reach this; propose_fix only
    ever computes a diff, never writes. See agent_tools.apply_fix for
    the backup-before-write and staleness-check behavior."""
    try:
        backup_path = apply_fix(
            codebase_root=req.codebase_root,
            path=req.path,
            expected_old_content=req.expected_old_content,
            new_content=req.new_content,
        )
    except AppliedFixError as e:
        # Staleness ("changed on disk") gets 409 Conflict so the UI can
        # distinguish "you need to re-diagnose" from a plain bad-request
        # (missing file, bad path) which is 400.
        status = 409 if "changed on disk" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e)) from e

    return ApplyFixResponse(backup_path=backup_path)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
