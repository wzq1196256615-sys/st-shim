#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
st_probe.py —— A/B 对比「裸客户端」和「酒馆指纹」两种请求，定位上游到底在卡什么。

做法：对同一个上游、同一个模型、同一个 prompt，连发三种请求

    A  bare      ：Tavo 那种裸请求（几乎只有 Authorization + Content-Type）
    B  openai-sdk：带 OpenAI SDK / httpx / okhttp 特征头的请求
    C  tavern    ：完整酒馆指纹（浏览器 UA + Origin/Referer + X-ST-* + stream 字段）

如果 A/B 被拒而 C 通过 —— 站点就是按请求头指纹卡人，直接上 st_shim.py。
如果三个都过 —— 站点卡的不是请求头，需要在 st_shim.py 里加 --json-field / --strip-field。
如果三个都被拒 —— 不是客户端指纹问题（余额、key、模型名、地区限制等）。

用法：
    python st_probe.py --upstream https://api.example.com/v1 --key sk-xxxx --model gpt-4o-mini
    python st_probe.py --upstream ... --key ... --dump      # 额外打印完整响应体
"""

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def base_headers(origin, ua, st_version):
    return {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": origin,
        "Referer": origin + "/",
        "X-Requested-With": "XMLHttpRequest",
        "X-ST-Version": st_version,
        "X-ST-Client": "SillyTavern",
        "X-ST-Chat-Source": "custom",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }


def variants(origin, key, ua, st_version):
    """返回三种请求方案：(名称, 请求头, 请求体改动函数)"""
    return [
        ("A bare 裸客户端", {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        }, lambda b: b),

        ("B openai-sdk 特征", {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "OpenAI/Python 1.40.0",
            "X-Stainless-Lang": "python",
            "X-Stainless-Package-Version": "1.40.0",
            "X-Stainless-OS": "Windows",
            "Accept": "application/json",
        }, lambda b: b),

        ("C tavern 酒馆指纹", base_headers(origin, ua, st_version),
         lambda b: b),
    ]


def call(url, headers, body, timeout, insecure):
    ctx = ssl._create_unverified_context() if insecure else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.getcode(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, "transport error: %r" % (e,)


def main():
    ap = argparse.ArgumentParser(description="对比裸请求与酒馆指纹请求")
    ap.add_argument("--upstream", required=True, help="上游 base URL，例如 https://api.example.com/v1")
    ap.add_argument("--key", required=True, help="API key")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--prompt", default="说一句「收到」即可。")
    ap.add_argument("--path", default="/chat/completions")
    ap.add_argument("--st-version", default="1.13.4")
    ap.add_argument("--ua", default=DEFAULT_UA)
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--insecure", action="store_true", help="跳过 HTTPS 证书校验")
    ap.add_argument("--dump", action="store_true", help="打印完整响应体")
    args = ap.parse_args()

    up = urlsplit(args.upstream)
    origin = urlunsplit((up.scheme, up.netloc, "", "", ""))
    base = args.upstream.rstrip("/")
    if base.endswith("/v1") and args.path.startswith("/v1"):
        args.path = args.path[3:]
    url = base + args.path

    body = json.dumps({
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "stream": False,
        "temperature": 0.7,
    }, ensure_ascii=False).encode("utf-8")

    print("目标：%s" % url)
    print("模型：%s\n" % args.model)

    verdict = {}
    for name, headers, mutate in variants(origin, args.key, args.ua, args.st_version):
        code, text = call(url, headers, mutate(body), args.timeout, args.insecure)
        verdict[name] = code
        head = text.replace("\n", " ")[:220]
        print("[%s] HTTP %s  %s" % (name, code or "ERR", head))
        if args.dump:
            print(text)
        print()

    codes = list(verdict.values())
    a, b, c = codes
    print("=" * 62)
    if c == 200 and a != 200:
        print("结论：站点按请求头指纹拦截非酒馆客户端。→ 直接用 st_shim.py（tavern 模式）。")
    elif a == 200 and b == 200 and c == 200:
        print("结论：三种都通过，拦截点不在请求头。→ 用 st_shim.py 的 --json-field/--strip-field 试请求体特征。")
    elif c != 200:
        print("结论：连酒馆指纹也被拒。→ 看一下上面的错误原文：可能是 key/余额/模型名/地区限制，")
        print("      也可能是需要酒馆的请求体结构（--dump 看原文后告诉我）。")
    print("=" * 62)
    return 0 if c == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
