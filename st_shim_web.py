#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
st_shim_web.py —— 带网页控制台的多站点版 st-shim。

在 st_shim.py（单站点核心）之上加一层：
    * 网页控制台：加/删/改/切换不同中转站点，存到 profiles.json，改完立刻生效，不用重启；
    * 每个站点可以单独配置上游地址、自己的 key、客户端令牌、额外请求头、请求体注入字段；
    * 客户端（Tavo）永远只填一个地址：
          http://<IP>:8787/v1           -> 走【当前启用】的站点
          http://<IP>:8787/p/<站点id>/v1 -> 走指定站点（不切换也能用，适合 A/B 对比）
    * 网页上还能一键“测试”：拿酒馆指纹去上游打一次 /models 和一次真实对话，直接看通不通。

启动：
    python st_shim_web.py --port 8787
    python st_shim_web.py --port 8787 --admin-token 面板密码     # 公网部署务必加
    # 兼容老用法（等价于创建一个名为“默认”的站点并启用）：
    python st_shim_web.py --upstream https://api.example.com/v1 --api-key sk-xxx

控制台： http://<IP>:8787/
"""

import argparse
import hmac
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import st_shim as core  # noqa: E402
from st_shim import (DEFAULT_ST_VERSION, DEFAULT_UA, Handler, Shim,  # noqa: E402
                     client_allowed, join_url, log_line, rewrite_body, rewrite_headers)

__all__ = ["Store", "MultiHandler", "ADMIN_HTML", "main", "core", "Shim"]

PROFILE_FIELDS = ("id", "name", "upstream", "api_key", "client_token",
                  "st_version", "ua", "headers", "json_fields", "strip_fields", "note")


# ---------------------------------------------------------------- 站点仓库

class Store:
    """profiles.json 的读写。文件是唯一事实来源，网页改完立即生效。

    云端（Render/Railway 这类临时文件系统）注意事项：
      平台重启后文件会丢。所以启动时若存在环境变量 PROFILES_JSON，就用它作为初始配置，
      控制台的「保存到云端」按钮会把它写回平台环境变量，重启也不丢。
    """

    def __init__(self, path, log=None, bootstrap_json=""):
        self.path = path
        self.log = log
        self.lock = threading.RLock()
        self.profiles = []
        self.active = ""
        self.bootstrap_json = (bootstrap_json or "").strip()
        self.load()

    # ---- 磁盘
    def load(self):
        with self.lock:
            data = None
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, json.JSONDecodeError) as e:
                    if self.log:
                        log_line(self.log, "profiles.json 读取失败：%r" % (e,))
            if data is None and self.bootstrap_json:
                try:
                    data = json.loads(self.bootstrap_json)
                    if self.log:
                        log_line(self.log, "已从环境变量 PROFILES_JSON 载入 %d 个站点"
                                 % len(data.get("profiles", [])))
                except json.JSONDecodeError as e:
                    if self.log:
                        log_line(self.log, "环境变量 PROFILES_JSON 不是合法 JSON：%r" % (e,))
            if data is None:
                data = {}
            self.profiles = [self._norm(p) for p in data.get("profiles", [])]
            self.active = data.get("active", "")
            if not self.active and self.profiles:
                self.active = self.profiles[0]["id"]

    # ---- 导出：给“保存到云端”和备份用
    def export(self):
        with self.lock:
            return {"active": self.active, "profiles": [dict(p) for p in self.profiles]}

    def export_for_env(self):
        """写进环境变量用的紧凑版：去掉没用的空字段，方便塞进平台的环境变量输入框。"""
        with self.lock:
            slim = []
            for p in self.profiles:
                keep = {k: v for k, v in p.items() if v not in ("", [], None)}
                keep["id"] = p["id"]
                slim.append(keep)
            return {"active": self.active, "profiles": slim}

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"active": self.active, "profiles": self.profiles},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    # ---- 校验/规整
    @staticmethod
    def _norm(p):
        out = {
            "id": str(p.get("id") or uuid.uuid4().hex[:8]),
            "name": str(p.get("name") or "未命名站点").strip(),
            "upstream": str(p.get("upstream") or "").strip().rstrip("/"),
            "api_key": str(p.get("api_key") or "").strip(),
            "client_token": str(p.get("client_token") or "").strip(),
            "st_version": str(p.get("st_version") or DEFAULT_ST_VERSION).strip(),
            "ua": str(p.get("ua") or DEFAULT_UA).strip(),
            "headers": [str(x) for x in (p.get("headers") or [])],
            "json_fields": [str(x) for x in (p.get("json_fields") or [])],
            "strip_fields": [str(x) for x in (p.get("strip_fields") or [])],
            "note": str(p.get("note") or ""),
        }
        return out

    # ---- 查询
    def list(self):
        with self.lock:
            return [dict(p) for p in self.profiles], self.active

    def get(self, pid):
        with self.lock:
            for p in self.profiles:
                if p["id"] == pid:
                    return dict(p)
        return None

    def active_profile(self):
        with self.lock:
            for p in self.profiles:
                if p["id"] == self.active:
                    return dict(p)
            return dict(self.profiles[0]) if self.profiles else None

    def default_upstream(self):
        p = self.active_profile()
        return p["upstream"] if p else ""

    # ---- 变更
    def upsert(self, payload):
        with self.lock:
            pid = str(payload.get("id") or "").strip()
            incoming = self._norm({**payload, "id": pid or uuid.uuid4().hex[:8]})
            for i, p in enumerate(self.profiles):
                if p["id"] == incoming["id"]:
                    # 空白字段表示“不改”，避免网页把已存的 key 覆盖没
                    for k in ("api_key", "client_token"):
                        if not payload.get(k, None):
                            incoming[k] = p[k]
                    self.profiles[i] = incoming
                    self.save()
                    return incoming
            if not pid:
                self.profiles.append(incoming)
                if len(self.profiles) == 1:
                    self.active = incoming["id"]
                self.save()
                return incoming
            raise KeyError("站点不存在：%s" % pid)

    def delete(self, pid):
        with self.lock:
            before = len(self.profiles)
            self.profiles = [p for p in self.profiles if p["id"] != pid]
            if len(self.profiles) == before:
                return False
            if self.active == pid:
                self.active = self.profiles[0]["id"] if self.profiles else ""
            self.save()
            return True

    def activate(self, pid):
        with self.lock:
            if not any(p["id"] == pid for p in self.profiles):
                return None
            self.active = pid
            self.save()
            return self.active_profile()


# ---------------------------------------------------------------- 请求处理

class MultiHandler(Handler):
    """在核心 Handler 之上加：控制台 API + 多站点路由。"""

    store: Store = None
    base_cfg = None
    admin_token = ""

    # ---- GET/POST 分流
    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            self._api("GET", path)
        elif path in ("/", "/index.html"):
            self._send_html(ADMIN_HTML)
        else:
            self._handle()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            self._api("POST", path)
        else:
            self._handle()

    do_PUT = do_POST

    # 有些客户端（含 PowerShell 的部分版本）会把 DELETE 发成 POST + X-HTTP-Method-Override
    def do_DELETE(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            self._api("DELETE", path)
        else:
            self._handle()

    # ---- 请求体：浏览器和命令行可能发 GBK，这里强制按 UTF-8 解，避免中文变问号
    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            try:
                return json.loads(raw.decode("gbk"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
        except json.JSONDecodeError:
            return {}

    # ---- 控制台接口
    def _api(self, method, path):
        if not self._admin_ok():
            self._send_json(401, {"error": "需要控制台口令：在网址后加 ?token=你的口令，或填 X-Admin-Token 请求头"})
            return
        payload = self._json_body()

        try:
            if path == "/api/profiles" and method == "GET":
                profiles, active = self.store.list()
                self._send_json(200, {
                    "active": active, "profiles": [self._public(p) for p in profiles],
                    "listen_port": self.server.server_address[1],
                    "platform": "render" if (os.environ.get("RENDER_API_KEY")
                                              and os.environ.get("RENDER_SERVICE_ID")) else "self",
                    "public_url": os.environ.get("RENDER_EXTERNAL_URL", ""),
                })
            elif path == "/api/profiles" and method == "POST":
                p = self.store.upsert(payload)
                log_line(self.base_cfg["log"], "控制台：保存站点 %s (%s) -> %s"
                         % (p["id"], p["name"], p["upstream"]))
                self._send_json(200, {"ok": True, "profile": self._public(p)})
            elif path.startswith("/api/profiles/") and method == "DELETE":
                pid = path.rsplit("/", 1)[-1]
                ok = self.store.delete(pid)
                self._send_json(200 if ok else 404, {"ok": ok})
            elif path == "/api/activate" and method == "POST":
                p = self.store.activate(str(payload.get("id", "")))
                if not p:
                    self._send_json(404, {"ok": False, "error": "站点不存在"})
                    return
                log_line(self.base_cfg["log"], "控制台：切换到站点 %s (%s)" % (p["id"], p["name"]))
                self._send_json(200, {"ok": True, "active": p["id"]})
            elif path == "/api/test" and method == "POST":
                self._send_json(200, self._test(payload))
            elif path == "/api/export" and method == "GET":
                self._send_json(200, {"active": self.store.active, "profiles": self.store.profiles})
            elif path == "/api/sync" and method == "POST":
                self._send_json(200, self._sync_cloud())
            else:
                self._send_json(404, {"error": "未知接口 %s" % path})
        except KeyError as e:
            self._send_json(404, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log_line(self.base_cfg["log"], "控制台接口出错：%r" % (e,))
            self._send_json(500, {"error": repr(e)})

    def _admin_ok(self):
        if not self.admin_token:
            return True
        got = self.headers.get("X-Admin-Token") or ""
        if not got:
            q = urlsplit(self.path).query
            m = re.search(r"(?:^|&)token=([^&]*)", q)
            got = m.group(1) if m else ""
        return hmac.compare_digest(got, self.admin_token)

    @staticmethod
    def _public(p):
        """给网页看的数据：不把上游 key 明文发回浏览器。"""
        out = dict(p)
        out["api_key_set"] = bool(p.get("api_key"))
        out["client_token_set"] = bool(p.get("client_token"))
        out["api_key"] = ""
        out["client_token"] = ""
        return out

    # ---- 一键测试：拿真实上游打一次，看酒馆指纹过不过
    def _test(self, payload):
        pid = str(payload.get("id") or "")
        p = self.store.get(pid) if pid else None
        if not p:
            return {"ok": False, "stage": "config", "error": "站点不存在"}
        if not p["upstream"]:
            return {"ok": False, "stage": "config", "error": "上游地址为空"}
        cfg = self._profile_cfg(p)
        auth = ("Bearer " + p["api_key"]) if p["api_key"] else None
        results = {}

        # 1) 先试着拉模型列表：最快的连通性 + 鉴权 + 区域限制检查
        model = str(payload.get("model") or "").strip()
        url = join_url(p["upstream"], "/models", "")
        headers = rewrite_headers({}, cfg, auth)
        code, text = self._call("GET", url, headers, None, cfg)
        results["models"] = {"url": url, "status": code, "body": text[:400]}
        if code == 200 and not model:
            try:
                items = json.loads(text).get("data") or []
                model = items[0].get("id", "") if items else ""
            except (json.JSONDecodeError, AttributeError):
                pass
        # 有些站点只开放对话接口，/models 返回 404/405/501，不算失败，继续测对话
        models_blocked = code in (401, 402, 403)

        if not model:
            model = "gpt-4o-mini"

        # 2) 真发一次对话（用当前站点的酒馆指纹）
        url2 = join_url(p["upstream"], "/chat/completions", "")
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "回复两个字：收到"}],
            "stream": False, "max_tokens": 16,
        }, ensure_ascii=False).encode("utf-8")
        headers2 = rewrite_headers({}, cfg, auth)
        headers2["Content-Type"] = "application/json"
        code2, text2 = self._call("POST", url2, headers2, body, cfg)
        results["chat"] = {"url": url2, "model": model, "status": code2, "body": text2[:600]}

        if code2 == 200:
            return {"ok": True, "stage": "ok",
                    "error": "" if code == 200 else "（/models 上游不支持，但对话接口正常）",
                    "results": results}
        if models_blocked and code == code2:
            return {"ok": False, "stage": "auth",
                    "error": "上游鉴权被拒（%s）：大概率是 key 不对、额度用完，或站点按别的条件拦你" % code2,
                    "results": results}
        return {"ok": False, "stage": "chat-failed",
                "error": "对话请求被上游拒绝：HTTP %s" % code2, "results": results}

    # ---- 把当前站点配置写回云平台的环境变量，避免临时文件系统重启后配置丢失
    def _sync_cloud(self):
        key = os.environ.get("RENDER_API_KEY", "").strip()
        service_id = os.environ.get("RENDER_SERVICE_ID", "").strip()
        if not key or not service_id:
            return {"ok": False, "platform": "none",
                    "error": "当前不在 Render 上（或缺 RENDER_API_KEY / RENDER_SERVICE_ID），"
                             "配置本来就存在本机 profiles.json 里，不需要同步。",
                    "profiles_json": json.dumps(self.store.export_for_env(), ensure_ascii=False)}

        payload = json.dumps(self.store.export_for_env(), ensure_ascii=False)
        base = "https://api.render.com/v1/services/%s" % service_id
        hdr = {"Authorization": "Bearer " + key, "Accept": "application/json",
               "Content-Type": "application/json"}

        # 1) 先取当前环境变量，只替换 PROFILES_JSON，别把别的变量弄丢
        try:
            with urllib.request.urlopen(urllib.request.Request(base + "/env-vars", headers=hdr), timeout=30) as r:
                current = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "platform": "render", "error": "读取 Render 环境变量失败：%s" % e}

        entries = []
        for item in current:
            ev = item.get("envVar", item)
            k, v = ev.get("key"), ev.get("value", "")
            if k and k != "PROFILES_JSON":
                entries.append({"key": k, "value": v})
        entries.append({"key": "PROFILES_JSON", "value": payload})

        # 2) 写回
        try:
            body = json.dumps(entries).encode("utf-8")
            req = urllib.request.Request(base + "/env-vars", data=body, headers=hdr, method="PUT")
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
        except urllib.error.HTTPError as e:
            return {"ok": False, "platform": "render",
                    "error": "写入 Render 环境变量失败 HTTP %s：%s"
                             % (e.code, e.read().decode("utf-8", "replace")[:300])}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "platform": "render", "error": "写入 Render 环境变量失败：%s" % e}

        # 3) 触发一次部署，让新配置生效
        redeploy = "跳过"
        try:
            req2 = urllib.request.Request(base + "/deploys",
                                          data=json.dumps({"clearCache": "do_not_clear"}).encode("utf-8"),
                                          headers=hdr, method="POST")
            with urllib.request.urlopen(req2, timeout=30) as r:
                redeploy = "已触发"
                r.read()
        except Exception as e:  # noqa: BLE001
            redeploy = "触发失败：%s" % e

        log_line(self.base_cfg["log"], "控制台：已把 %d 个站点同步到 Render 环境变量（重新部署 %s）"
                 % (len(self.store.profiles), redeploy))
        return {"ok": True, "platform": "render", "redeploy": redeploy,
                "bytes": len(payload),
                "note": "配置已写入环境变量 PROFILES_JSON；实例重启或重新部署后会自动载入。"}

    def _call(self, method, url, headers, body, cfg):
        ctx = ssl._create_unverified_context() if cfg.get("insecure") else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                return r.getcode(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, "transport error: %r" % (e,)

    # ---- 多站点路由
    def _profile_cfg(self, p):
        cfg = dict(self.base_cfg)
        cfg.update({
            "upstream": p["upstream"],
            "origin": urlunsplit((urlsplit(p["upstream"]).scheme, urlsplit(p["upstream"]).netloc, "", "", "")),
            "st_version": p["st_version"] or DEFAULT_ST_VERSION,
            "ua": p["ua"] or DEFAULT_UA,
            "api_key": p["api_key"],
            "client_tokens": [t.strip() for t in (p["client_token"] or "").split(",") if t.strip()],
            "extra_headers": list(p["headers"]),
            "json_fields": list(p["json_fields"]),
            "strip_fields": list(p["strip_fields"]),
        })
        return cfg

    def _resolve(self, path):
        """路径 -> (站点, 去掉 /p/<id> 前缀后送给上游的路径, 站点id)"""
        m = re.match(r"^/p/([A-Za-z0-9_-]+)(/.*)?$", path)
        if m:
            pid, rest = m.group(1), (m.group(2) or "/")
            p = self.store.get(pid)
            if p:
                return p, rest, pid
            return None, path, pid
        p = self.store.active_profile()
        return p, path, (p["id"] if p else "")

    def _handle(self):
        cfg0 = self.base_cfg
        path = urlsplit(self.path).path

        if path.startswith("/__stshim/health") or path == "/healthz":
            profiles, active = self.store.list()
            self._send_json(200, {
                "ok": True,
                "profiles": len(profiles),
                "active": active,
                "active_upstream": self.store.default_upstream(),
            })
            return

        profile, upstream_path, pid = self._resolve(path)
        if profile is None:
            self._send_json(404, {"error": {"message": "没有这个站点 id：%s（去控制台确认）" % pid,
                                            "type": "invalid_request_error"}})
            return
        if not profile["upstream"]:
            self._send_json(502, {"error": {"message": "站点 %s 还没填上游地址，去控制台配置" % pid,
                                            "type": "upstream_error"}})
            return

        cfg = self._profile_cfg(profile)
        with self.shim.count_lock:
            self.shim.count += 1
            rid = "#%d" % self.shim.count
        # 复用核心 Handler 的转发实现，但把上游请求头/体按当前站点重写
        self._forward(rid, cfg, profile, upstream_path)

    # ---- 真正转发（与核心逻辑一致，只是配置来自站点）
    def _forward(self, rid, cfg, profile, upstream_path):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        query = urlsplit(self.path).query
        url = join_url(cfg["upstream"], upstream_path, query)

        ok, upstream_auth = client_allowed(self.headers, cfg)
        if not ok:
            log_line(cfg["log"], "%s 拒绝：站点 %s 的客户端令牌不正确" % (rid, profile["id"]))
            self._send_json(401, {"error": {"message": "invalid client token for st-shim",
                                            "type": "invalid_request_error"}})
            return
        headers = rewrite_headers(self.headers, cfg, upstream_auth)
        out_body = rewrite_body(body, cfg, self.headers.get("Content-Type"))

        log_line(cfg["log"], "%s [%s] %s -> %s  (%d B in, %d B out)"
                 % (rid, profile["id"], self.command, url, len(body), len(out_body)))
        if cfg["verbose"]:
            log_line(cfg["log"], "%s  headers: %s" % (rid, json.dumps(headers, ensure_ascii=False)))
            if out_body:
                log_line(cfg["log"], "%s  body: %s" % (rid, out_body[:400].decode("utf-8", "replace")))

        req = urllib.request.Request(url, data=out_body if out_body else None,
                                     headers=headers, method=self.command)
        try:
            resp = urllib.request.urlopen(req, timeout=cfg["timeout"])
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:  # noqa: BLE001
            log_line(cfg["log"], "%s  upstream error: %r" % (rid, e))
            self._send_json(502, {"error": {"message": "st-shim upstream error: %s" % e,
                                            "type": "upstream_error"}})
            return

        status = resp.getcode()
        passthru_skip = {"transfer-encoding", "connection", "content-length", "keep-alive"}
        self.send_response(status)
        for k, v in resp.headers.items():
            if k.lower() in passthru_skip:
                continue
            self.send_header(k, v)

        total = 0
        try:
            if status == 200:
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read(cfg["bufsize"])
                    if not chunk:
                        break
                    total += len(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
                self.close_connection = True
            else:
                raw = resp.read()
                total = len(raw)
                self.send_header("Content-Length", str(total))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            total = -1
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        log_line(cfg["log"], "%s  <- HTTP %s (%s)"
                 % (rid, status, "client aborted" if total < 0 else "%d B" % total))

    def _send_html(self, html):
        raw = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


# ---------------------------------------------------------------- 控制台页面

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>st-shim 站点控制台</title>
<style>
  :root{--bg:#0f1115;--card:#181b22;--line:#2a2f3a;--fg:#e6e8ee;--dim:#98a0b3;--acc:#4f8cff;--ok:#28c76f;--bad:#ff5c5c}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  header{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
  h1{font-size:17px;margin:0;font-weight:600}
  .grow{flex:1}
  .wrap{padding:18px 22px;max-width:1180px;margin:0 auto}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
  .row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
  label{display:block;font-size:12px;color:var(--dim);margin:10px 0 4px}
  input,textarea,select{width:100%;background:#11141a;border:1px solid var(--line);color:var(--fg);
    border-radius:8px;padding:8px 10px;font:13px/1.5 ui-monospace,Consolas,monospace}
  textarea{min-height:64px;resize:vertical}
  button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer}
  button.ghost{background:#232833;color:var(--fg)}
  button.danger{background:#7a2222}
  button:disabled{opacity:.5;cursor:not-allowed}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
  .tag{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
  .tag.on{color:#0b1a10;background:var(--ok);border-color:var(--ok);font-weight:600}
  .mono{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--dim);word-break:break-all}
  pre{background:#11141a;border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;max-height:260px;font-size:12px}
  .ok{color:var(--ok)} .bad{color:var(--bad)} .dim{color:var(--dim)}
  .site{display:flex;gap:12px;align-items:center;padding:12px;border:1px solid var(--line);border-radius:10px;margin-bottom:10px}
  .site .name{font-weight:600}
  .hidden{display:none}
</style>
</head>
<body>
<header>
  <h1>st-shim 站点控制台</h1>
  <span class="tag" id="hdrActive">读取中…</span>
  <span class="grow"></span>
  <input id="token" placeholder="控制台口令（若已设置）" style="width:220px">
  <button class="ghost" onclick="load()">刷新</button>
</header>

<div class="wrap">
  <div class="card">
    <div class="row">
      <div>
        <div style="font-weight:600">Tavo 里固定填这个地址，切换站点不用改客户端</div>
        <div class="mono" id="baseHint">http://&lt;本机或云IP&gt;:端口/v1</div>
      </div>
      <span class="grow"></span>
      <button class="ghost" onclick="copyBase()">复制</button>
    </div>
    <div class="mono" style="margin-top:8px">想临时指定某个站点（不切换）：把 <b>/v1</b> 换成 <b>/p/&lt;站点id&gt;/v1</b></div>
  </div>

  <div class="card hidden" id="cloudCard">
    <div class="row">
      <div>
        <div style="font-weight:600">云端持久化（Render 等临时文件系统）</div>
        <div class="dim">实例重启或重新部署后，本地文件会丢。点下面的按钮把当前站点配置写回平台环境变量，重启自动恢复。</div>
      </div>
      <span class="grow"></span>
      <button onclick="syncCloud()">保存到云端</button>
    </div>
    <pre id="syncOut" class="hidden"></pre>
  </div>

  <div class="card">
    <div class="row"><div style="font-weight:600">已有站点</div><span class="grow"></span>
      <button onclick="newProfile()">+ 新增站点</button></div>
    <div id="list" style="margin-top:12px"></div>
  </div>

  <div class="card" id="editor">
    <div class="row"><div style="font-weight:600" id="edTitle">新增站点</div><span class="grow"></span>
      <button class="ghost" onclick="hideEditor()">收起</button></div>
    <div class="grid">
      <div><label>名称（自己认得出就行）</label><input id="f_name" placeholder="某某中转站"></div>
      <div><label>上游 Base URL（含 /v1）</label><input id="f_upstream" placeholder="https://api.example.com/v1"></div>
      <div><label>上游 API Key（留空=不改，已存的不会显示）</label><input id="f_api_key" placeholder="sk-..."></div>
      <div><label>客户端令牌（可选，公网建议设置；Tavo 的 API Key 填这个）</label><input id="f_client_token" placeholder="自己起一个长随机串"></div>
      <div><label>伪装酒馆版本</label><input id="f_st_version" placeholder="1.13.4"></div>
      <div><label>要不要手填模型名做测试？（可留空）</label><input id="f_model" placeholder="gpt-4o-mini"></div>
    </div>
    <label>额外请求头（一行一个，形如 <span class="mono">X-Foo: bar</span>）</label>
    <textarea id="f_headers" placeholder="X-Foo: bar"></textarea>
    <label>请求体注入字段（一行一个，形如 <span class="mono">chat_completion_source="custom"</span>）</label>
    <textarea id="f_json_fields"></textarea>
    <label>请求体删除字段（一行一个，逗号分隔的字段名）</label>
    <textarea id="f_strip_fields" placeholder="frequency_penalty"></textarea>
    <label>备注</label>
    <textarea id="f_note"></textarea>
    <div class="row" style="margin-top:14px">
      <button onclick="saveProfile()">保存</button>
      <button class="ghost" onclick="testProfile()">测试连通（真打上游）</button>
      <span class="grow"></span>
      <span id="saveHint" class="dim"></span>
    </div>
    <pre id="testOut" class="hidden"></pre>
  </div>
</div>

<script>
let state = {profiles: [], active: "", port: 8787, editing: null};

function token() { return document.getElementById('token').value.trim(); }
function api(path, method, body) {
  const h = {'Content-Type': 'application/json'};
  const t = token(); if (t) h['X-Admin-Token'] = t;
  return fetch(path, {method: method || 'GET', headers: h, body: body ? JSON.stringify(body) : undefined})
    .then(async r => {
      const text = await r.text();
      let data; try { data = JSON.parse(text); } catch (e) { data = {error: text}; }
      if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
      return data;
    });
}
function esc(s) { return (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function lines(id) { return document.getElementById(id).value.split('\n').map(s => s.trim()).filter(Boolean); }

function load() {
  api('/api/profiles').then(d => {
    state.profiles = d.profiles; state.active = d.active; state.port = d.listen_port;
    state.platform = d.platform; state.publicUrl = d.public_url || '';
    const host = state.publicUrl ? state.publicUrl.replace(/\/$/, '') : ('http://<本机或云IP>:' + d.listen_port);
    document.getElementById('baseHint').textContent = host + '/v1';
    document.getElementById('hdrActive').textContent = '当前启用：' +
      ((d.profiles.find(p => p.id === d.active) || {}).name || '（没有站点，点新增）');
    if (d.platform === 'render') document.getElementById('cloudCard').classList.remove('hidden');
    render();
  }).catch(e => { document.getElementById('list').innerHTML = '<span class="bad">读取失败：' + esc(e.message) + '</span>'; });
}

function syncCloud() {
  const out = document.getElementById('syncOut'); out.classList.remove('hidden'); out.textContent = '同步中…';
  api('/api/sync', 'POST', {}).then(d => {
    out.innerHTML = (d.ok ? '<span class="ok">已保存 ✓ </span>' : '<span class="bad">失败 ✗ </span>')
      + esc(d.error || '') + '\n' + esc(JSON.stringify({redeploy: d.redeploy, bytes: d.bytes, note: d.note}, null, 1));
  }).catch(e => { out.textContent = '同步失败：' + e.message; });
}

function render() {
  const box = document.getElementById('list');
  if (!state.profiles.length) { box.innerHTML = '<span class="dim">还没有站点，点右上角「+ 新增站点」。</span>'; return; }
  box.innerHTML = state.profiles.map(p => {
    const on = p.id === state.active;
    return `<div class="site">
      <span class="tag ${on ? 'on' : ''}">${on ? '启用中' : '备用'}</span>
      <span class="name">${esc(p.name)}</span>
      <span class="mono">${esc(p.upstream || '（未填上游）')}</span>
      <span class="mono">id=${esc(p.id)}</span>
      ${p.client_token_set ? '<span class="tag">有客户端令牌</span>' : ''}
      <span class="grow"></span>
      ${on ? '' : `<button class="ghost" onclick="activate('${p.id}')">切到这个</button>`}
      <button class="ghost" onclick="edit('${p.id}')">编辑</button>
      <button class="ghost" onclick="quickTest('${p.id}')">测试</button>
      <button class="danger" onclick="del('${p.id}')">删除</button>
    </div>`;
  }).join('');
}

function newProfile() {
  state.editing = null;
  document.getElementById('edTitle').textContent = '新增站点';
  ['f_name','f_upstream','f_api_key','f_client_token','f_model','f_headers','f_json_fields','f_strip_fields','f_note']
    .forEach(id => document.getElementById(id).value = '');
  document.getElementById('f_st_version').value = '1.13.4';
  document.getElementById('testOut').classList.add('hidden');
  document.getElementById('saveHint').textContent = '';
}

function edit(id) {
  const p = state.profiles.find(x => x.id === id); if (!p) return;
  state.editing = id;
  document.getElementById('edTitle').textContent = '编辑站点：' + p.name + '（id=' + p.id + '）';
  document.getElementById('f_name').value = p.name;
  document.getElementById('f_upstream').value = p.upstream;
  document.getElementById('f_api_key').value = '';
  document.getElementById('f_api_key').placeholder = p.api_key_set ? '已保存（留空=不改）' : 'sk-...';
  document.getElementById('f_client_token').value = '';
  document.getElementById('f_client_token').placeholder = p.client_token_set ? '已保存（留空=不改）' : '可选';
  document.getElementById('f_st_version').value = p.st_version || '1.13.4';
  document.getElementById('f_headers').value = (p.headers || []).join('\n');
  document.getElementById('f_json_fields').value = (p.json_fields || []).join('\n');
  document.getElementById('f_strip_fields').value = (p.strip_fields || []).join('\n');
  document.getElementById('f_note').value = p.note || '';
  document.getElementById('testOut').classList.add('hidden');
  document.getElementById('saveHint').textContent = '';
  window.scrollTo({top: document.getElementById('editor').offsetTop - 20, behavior: 'smooth'});
}

function hideEditor() { document.getElementById('testOut').classList.add('hidden'); }

function payload() {
  return {
    id: state.editing || '',
    name: document.getElementById('f_name').value.trim() || '未命名站点',
    upstream: document.getElementById('f_upstream').value.trim(),
    api_key: document.getElementById('f_api_key').value.trim(),
    client_token: document.getElementById('f_client_token').value.trim(),
    st_version: document.getElementById('f_st_version').value.trim() || '1.13.4',
    headers: lines('f_headers'),
    json_fields: lines('f_json_fields'),
    strip_fields: lines('f_strip_fields'),
    note: document.getElementById('f_note').value.trim(),
  };
}

function saveProfile() {
  const body = payload();
  if (!body.upstream) { document.getElementById('saveHint').innerHTML = '<span class="bad">上游地址不能为空</span>'; return; }
  document.getElementById('saveHint').textContent = '保存中…';
  api('/api/profiles', 'POST', body).then(d => {
    document.getElementById('saveHint').innerHTML = '<span class="ok">已保存，立即生效</span>';
    if (!state.editing) state.editing = d.profile.id;
    load();
  }).catch(e => { document.getElementById('saveHint').innerHTML = '<span class="bad">失败：' + esc(e.message) + '</span>'; });
}

function testProfile() {
  const out = document.getElementById('testOut'); out.classList.remove('hidden');
  out.textContent = '测试中…（会用酒馆指纹真打一次上游，几秒）';
  const body = payload();
  if (state.editing) body.id = state.editing;
  body.model = document.getElementById('f_model').value.trim();
  if (!body.id) { out.textContent = '先保存站点再测试。'; return; }
  api('/api/test', 'POST', body).then(d => {
    out.innerHTML = (d.ok ? '<span class="ok">通过 ✓ </span>' : '<span class="bad">失败 ✗ </span>')
      + esc(d.error || '') + '\n' + esc(JSON.stringify(d.results, null, 1));
  }).catch(e => { out.textContent = '测试失败：' + e.message; });
}

function quickTest(id) {
  const out = document.getElementById('testOut'); out.classList.remove('hidden');
  out.textContent = '测试中…';
  api('/api/test', 'POST', {id}).then(d => {
    out.innerHTML = (d.ok ? '<span class="ok">通过 ✓ </span>' : '<span class="bad">失败 ✗ </span>')
      + esc(d.error || '') + '\n' + esc(JSON.stringify(d.results, null, 1));
  }).catch(e => { out.textContent = '测试失败：' + e.message; });
}

function activate(id) {
  api('/api/activate', 'POST', {id}).then(load).catch(e => alert('切换失败：' + e.message));
}
function del(id) {
  if (!confirm('删除这个站点？（profiles.json 里会移除）')) return;
  api('/api/profiles/' + id, 'DELETE').then(load).catch(e => alert('删除失败：' + e.message));
}
function copyBase() {
  const t = document.getElementById('baseHint').textContent;
  navigator.clipboard.writeText(t).then(() => alert('已复制：' + t));
}
load();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 启动

def main():
    ap = argparse.ArgumentParser(description="st-shim 多站点版（带网页控制台）")
    ap.add_argument("--upstream", default=None, help="兼容老用法：首次启动时创建一个默认站点")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--client-token", default=os.environ.get("ST_SHIM_CLIENT_TOKEN"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8787),
                    help="监听端口；云端平台会用 PORT 环境变量指定")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--profiles", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json"))
    ap.add_argument("--bootstrap-profiles", default=os.environ.get("PROFILES_JSON", ""),
                    help="云端用：从环境变量载入初始站点配置（临时文件系统重启后靠它恢复）")
    ap.add_argument("--admin-token", default=os.environ.get("ST_SHIM_ADMIN_TOKEN", ""),
                    help="控制台口令；公网部署务必设置，否则任何人都能改你的站点配置")
    ap.add_argument("--st-version", default=DEFAULT_ST_VERSION, help="新建站点默认伪装用的酒馆版本号")
    ap.add_argument("--ua", default=None)
    ap.add_argument("--header", action="append")
    ap.add_argument("--json-field", action="append")
    ap.add_argument("--strip-field", action="append")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--bufsize", type=int, default=8192)
    ap.add_argument("--log", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "st-shim.log"))
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--insecure", action="store_true", help="跳过 HTTPS 证书校验（自签证书的上游用）")
    args = ap.parse_args()

    store = Store(args.profiles, log=args.log, bootstrap_json=args.bootstrap_profiles)

    # 兼容老用法：命令行给了 upstream 且仓库为空 -> 建一个默认站点
    if args.upstream and not store.profiles:
        p = store.upsert({
            "name": "默认站点", "upstream": args.upstream, "api_key": args.api_key or "",
            "client_token": args.client_token or "", "st_version": args.st_version,
            "headers": args.header or [], "json_fields": args.json_field or [],
            "strip_fields": args.strip_field or [],
        })
        store.activate(p["id"])
        log_line(args.log, "已把 --upstream 存成站点 %s（%s）" % (p["id"], p["name"]))

    core.Shim.count_lock = threading.Lock()
    base_cfg = {
        "mode": "tavern",
        "log": args.log,
        "verbose": args.verbose,
        "timeout": args.timeout,
        "bufsize": args.bufsize,
        "insecure": args.insecure,
    }
    shim = Shim({**base_cfg, "upstream": store.default_upstream(), "mode": "tavern",
                 "origin": "", "ua": args.ua or DEFAULT_UA, "st_version": args.st_version,
                 "api_key": "", "client_tokens": [], "extra_headers": [], "json_fields": [],
                 "strip_fields": []})
    Handler.shim = shim

    MultiHandler.store = store
    MultiHandler.base_cfg = base_cfg
    MultiHandler.admin_token = args.admin_token

    srv = ThreadingHTTPServer((args.host, args.port), MultiHandler)
    srv.daemon_threads = True
    srv.allow_reuse_address = True

    profiles, active = store.list()
    log_line(args.log, "st-shim-web 启动：%s:%d  站点 %d 个，当前启用 %s"
             % (args.host, args.port, len(profiles), active or "（无）"))
    log_line(args.log, "控制台： http://<本机或云IP>:%d/    Tavo 填： http://<本机或云IP>:%d/v1" % (args.port, args.port))
    if not args.admin_token and args.host not in ("127.0.0.1", "localhost"):
        log_line(args.log, "⚠ 未设置 --admin-token，控制台在公网上任何人都能打开和改配置")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log_line(args.log, "已停止")


if __name__ == "__main__":
    main()
