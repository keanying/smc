#!/usr/bin/env python3
"""抓一份同程点评接口的真实响应，用于对齐字段映射。

在能访问 www.ly.com 的机器上运行：
    python scripts/probe_tongcheng.py --sid 32289
成功后会：
  1. 打印顶层键名和单条评论的键名
  2. 把原始响应存到 backend/tests/fixtures/tongcheng_page.json
把打印结果发回，就能把 app/collectors/tongcheng.py 的 FIELD_MAP 一次性对准。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402

URL = "https://www.ly.com/scenery/AjaxHelper/DianPingAjax.aspx"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.ly.com/scenery/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sid", required=True, help="同程景区 sid，例如 32289")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=10)
    parser.add_argument("--lab-id", type=int, default=6)
    parser.add_argument("--sort", type=int, default=0)
    parser.add_argument("--proxy", default="", help="可选：http://user:pass@ip:port")
    args = parser.parse_args()

    params = {
        "action": "GetDianPingList",
        "sid": args.sid,
        "page": args.page,
        "pageSize": args.page_size,
        "labId": args.lab_id,
        "sort": args.sort,
        "iid": f"0.{random.randint(10**16, 10**17 - 1)}",
    }

    try:
        with httpx.Client(headers=HEADERS, timeout=25,
                          proxy=args.proxy or None, follow_redirects=True) as client:
            response = client.get(URL, params=params)
    except httpx.HTTPError as exc:
        print(f"请求失败：{exc}", file=sys.stderr)
        return 1

    print(f"HTTP {response.status_code}，响应长度 {len(response.text)} 字符")
    body = response.text.strip()
    if not body:
        print("响应为空，可能被风控或参数不对", file=sys.stderr)
        return 1

    try:
        data = response.json()
    except ValueError:
        print("响应不是 JSON，前 500 字符如下：\n" + body[:500], file=sys.stderr)
        return 1

    print("\n顶层键名：", sorted(data.keys()) if isinstance(data, dict) else type(data).__name__)

    def find_list(node):
        if isinstance(node, list) and node and isinstance(node[0], dict):
            return node
        if isinstance(node, dict):
            for value in node.values():
                found = find_list(value)
                if found:
                    return found
        return None

    items = find_list(data)
    if items:
        print(f"评论条数：{len(items)}")
        print("单条评论键名：", sorted(items[0].keys()))
        print("\n第一条示例：")
        print(json.dumps(items[0], ensure_ascii=False, indent=2)[:1500])
    else:
        print("没找到评论列表，完整响应前 1000 字符：")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:1000])

    out = ROOT / "backend" / "tests" / "fixtures" / "tongcheng_page.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始响应已保存到：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
