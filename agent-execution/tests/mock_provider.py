"""Local Responses SSE fixture. Never contacts an external model endpoint."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

REPLY = "Local mock response"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        expected = self.server.expected_authorization
        if expected and self.headers.get("Authorization") != expected:
            self.send_error(401)
            return
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        item = {
            "id": f"msg_{uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "status": "completed",
            "content": [{"type": "output_text", "text": REPLY, "annotations": []}],
        }
        if self.server.response_factory:
            item = self.server.response_factory(request, item)
        is_tool = item["type"] == "function_call"
        response = {
            "id": f"resp_{uuid4().hex}",
            "object": "response",
            "status": "completed",
            "output": [item],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }
        events = [
            {
                "type": "response.created",
                "response": {**response, "status": "in_progress", "output": []},
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    **item,
                    "status": "in_progress",
                    **({"arguments": ""} if is_tool else {"content": []}),
                },
            },
            {
                "type": (
                    "response.function_call_arguments.delta"
                    if is_tool
                    else "response.output_text.delta"
                ),
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": item["arguments"] if is_tool else REPLY,
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        body = "".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def provider(*, response_factory=None, expected_authorization=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.response_factory = response_factory
    server.expected_authorization = expected_authorization
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
