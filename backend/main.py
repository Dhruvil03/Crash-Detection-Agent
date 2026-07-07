"""
main.py

FastAPI backend for CrashAgent. Run with:
  GROQ_API_KEY=sk-... python3 main.py
Serves on http://127.0.0.1:8000
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


# ----- Pydantic models -----

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


class DiagnoseRequest(BaseModel):
    crash: SymbolicateResponse
    codebase_root: str
    groq_api_key: str
    groq_model: str | None = None


class ApplyFixRequest(BaseModel):
    codebase_root: str
    path: str
    expected_old_content: str
    new_content: str


class ApplyFixResponse(BaseModel):
    backup_path: str


# ----- Endpoints -----

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


@app.post("/diagnose")
async def diagnose_endpoint(req: DiagnoseRequest):
    """Streams AgentStep events as newline-delimited JSON (NDJSON)."""
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


@app.post("/apply_fix", response_model=ApplyFixResponse)
def apply_fix_endpoint(req: ApplyFixRequest) -> ApplyFixResponse:
    """Writes an approved fix to disk with backup and staleness check."""
    try:
        backup_path = apply_fix(
            codebase_root=req.codebase_root,
            path=req.path,
            expected_old_content=req.expected_old_content,
            new_content=req.new_content,
        )
    except AppliedFixError as e:
        status = 409 if "changed on disk" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e)) from e

    return ApplyFixResponse(backup_path=backup_path)


@app.get("/health")
def health():
    return {"status": "ok", "atos_available": atos_available()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
