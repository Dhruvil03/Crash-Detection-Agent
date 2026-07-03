"""
mock_groq_server.py

A tiny FastAPI server that mimics Groq's chat completions endpoint
closely enough to drive a real end-to-end test of /diagnose -- real
HTTP calls, real JSON wire format, just pointed at this instead of
api.groq.com. This is the most realistic test possible without an
actual Groq API key.

Scripted behavior: first call returns a tool_call for read_file on
ViewController.swift; second call returns a final answer referencing
what it "read". Mimics the real investigation flow.
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import json

app = FastAPI()

_call_count = {"n": 0}


@app.post("/openai/v1/chat/completions")
async def chat_completions(request: dict):
    _call_count["n"] += 1

    if _call_count["n"] == 1:
        return JSONResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Let me look at the crash site first.",
                    "tool_calls": [{
                        "id": "call_abc123",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({
                                "path": "Sources/ViewController.swift",
                                "start_line": 1,
                                "end_line": 20,
                            }),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }]
        })
    else:
        return JSONResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        "ROOT CAUSE: data!.first force-unwraps a nil optional "
                        "in loadData(), called synchronously from viewDidLoad "
                        "before the async network fetch in fetchFromNetwork() "
                        "has a chance to populate it.\n"
                        "EVIDENCE: Saw `let first = data!.first` at line 11, "
                        "and `self?.data = result` set inside a network "
                        "completion handler.\n"
                        "SUGGESTED FIX: guard let data else { return } before "
                        "accessing it, and trigger loadData() from the network "
                        "completion handler instead of viewDidLoad."
                    ),
                    "tool_calls": None,
                },
                "finish_reason": "stop",
            }]
        })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
