# -*- coding: utf-8 -*-
"""
@Time ：2026/9/8 20:42
@Auth ：keanying
@File ：data_db_sync.py
@IDE  ：PyCharm
@Motto：ABC(Always Be Coding)
"""
"""跨机器迁移：把景区、任务、系统配置从一台机器搬到另一台。

典型场景：Windows 跑着，要整体搬到 Ubuntu。

    # 在 Windows 上（旧机器）
    python migrate.py export -o smc_migration.json

    # 把 json 拷到新机器，在 Ubuntu 上
    python migrate.py import smc_migration.json --dry-run   # 先看会做什么
    python migrate.py import smc_migration.json

=======================================================================
⚠️ 为什么不能直接 mysqldump 整库搬过去
=======================================================================

因为有一批**机器相关的路径**藏在数据库里，搬过去会静悄悄地把新机器搞坏：

`src_opinion_sys_setting` 表存的是「系统设置页」上的改动，服务启动时会
把它**合并进配置、并且覆盖 config.yaml**（见 setting_repo.apply_overrides）。
而设置接口返回的路径是**已经解析成绝对路径**的，所以只要你在 Windows 上
点过一次「调度与浏览器」的保存，库里就存着：

    browser.profiles_dir   = E:\\download\\scenicmediacollector\\smc\\data\\browser_profiles
    browser.executable_path = C:\\Program Files\\Google\\Chrome\\...\\chrome.exe
    browser.stealth_js     = E:\\download\\...\\backend\\libs\\stealth.min.js
    export.dir             = E:\\download\\...\\data\\exports

搬到 Ubuntu 之后：浏览器起不来，或者 profile 建到一个莫名其妙的地方。
最要命的是**你改 config.yaml 完全没用**——库里的覆盖优先级更高，
而报错信息只会说"浏览器启动失败"，根本不会提数据库里还存着一份配置。
这种问题不知道的话能查一整天。

所以这个脚本在导入时会**主动丢掉**这些路径项（见 MACHINE_SPECIFIC_KEYS），
让新机器的 config.yaml 说了算，并且把丢掉了什么、为什么丢，逐条打出来。

=======================================================================
不迁移什么，以及为什么
=======================================================================

**账号和登录态不迁移。** 每个账号对应一个 Chrome user-data-dir
（data/browser_profiles/<平台>/<账号名>/），那是一堆和操作系统、
Chrome 版本绑死的二进制文件，拷到 Linux 上不能用。
新机器上重新添加账号扫码登录即可——账号本身就几条记录，
比修一个"看着能用其实登录态是坏的"的 profile 省事得多。

如果不想重新扫码，可以在旧机器上用账号管理页的「Cookie」按钮把
Cookie 串复制出来，在新机器上用同一个对话框粘贴导入。

**采集到的数据（作品/评论/作者）不迁移。** 量大，而且和这次搬家无关；
要搬的话用 mysqldump 单独导那几张表更合适：

    mysqldump -u用户 -p 库名 src_opinion_social_work_di \\
        src_opinion_social_work_comment_di src_opinion_social_author > data.sql
"""
# from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.config import load_config          # noqa: E402
from app.core.db import Database                 # noqa: E402
from app.db import tables                        # noqa: E402

#: 导出格式版本。以后字段变了靠它判断兼容性。
FORMAT_VERSION = 1

#: 要迁移的表，按导入顺序排（景区必须在任务之前——任务引用 scenic_id）
SCENIC_TABLES = [
    (tables.SCENIC, "scenic_id"),
    (tables.SCENIC_KEYWORD, None),
    (tables.SCENIC_TARGET, None),
]
TASK_TABLES = [(tables.TASK, "task_id")]
SETTING_TABLES = [(tables.SETTING, "setting_key")]

