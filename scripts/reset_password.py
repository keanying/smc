# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""在服务器上重置某个用户的密码（忘了管理员密码、管理员被锁住时用）。

    python scripts/reset_password.py admin            # 交互输入新密码
    python scripts/reset_password.py admin --admin    # 顺便把他改成管理员并启用

直接连 config.yaml 里的数据库，不经过网页，所以要在能连上数据库的机器上跑。
重置后会解锁，并让他所有地方的登录失效。
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


async def main() -> int:
    parser = argparse.ArgumentParser(description="重置用户密码")
    parser.add_argument("username")
    parser.add_argument("--admin", action="store_true", help="同时设成管理员并启用")
    args = parser.parse_args()

    from app.auth.passwords import check_strength
    from app.auth.repo import UserRepository
    from app.core.config import load_config
    from app.core.db import Database

    password = getpass.getpass("新密码：")
    if reason := check_strength(password):
        print(f"[错误] {reason}")
        return 2
    if getpass.getpass("再输一次：") != password:
        print("[错误] 两次输入不一样")
        return 2

    db = Database(load_config())
    await db.connect()
    try:
        users = UserRepository(db)
        row = await users.get_with_hash(args.username)
        if not row:
            if not args.admin:
                print(f"[错误] 没有用户 {args.username}（加 --admin 可以直接建一个管理员）")
                return 2
            await users.create(username=args.username, password=password, role="admin")
            print(f"已创建管理员 {args.username}")
            return 0
        await users.set_password(row["id"], password)
        await users.delete_sessions(row["id"])
        if args.admin:
            await users.update(row["id"], {"role": "admin", "enabled": 1})
        print(f"已重置 {args.username} 的密码{'，并设为启用的管理员' if args.admin else ''}")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
