#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
st_shim.py —— 把第三方客户端的请求伪装成 SillyTavern（酒馆）发出的请求。

用途：
    某些 OpenAI 兼容中转站只服务酒馆用户，会对非酒馆客户端做指纹拦截。
    本程序在本机（或云服务器）监听一个端口，接收 Tavo 等客户端的请求，
    改写成酒馆的请求头 / 请求体后转发到上游站点，再把响应原样透传回去。
    不需要修改 Tavo 本身，也不需要上游站点配合。

零依赖：只用 Python 3 标准库。流式（SSE）与非流式都支持。

启动示例：
    python st_shim.py --upstream https://api.example.com/v1 --port 8787
    # 上游是 https://api.example.com/v1 时，Tavo 的 base URL 填 http://127.0.0.1:8787/v1

常用参数：
    --upstream     上游 base URL（含 /v1 也行，不含也行）
    --port         本地监听端口，默认 8787
    --host         本地监听地址，默认 0.0.0.0
    --name         命令行里显示的“客户端名”，默认 SillyTavern
    --mode         tavern = 完整伪装（默认）；plain = 只做透传对照
    --st-version   伪装用的酒馆版本号
    --ua           覆盖 User-Agent
    --header       追加 / 覆盖请求头，可重复：--header "X-Foo: bar"
    --json-field   给 JSON 请求体注入字段，可重复：--json-field 'chat_completion_source="custom"'
    --strip-field  从 JSON 请求体里删除字段，可重复
    --log          日志文件路径，默认 st-shim.log
    --verbose      打印每个请求头与请求体摘要