#: 这些字段是"这台机器上跑成什么样"，不是"任务是怎么配的"。
#: 不清掉的话，新机器一上来就有一堆假的运行记录；
#: 更糟的是 status=running 的任务——调度器会以为它还在跑，
#: 那个任务就永远轮不到执行了。
TASK_RUNTIME_FIELDS = {
    "status": "pending",
    # ⚠️ progress 是 INT NOT NULL DEFAULT 0，写 None 会直接被 MySQL 拒掉。
    #    这种"重置成空"的字段一定要照着建表语句写，凭感觉给 None 会在
    #    导入到一半的时候炸——前面的景区已经写进去了，任务写了一半。
    "progress": 0,
    "next_run_time": None,
    "last_run_time": None,
    "runs_count": 0,
    "start_time": None,
    "end_time": None,
    "stat_new_works": 0,
    "stat_updated_works": 0,
    "stat_new_comments": 0,
    "stat_updated_comments": 0,
    "error": None,
}

#: 系统设置里**必须丢掉**的路径项：段名 -> 该段里的键
#: 丢掉之后这一项由新机器的 config.yaml 决定，这正是我们要的。
MACHINE_SPECIFIC_KEYS: Dict[str, Dict[str, str]] = {
    "browser": {
        "profiles_dir": "浏览器 profile 目录（每台机器不一样，而且 Windows 路径在 Linux 上无效）",
        "executable_path": "Chrome 可执行文件路径（Windows 的 .exe 路径在 Linux 上跑不起来）",
        "stealth_js": "stealth.min.js 的绝对路径（跟着项目目录走）",
    },
    "export": {
        "dir": "导出目录（跟着项目目录走）",
    },
    "server": {
        "data_dir": "数据目录（跟着项目目录走）",
    },
    "labeling": {
        "runtime_config_path": "标注引擎运行时配置的落盘位置（跟着项目目录走）",
    },
}


