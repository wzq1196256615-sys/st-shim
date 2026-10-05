#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_site.py —— 模拟“只服务酒馆用户”的中转站，用来离线验证 st_shim.py 的改写是否生效。

判定规则（模拟真实站点的常见做法）：
    必须同时满足 —— User-Agent 是浏览器 UA、Origin/Referer 与本站同源、
    带 X-ST-Version 头、请求体里有 stream 字段。
    否则返回 403 {"error":{"message":"only SillyTavern clients are supported"}}

用法：
    python mock_site.py --port 9911
    # 然后：python st_shim.py --upstream http://127.0.0.1:9911/v1 --port 8787 --log mock_shim.log
    # 裸请求测试（应被 403）：
    #   curl -s -o - -w "\\n%{http_code}\\n" -X POST http://127.0.0.1:9911/v1/chat/completions -H "Content-Type: application/json" -d "{\\"model\\":\\"m\\",\\"messages\\":[]}"
    # 走代理（应 200）：
    #   curl -s -o - -w "\\n%{http_code}\\n" -X POST http://127.0.0.1:8787/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer test" -d "{\\"model\\":\\"m\\",\\"messages\\":[{\\"role\\":\\"user\\",\\"content\\":\\"hi\\"}]}"
"""

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mock-tavern-only-site"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                TimeoutError, OSError):
            self.close_connection = True

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def do_GET(self):
        """模拟 /models：同样只认酒馆指纹，用来测连通性。"""
        ua = self.headers.get("User-Agent") or ""
        if "Mozilla" not in ua or not self.headers.get("X-ST-Version"):
            self._json(403, {"error": {"message": "only SillyTavern clients are supported",
                                       "type": "forbidden_client"}})
            return
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [
                {"id": "mock-model-a", "object": "model"},
                {"id": "mock-model-b", "object": "model"},
            ]})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)

        problems = []
        ua = self.headers.get("User-Agent") or ""
        if "Mozilla" not in ua:
            problems.append("user-agent is not a browser")
        origin = self.headers.get("Origin") or ""
        if not origin.startswith("http://127.0.0.1") and not origin.startswith("http://localhost"):
            problems.append("missing/foreign Origin")
        ref = self.headers.get("Referer") or ""
        if not ref:
            problems.append("missing Referer")
        if not self.headers.get("X-ST-Version"):
            problems.append("missing X-ST-Version")
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            data = None
            problems.append("body is not JSON")
        if isinstance(data, dict) and "stream" not in data:
            problems.append("missing stream field")

        print("[mock] %s  rejected=%s  ua=%r" % (self.path, problems, ua[:40]), flush=True)

        if problems:
            self._json(403, {"error": {"message": "only SillyTavern clients are supported",
                                       "details": problems, "type": "forbidden_client"}})
            return

        # 通过检查：stream=true 走 SSE（验证代理的流式透传），否则普通 JSON
        if isinstance(data, dict) and data.get("stream"):
            self._sse(data)
            return

        self._json(200, {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": (data or {}).get("model", "mock"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "收到（服务端已认定为酒馆客户端）"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 8, "completion_tokens": 9, "total_tokens": 17},
            "x_mock_seen": {
                "ua": ua, "origin": origin, "referer": ref,
                "x_st_version": self.headers.get("X-ST-Version"),
                "x_st_client": self.headers.get("X-ST-Client"),
                "has_stream": isinstance(data, dict) and "stream" in data,
            },
        })

    def _sse(self, data):
        """分块发送 SSE，模拟真实上游的流式生成。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        words = ["服务端", "已认定", "为", "酒馆", "客户端", "。"]
        for i, w in enumerate(words):
            chunk = {
                "id": "chatcmpl-mock", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": data.get("model", "mock"),
                "choices": [{"index": 0, "delta": {"content": w}, "finish_reason": None}],
            }
            self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.05)
        done = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": data.get("model", "mock"),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self.wfile.write(("data: " + json.dumps(done, ensure_ascii=False) + "\n\n").encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _json(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # GBK 控制台下中文日志不至于抛异常
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9911)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    print("mock 站点监听 http://%s:%d/v1  （只接受酒馆指纹请求）" % (a.host, a.port), flush=True)
    Server((a.host, a.port), Handler).serve_forever()
