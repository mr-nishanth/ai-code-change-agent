"""A fake OpenAI-compatible `/v1/chat/completions` server (no model) for the Floci Bedrock proxy test.

Run inside a throwaway container that shares its network namespace with Floci:
`python fake_openai_upstream.py <port> <log-file>`. It binds 127.0.0.1 only and appends every
request (path, Authorization header, JSON body) to the log file as one JSON line. A request that
offers tools and does not end with a tool result gets a `read_file` tool call; anything else gets
the text "fake reply". Streaming requests are answered as SSE chunks with usage in the last one.
Standard library only, so it runs on a plain Python image."""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


class Handler(BaseHTTPRequestHandler):
    log_path = ""

    def log_message(self, *_: Any) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        body: dict[str, Any] = json.loads(self.rfile.read(length) or b"{}")
        entry = {"path": self.path, "auth": self.headers.get("authorization"), "body": body}
        with open(self.log_path, "a", encoding="utf-8") as log:
            log.write(json.dumps(entry) + "\n")
        tools = body.get("tools") or []
        last = (body.get("messages") or [{}])[-1]
        use_tool = bool(tools) and last.get("role") != "tool"
        call = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": tools[0]["function"]["name"] if tools else "",
                "arguments": json.dumps({"path": "README.md"}),
            },
        }
        finish = "tool_calls" if use_tool else "stop"
        if body.get("stream"):
            self._stream(body, call if use_tool else None, finish)
            return
        message: dict[str, Any] = (
            {"role": "assistant", "content": None, "tool_calls": [call]}
            if use_tool
            else {"role": "assistant", "content": "fake reply"}
        )
        out = {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": USAGE,
        }
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, body: dict[str, Any], call: dict[str, Any] | None, finish: str) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        base = {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": body.get("model"),
        }

        def event(delta: dict[str, Any], **extra: Any) -> None:
            chunk = {**base, "choices": [{"index": 0, "delta": delta, **extra}]}
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()

        if call is not None:
            head = {**call, "function": {"name": call["function"]["name"], "arguments": ""}}
            event({"role": "assistant", "tool_calls": [{"index": 0, **head}]})
            args = call["function"]["arguments"]
            event({"tool_calls": [{"index": 0, "function": {"arguments": args}}]})
        else:
            for part in ("fake ", "reply"):
                event({"content": part})
        last = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
        self.wfile.write(b"data: " + json.dumps({**last, "usage": USAGE}).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> None:
    Handler.log_path = sys.argv[2]
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()


if __name__ == "__main__":
    main()