# --------------------------------------------------------------- 工具
def _jsonable(value: Any) -> Any:
    """datetime / Decimal / bytes 这些 json 不认识的类型，转成认识的。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def _row_to_json(row: Dict[str, Any]) -> Dict[str, Any]:
    # id 是自增主键，跟着旧库走没有意义，还可能和新库already有的记录撞上
    return {k: _jsonable(v) for k, v in row.items() if k != "id"}


def _looks_like_windows_path(value: Any) -> bool:
    """像不像一个 Windows 路径。用来兜底提醒，不参与删除决策。"""
    if not isinstance(value, str) or len(value) < 3:
        return False
    return (value[1:3] == ":\\") or value.startswith("\\\\") or "\\" in value and "/" not in value


# --------------------------------------------------------------- 导出
async def do_export(args: argparse.Namespace) -> int:
    config = load_config()
    db = Database(config)
    await db.connect()
    try:
        payload: Dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "source": {
                "platform": sys.platform,
                "project_root": str(Path(__file__).resolve().parents[1]),
                "database": config.get("mysql.database"),
            },
            "tables": {},
        }

        groups = []
        if not args.no_scenic:
            groups += SCENIC_TABLES
        if not args.no_task:
            groups += TASK_TABLES
        if not args.no_setting:
            groups += SETTING_TABLES

        for table, _key in groups:
            rows = await db.fetch_all(f"SELECT * FROM `{table}`")
            payload["tables"][table] = [_row_to_json(r) for r in rows]
            print(f"  导出 {table:42s} {len(rows):5d} 条")

        out = Path(args.output)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"\n✅ 已写入 {out.resolve()}")

        # 提前把"新机器上会被丢掉的东西"告诉用户，别等到导入才知道
        settings = payload["tables"].get(tables.SETTING, [])
        hits = _preview_dropped(settings)
        if hits:
            print("\n⚠️ 这些设置项是这台机器专属的，导入到新机器时会被**丢掉**")
            print("   （丢掉之后由新机器的 config.yaml 决定，这是有意的）：")
            for line in hits:
                print(f"     {line}")
        print("\n下一步：把这个文件拷到新机器的 backend/ 目录下，然后")
        print(f"    python migrate.py import {out.name} --dry-run")
        return 0
    finally:
        await db.close()


def _preview_dropped(setting_rows: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for row in setting_rows:
        section = row.get("setting_key")
        value = _parse_setting(row.get("setting_value"))
        if not isinstance(value, dict):
            continue
        for key, why in MACHINE_SPECIFIC_KEYS.get(section, {}).items():
            if key in value:
                lines.append(f"{section}.{key} = {value[key]!r}\n       → {why}")
        # 兜底：不在名单里、但长得像 Windows 路径的，提醒一声
        for key, val in value.items():
            if key in MACHINE_SPECIFIC_KEYS.get(section, {}):
                continue
            if _looks_like_windows_path(val):
                lines.append(f"{section}.{key} = {val!r}\n       → 看着像 Windows 路径，"
                             f"但不在已知名单里，**会原样搬过去**，请自己确认")
    return lines


def _parse_setting(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return raw


# --------------------------------------------------------------- 导入
def _scrub_setting(section: str, value: Any, report: List[str]) -> Any:
    """把机器相关的路径项从设置里摘掉。"""
    if not isinstance(value, dict):
        return value
    drop = MACHINE_SPECIFIC_KEYS.get(section, {})
    cleaned = {}
    for key, val in value.items():
        if key in drop:
            report.append(f"  丢弃 {section}.{key} = {val!r}\n        原因：{drop[key]}")
            continue
        cleaned[key] = val
    return cleaned


async def do_import(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"❌ 找不到文件：{path.resolve()}")
        return 1
    payload = json.loads(path.read_text(encoding="utf-8"))

    version = payload.get("format_version")
    if version != FORMAT_VERSION:
        print(f"❌ 导出文件版本是 {version}，这个脚本只认 {FORMAT_VERSION}。"
              f"请用同一个版本的 migrate.py 重新导出")
        return 1

    src = payload.get("source", {})
    print(f"来源：{src.get('platform')} / {src.get('project_root')} / 库 {src.get('database')}")
    print(f"导出于：{payload.get('exported_at')}")

    config = load_config()
    print(f"目标：{sys.platform} / 库 {config.get('mysql.database')}\n")

    if args.dry_run:
        print("—— 演练模式，不会写任何数据 ——\n")

    db = Database(config)
    await db.connect()
    try:
        report: List[str] = []
        total_new = total_upd = 0

        groups = []
        if not args.no_scenic:
            groups += SCENIC_TABLES
        if not args.no_task:
            groups += TASK_TABLES
        if not args.no_setting:
            groups += SETTING_TABLES

        for table, unique_key in groups:
            rows = payload["tables"].get(table)
            if rows is None:
                print(f"  跳过 {table}（导出文件里没有这张表）")
                continue

            prepared = []
            for row in rows:
                row = dict(row)
                if table == tables.TASK:
                    row.update(TASK_RUNTIME_FIELDS)
                if table == tables.SETTING:
                    parsed = _parse_setting(row.get("setting_value"))
                    cleaned = _scrub_setting(row.get("setting_key"), parsed, report)
                    if isinstance(cleaned, dict) and not cleaned:
                        report.append(f"  整段丢弃 {row.get('setting_key')}"
                                      f"（里面只有机器相关的路径项）")
                        continue
                    row["setting_value"] = json.dumps(cleaned, ensure_ascii=False)
                prepared.append(row)

            new, upd = await _upsert(db, table, prepared, unique_key,
                                     dry_run=args.dry_run)
            total_new += new
            total_upd += upd
            print(f"  {table:42s} 新增 {new:5d}  更新 {upd:5d}")

        # ---- 再洗一遍**目标库里已有的**设置 ----
        # ⚠️ 这一步不是多余的。真实顺序往往是：先试 mysqldump 整库搬 →
        #    浏览器起不来 → 才来用这个脚本。那时候 Windows 路径**已经在
        #    目标库里了**，而上面的清洗只作用于「这次导入的数据」，
        #    库里存量的那份会原封不动地留着继续生效。
        #    实测就是这样：设置表里躺着一条
        #    export = {"dir": "E:\\download\\...\\exports"}，
        #    导入报告全绿，服务照样用 Windows 路径。
        if not args.no_setting:
            existing = await db.fetch_all(
                f"SELECT setting_key, setting_value FROM `{tables.SETTING}`")
            for row in existing:
                section = row["setting_key"]
                if section not in MACHINE_SPECIFIC_KEYS:
                    continue
                parsed = _parse_setting(row["setting_value"])
                if not isinstance(parsed, dict):
                    continue
                before = dict(parsed)
                local: List[str] = []
                cleaned = _scrub_setting(section, parsed, local)
                if cleaned == before:
                    continue
                report.extend(f"  [目标库存量] {line.strip()}" for line in local)
                if not args.dry_run:
                    if cleaned:
                        await db.execute(
                            f"UPDATE `{tables.SETTING}` SET setting_value = %s "
                            f"WHERE setting_key = %s",
                            [json.dumps(cleaned, ensure_ascii=False), section])
                    else:
                        await db.execute(
                            f"DELETE FROM `{tables.SETTING}` WHERE setting_key = %s",
                            [section])
                        report.append(f"  [目标库存量] 整段删除 {section}"
                                      f"（只剩机器相关的路径项）")

        if report:
            print("\n⚠️ 机器相关的配置项已丢掉（由本机 config.yaml 决定，这是有意的）：")
            for line in report:
                print(line)

        if not args.no_task:
            print("\n说明：导入的任务全部重置为「待执行」，运行统计清零——"
                  "\n      旧机器上的运行记录搬过来没有意义，而 status=running 的任务"
                  "\n      会让调度器以为它还在跑，那个任务就永远轮不到执行了。")

        print(f"\n{'（演练）' if args.dry_run else '✅'} 合计 新增 {total_new} 条，更新 {total_upd} 条")
        if args.dry_run:
            print("确认无误后去掉 --dry-run 再跑一次")
        else:
            print("\n下一步：")
            print("  1. 检查 config/config.yaml 里的 mysql / redis / browser 段是不是本机的")
            print("  2. 到账号管理页重新添加账号并扫码登录（登录态无法跨机器迁移）")
            print("  3. 启动服务，去系统设置页面核对一遍配置")
        return 0
    finally:
        await db.close()


async def _upsert(db: Database, table: str, rows: List[Dict[str, Any]],
                  unique_key: str | None, *, dry_run: bool) -> tuple[int, int]:
    """按唯一键插入或更新。没有唯一键的表（景区关键字、平台目标）整表替换。"""
    if not rows:
        return 0, 0

    if unique_key is None:
        # 这类表是"景区的附属明细"，没有业务唯一键，一条条对不上。
        # 整表替换最省心，也符合直觉：迁移就是让新机器和旧机器一致。
        if not dry_run:
            await db.execute(f"DELETE FROM `{table}`")
            for row in rows:
                cols = list(row.keys())
                await db.execute(
                    f"INSERT INTO `{table}` ({', '.join(f'`{c}`' for c in cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))})",
                    [row[c] for c in cols],
                )
        return len(rows), 0

    new = upd = 0
    for row in rows:
        key_value = row.get(unique_key)
        exists = await db.fetch_value(
            f"SELECT COUNT(*) AS c FROM `{table}` WHERE `{unique_key}` = %s",
            [key_value], 0)
        if exists:
            upd += 1
            if not dry_run:
                fields = [c for c in row if c != unique_key]
                await db.execute(
                    f"UPDATE `{table}` SET "
                    f"{', '.join(f'`{c}` = %s' for c in fields)} "
                    f"WHERE `{unique_key}` = %s",
                    [row[c] for c in fields] + [key_value],
                )
        else:
            new += 1
            if not dry_run:
                cols = list(row.keys())
                await db.execute(
                    f"INSERT INTO `{table}` ({', '.join(f'`{c}`' for c in cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))})",
                    [row[c] for c in cols],
                )
    return new, upd


# --------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(
        description="景区 / 任务 / 系统配置的跨机器迁移（如 Windows → Ubuntu）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("export", help="从当前机器导出到 json")
    ex.add_argument("-o", "--output", default="smc_migration.json", help="输出文件名")

    im = sub.add_parser("import", help="把 json 导入当前机器")
    im.add_argument("file", help="导出的 json 文件")
    im.add_argument("--dry-run", action="store_true",
                    help="只看会做什么，不写任何数据（建议先跑一次）")

    for p in (ex, im):
        p.add_argument("--no-scenic", action="store_true", help="不处理景区")
        p.add_argument("--no-task", action="store_true", help="不处理任务")
        p.add_argument("--no-setting", action="store_true", help="不处理系统设置")

    args = parser.parse_args()
    if args.command == "export":
        return asyncio.run(do_export(args))
    return asyncio.run(do_import(args))


if __name__ == "__main__":
    sys.exit(main())