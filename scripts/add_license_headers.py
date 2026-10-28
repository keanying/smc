"""给仓库里每个源文件加许可声明头。可以重复跑，已经有声明的文件会跳过。

    python scripts/add_license_headers.py           # 补上缺的
    python scripts/add_license_headers.py --check   # 只检查，缺了就列出来并返回 1

新建文件后跑一次即可。文件范围取 `git ls-files`；JSON、Markdown、图片、锁文件
没有注释语法或不是源码，不加。第三方文件用各自的声明（见 THIRD_PARTY_NOTICES.md）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
MARKER = "SPDX-License-Identifier:"
SELF = "scripts/add_license_headers.py"

HEADER = [
    "景区社媒采集系统 · Copyright (c) 2026 keanying",
    "SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0",
    "按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，",
    "不得用于大规模抓取、绕过平台防护或非法获取个人信息。",
    "首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。",
]

MEDIACRAWLER = [
    "本文件参考了 MediaCrawler 的逻辑（Copyright (c) 2024 relakkes@gmail.com，",
    "NON-COMMERCIAL LEARNING LICENSE 1.1），同时受其许可证约束，见 THIRD_PARTY_NOTICES.md。",
]

#: 第三方文件：版权不归本项目，只写明来源和各自的许可证
THIRD_PARTY = {
    "backend/libs/douyin.cjs": [
        "SPDX-License-Identifier: LicenseRef-ShilongLee-Crawler-NC-1.0",
        "第三方代码：原样复制自 ShilongLee/Crawler，版权归原作者",
        "（版权所有 (c) 2024 lishilong0101@gmail.com），按其《非商业使用许可证 1.0》授权，禁止商用。",
        "随「景区社媒采集系统」分发时同时受根目录 LICENSE 约束，见 THIRD_PARTY_NOTICES.md。",
    ],
    "backend/libs/stealth.min.js": [
        "SPDX-License-Identifier: MIT",
        "第三方代码：由 puppeteer-extra 的 extract-stealth-evasions 生成，版权归原作者，MIT 许可。",
        "随「景区社媒采集系统」分发，见 THIRD_PARTY_NOTICES.md。",
    ],
}

HASH = {".py", ".sh", ".yaml", ".yml", ".ini", ".conf", ".txt", ".ps1", ".example", ".toml"}
SLASH = {".ts", ".tsx", ".js", ".cjs", ".mjs"}
MARKUP = {".vue", ".html"}
SKIP_NAMES = {"pnpm-lock.yaml", "package-lock.json", "yarn.lock"}


def style_of(path: str) -> Optional[str]:
    name = Path(path).name
    if name in SKIP_NAMES:
        return None
    if name.startswith("Dockerfile") or name == ".gitignore":
        return "hash"
    suffix = Path(path).suffix.lower()
    if suffix in HASH:
        return "hash"
    if suffix in SLASH:
        return "slash"
    if suffix in MARKUP:
        return "markup"
    if suffix == ".css":
        return "css"
    if suffix == ".sql":
        return "sql"
    if suffix == ".bat":
        return "bat"
    return None


def render(style: str, lines: List[str]) -> List[str]:
    if style == "hash":
        return [f"# {ln}" for ln in lines]
    if style == "slash":
        return [f"// {ln}" for ln in lines]
    if style == "sql":
        return [f"-- {ln}" for ln in lines]
    if style == "bat":
        return [f"REM {ln}" for ln in lines]
    if style == "css":
        return ["/*"] + [f" * {ln}" for ln in lines] + [" */"]
    return ["<!--"] + [f"  {ln}" for ln in lines] + ["-->"]


def keep_first(style: str, path: str, lines: List[str]) -> int:
    """开头必须留在第一行的东西：shebang、编码声明、Dockerfile 指令、doctype、@echo off。"""
    n = 0
    if style == "hash":
        if lines and lines[0].startswith("#!"):
            n = 1
        if path.endswith(".py") and len(lines) > n and "coding" in lines[n] and lines[n].startswith("#"):
            n += 1
        if Path(path).name.startswith("Dockerfile") and lines and lines[0].lower().startswith(("# syntax=", "# escape=")):
            n = 1
    elif style == "markup" and lines and lines[0].lower().startswith("<!doctype"):
        n = 1
    elif style == "bat" and lines and lines[0].strip().lower() == "@echo off":
        n = 1
    return n


def header_lines(path: str, text: str) -> List[str]:
    if path in THIRD_PARTY:
        return THIRD_PARTY[path]
    if path != SELF and "mediacrawler" in text.lower():
        return HEADER + MEDIACRAWLER
    return HEADER


def process(path: str, check: bool) -> bool:
    """返回 True 表示这个文件缺声明（check 模式）或刚补上。"""
    style = style_of(path)
    file = ROOT / path
    if style is None or not file.is_file():
        return False
    raw = file.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    if MARKER in text[:2000]:
        return False
    if check:
        return True
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    n = keep_first(style, path, lines)
    block = render(style, header_lines(path, text))
    rest = lines[n:]
    if rest and rest[0].strip():
        block.append("")
    out = newline.join(lines[:n] + block + rest)
    if text.endswith(("\n", "\r\n")) or not text:
        out += newline
    file.write_bytes((b"\xef\xbb\xbf" if bom else b"") + out.encode("utf-8"))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只检查，不改文件")
    args = parser.parse_args()
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                           capture_output=True).stdout.decode("utf-8").split("\0")
    hit = [f for f in files if f and process(f, args.check)]
    if args.check:
        for f in hit:
            print(f"缺许可声明：{f}")
        return 1 if hit else 0
    print(f"补了 {len(hit)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