"""

import argparse
import hmac
import json
import os
import re
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

# ---------------------------------------------------------------- 默认配置

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_ST_VERSION = "1.13.4"

# 客户端自报身份相关的头：全部丢弃，避免暴露“不是酒馆”
DROP_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade", "accept-encoding", "expect",
    # 客户端身份泄露
    "x-client", "x-client-name", "x-client-version", "x-app", "x-app-version",
    "x-request-source", "x-source", "x-platform", "x-tavo", "tavo-client",
    "x-title", "http-referer", "x-stainless-lang", "x-stainless-package-version",
    "x-stainless-os", "x-stainless-arch", "x-stainless-runtime",
    "x-stainless-runtime-version", "x-stainless-retry-count",
    "x-stainless-timeout", "openai-beta", "openai-organization", "openai-project",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-site",
    "sec-fetch-mode", "sec-fetch-dest", "dnt", "priority",
}

# 转发时“不保留客户端原值”的头：一律由本程序按酒馆格式重写
FORCE_HEADERS = {
    "user-agent", "accept", "accept-language", "origin", "referer",
    "content-type", "x-st-version", "x-st-chat-source", "x-st-client",
    "x-requested-with",
}

# 伪装成的浏览器上下文（Origin / Referer 必须和上游同源，否则更可疑）
def st_browser_headers(upstream_origin, st_version, ua):
    return {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": upstream_origin,
        "Referer": upstream_origin + "/",
        "X-Requested-With": "XMLHttpRequest",
        # 酒馆前端版本标识（部分中转站用这个判断“是不是酒馆”）
        "X-ST-Version": st_version,
        "X-ST-Client": "SillyTavern",
        "X-ST-Chat-Source": "custom",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }


# ---------------------------------------------------------------- 工具

def _print_safe(text):
    """Windows 控制台默认是 GBK，直接 print 生僻字符/emoji 会抛 UnicodeEncodeError 把进程搞挂。"""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(text.encode(enc, "replace").decode(enc, "replace") + "\n")
        sys.stdout.flush()


def log_line(path, text):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + text
    _print_safe(line)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def join_url(upstream, incoming_path, query):
    """把上游 base 与客户端路径拼成一个完整 URL。

    upstream 结尾带 /v1、客户端也发 /v1/chat/completions 时，不会拼成 /v1/v1/...。
    """
    up = urlsplit(upstream)
    up_path = up.path.rstrip("/")
    rest = incoming_path or "/"

    for prefix in ("/v1", "/api/v1", "/openai/v1"):
        if up_path.endswith(prefix) and (rest == prefix or rest.startswith(prefix + "/")):
            rest = rest[len(prefix):] or "/"
            break

    if not rest.startswith("/"):
        rest = "/" + rest

    path = (up_path + rest) if up_path else rest
    path = re.sub(r"/{2,}", "/", path)
    return urlunsplit((up.scheme, up.netloc, path, query, ""))


def parse_json_field(spec):
    """解析 --json-field 'key=value'，value 会按 JSON 解析，失败则当字符串。"""
    if "=" not in spec:
        return spec, True
    key, raw = spec.split("=", 1)
    raw = raw.strip()
    try:
        return key.strip(), json.loads(raw)
    except json.JSONDecodeError:
        return key.strip(), raw.strip('"').strip("'")


# ---------------------------------------------------------------- 改写逻辑

def extract_token(auth_header):
    if not auth_header:
        return ""
    parts = auth_header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return auth_header.strip()


def client_allowed(client_headers, cfg):
    """开启 --client-token 时，只有带了正确令牌的客户端能用这个代理。

    返回 (是否放行, 上游要用的 Authorization)。
    """
    allowed = cfg["client_tokens"]
    if not allowed:
        auth = client_headers.get("Authorization") or client_headers.get("authorization")
        if auth:
            return True, auth
        if cfg["api_key"]:
            return True, "Bearer " + cfg["api_key"]
        return True, None

    got = extract_token(client_headers.get("Authorization") or client_headers.get("authorization"))
    ok = any(hmac.compare_digest(got, t) for t in allowed)
    if not ok:
        return False, None
    # 客户端令牌只用于过闸，真正发给上游的是你自己的 key
    return True, ("Bearer " + cfg["api_key"]) if cfg["api_key"] else None


def rewrite_headers(client_headers, cfg, upstream_auth):
    """把客户端请求头改造成酒馆请求头。"""
    out = {}
    for k, v in client_headers.items():
        lk = k.lower()
        if lk in DROP_HEADERS or lk in FORCE_HEADERS:
            continue
        out[k] = v

    # Authorization 单独处理：要么原样透传，要么换成自己的 key
    base = st_browser_headers(cfg["origin"], cfg["st_version"], cfg["ua"])
    for k, v in base.items():
        out[k] = v

    if upstream_auth:
        out["Authorization"] = upstream_auth

    for spec in cfg["extra_headers"]:
        if ":" in spec:
            k, v = spec.split(":", 1)
            out[k.strip()] = v.strip()

    return out


def rewrite_body(body, cfg, content_type):
    """按需规范化 JSON 请求体。默认只动必要部分，不动 prompt 内容。"""
    if not body or "json" not in (content_type or "").lower():
        return body
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(data, dict):
        return body

    changed = False
    for f in cfg["strip_fields"]:
        if f in data:
            data.pop(f, None)
            changed = True

    # OpenAI 兼容站点几乎都要求 stream 字段显式存在
    if cfg["mode"] == "tavern" and "stream" not in data:
        data["stream"] = False
        changed = True

    for spec in cfg["json_fields"]:
        key, value = parse_json_field(spec)
        if data.get(key) != value:
            data[key] = value
            changed = True

    if not changed:
        return body
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------- 服务

class Shim:
    count_lock = threading.Lock()  # 多线程下保证请求编号不重复

    def __init__(self, cfg):
        self.cfg = cfg
        self.count = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "st-shim"
    sys_version = ""

    shim: Shim = None  # 由 main() 注入

    # ---- 日志：把默认 stderr 噪音压掉，走我们自己的日志
    def log_message(self, fmt, *args):
        pass

    # ---- 客户端（尤其流式请求）提前断开是常态，不要刷一堆 traceback
    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                TimeoutError, OSError):
            self.close_connection = True

    def _cfg(self):
        return self.shim.cfg

    def _handle(self):
        cfg = self._cfg()
        with self.shim.count_lock:
            self.shim.count += 1
            rid = "#%d" % self.shim.count

        # 健康检查，方便确认代理活着
        if self.path.startswith("/__stshim/health"):
            self._send_json(200, {"ok": True, "mode": cfg["mode"], "upstream": cfg["upstream"]})
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        url = join_url(cfg["upstream"], urlsplit(self.path).path, urlsplit(self.path).query)
        ok, upstream_auth = client_allowed(self.headers, cfg)
        if not ok:
            log_line(cfg["log"], "%s 拒绝：客户端令牌不正确" % rid)
            self._send_json(401, {"error": {"message": "invalid client token for st-shim",
                                            "type": "invalid_request_error"}})
            return
        headers = rewrite_headers(self.headers, cfg, upstream_auth)
        out_body = rewrite_body(body, cfg, self.headers.get("Content-Type"))

        log_line(cfg["log"], "%s %s -> %s  (%d B in, %d B out)"
                 % (rid, self.command, url, len(body), len(out_body)))
        if cfg["verbose"]:
            log_line(cfg["log"], "%s  headers: %s" % (rid, json.dumps(headers, ensure_ascii=False)))
            if out_body:
                log_line(cfg["log"], "%s  body: %s" % (rid, out_body[:400].decode("utf-8", "replace")))

        req = urllib.request.Request(url, data=out_body if out_body else None,
                                     headers=headers, method=self.command)
        try:
            # 超时给足：上游生成长文本很慢
            resp = urllib.request.urlopen(req, timeout=cfg["timeout"])
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:  # noqa: BLE001 —— 网络层错误统一回 502，便于客户端看到原因
            log_line(cfg["log"], "%s  upstream error: %r" % (rid, e))
            self._send_json(502, {"error": {"message": "st-shim upstream error: %s" % e,
                                            "type": "upstream_error"}})
            return

        status = resp.getcode()
        # 透传响应头，只丢掉会破坏 keep-alive / 长度的那些
        passthru_skip = {"transfer-encoding", "connection", "content-length", "keep-alive"}
        self.send_response(status)
        for k, v in resp.headers.items():
            if k.lower() in passthru_skip:
                continue
            self.send_header(k, v)

        # 关键：上游的分块/连接收尾方式不能照搬，否则客户端会一直挂着等更多数据。
        # 这里统一“读完再定长发回”，既保留 SSE 内容，又让客户端明确知道响应结束。
        total = 0
        try:
            if status == 200:
                # 上游可能是 SSE 长连接：用“发完即断”的方式收尾，客户端一定能读到结尾
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read(cfg["bufsize"])
                    if not chunk:
                        break
                    total += len(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()  # 每个分块立刻推给客户端，流式才有“打字”感
                self.close_connection = True
            else:
                # 错误响应一般不大：定长发回，避免客户端读不到结束
                raw = resp.read()
                total = len(raw)
                self.send_header("Content-Length", str(total))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            total = -1  # 客户端主动断开（用户点了停止），属正常
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        log_line(cfg["log"], "%s  <- HTTP %s (%s)"
                 % (rid, status, "client aborted" if total < 0 else "%d B" % total))

    do_POST = _handle
    do_GET = _handle

    def _send_json(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_cfg(args):
    up = urlsplit(args.upstream)
    origin = urlunsplit((up.scheme, up.netloc, "", "", ""))
    return {
        "upstream": args.upstream.rstrip("/"),
        "origin": origin,
        "mode": args.mode,
        "ua": args.ua or DEFAULT_UA,
        "st_version": args.st_version,
        "api_key": args.api_key,
        "client_tokens": [t.strip() for t in (args.client_token or "").split(",") if t.strip()],
        "extra_headers": args.header or [],
        "json_fields": args.json_field or [],
        "strip_fields": args.strip_field or [],
        "log": args.log,
        "verbose": args.verbose,
        "timeout": args.timeout,
        "bufsize": args.bufsize,
    }


def main():
    ap = argparse.ArgumentParser(description="把客户端请求伪装成 SillyTavern 请求后转发")
    ap.add_argument("--upstream", required=True, help="上游 base URL，例如 https://api.example.com/v1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8787),
                    help="监听端口；云端平台会用 PORT 环境变量指定")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--mode", choices=["tavern", "plain"], default="tavern")
    ap.add_argument("--name", default="SillyTavern", help="仅用于日志显示")
    ap.add_argument("--st-version", default=DEFAULT_ST_VERSION)
    ap.add_argument("--ua", default=None)
    ap.add_argument("--api-key", default=None, help="发给上游用的 key（客户端不带 Authorization 时兜底）")
    ap.add_argument("--client-token", default=os.environ.get("ST_SHIM_CLIENT_TOKEN"),
                    help="公网部署时用：客户端必须带的令牌，逗号分隔可多个；不匹配则 401")
    ap.add_argument("--header", action="append", help='追加请求头："X-Foo: bar"')
    ap.add_argument("--json-field", action="append", help='注入请求体字段：key=value（value 按 JSON 解析）')
    ap.add_argument("--strip-field", action="append", help="从请求体删除字段")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--bufsize", type=int, default=8192, help="流式转发的分块大小")
    ap.add_argument("--log", default="st-shim.log")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = build_cfg(args)
    shim = Shim(cfg)
    Handler.shim = shim

    srv = Server((args.host, args.port), Handler)
    log_line(cfg["log"], "st-shim 启动：%s:%d  ->  %s  [mode=%s, 伪装为 %s %s]"
             % (args.host, args.port, cfg["upstream"], cfg["mode"], args.name, cfg["st_version"]))
    log_line(cfg["log"], "客户端 base URL 填： http://<本机或云IP>:%d/v1" % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log_line(cfg["log"], "已停止")


if __name__ == "__main__":
    main()
