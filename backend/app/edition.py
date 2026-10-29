# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""版本：总部版（full，默认）和景区版（scenic，部署在景区的盒子上）。

景区版是同一套代码换个开关（config.yaml 的 edition.name 或环境变量 SMC_EDITION_NAME=scenic）：

- 景区由总部分配：总部在「景区管理」里勾选景区导出「分配包」，盒子上导入。
  盒子上看得到、能用来建任务，但**改不了**景区本身（关键字、目标都跟着分配包走）；
- 采集在盒子上跑，采集账号盒子自己加、自己维护；
- 看不到也调不了：系统设置、集群、景区档案、景区管理、指标计算。

限制按「方法 + 路径」统一在中间件里卡（和 auth/roles.py 一个思路），不靠前端藏菜单——
藏菜单只是看不见，接口不卡照样能调。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

EDITIONS = ("full", "scenic")
EDITION_LABELS = {"full": "总部版", "scenic": "景区版"}

BUNDLE_KIND = "cloudsense.scenic-bundle"
BUNDLE_VERSION = 1
#: 盒子上记录「当前分配」的设置键（不在 OVERRIDABLE_SECTIONS 里，不会被合并进配置）
ASSIGNMENT_KEY = "edition.assignment"

_READ = ("GET", "HEAD", "OPTIONS")

#: 景区版里只能看、不能改的接口
_READ_ONLY = (
    ("/api/scenics", "景区由总部分配，盒子上不能改；要调整请联系总部重新下发"),
    ("/api/settings", "景区版没有系统设置"),
    ("/api/cluster", "景区版没有集群管理"),
    ("/api/archive", "景区版没有景区档案"),
    ("/api/qunar", "景区由总部分配，盒子上不能导入景区"),
    ("/api/metrics", "景区版没有指标计算"),
)


def edition_of(config) -> str:
    name = str(config.get("edition.name", "full") or "full").strip().lower()
    return name if name in EDITIONS else "full"


#: 本进程的版本。create_app() 启动时按配置设一次；中间件和接口都读它
_current = "full"


def set_current(config) -> str:
    global _current
    _current = edition_of(config)
    return _current


def current() -> str:
    return _current


def is_scenic() -> bool:
    """本进程是不是景区版（按 set_current 设好的那个）。"""
    return _current == "scenic"


def denied_reason(edition: str, method: str, path: str) -> str:
    """景区版不让做的操作返回原因，允许返回空串。总部版什么都不卡（角色另有 roles.py 管）。"""
    if edition != "scenic":
        return ""
    if path.startswith("/api/edition/") and not path.startswith("/api/edition/bundle/import"):
        # 总部那边的「导出分配包」在盒子上没有意义
        return "景区版不能导出分配包" if path.startswith("/api/edition/bundle") else ""
    if method in _READ:
        return ""
    if path == "/api/settings" and method == "PUT":
        return ""                             # 账号额度要能改；具体哪段能改由接口按 settings_allowed 判
    for prefix, reason in _READ_ONLY:
        if path.startswith(prefix):
            return reason
    return ""


def settings_allowed(edition: str, section: str, values: Dict[str, Any]) -> bool:
    """景区版唯一能在页面上改的设置：账号管理里的「账号额度」（crawl.account_quota）。"""
    if edition != "scenic":
        return True
    return section == "crawl" and bool(values) and set(values) <= {"account_quota"}


def info(config) -> Dict[str, Any]:
    edition = _current
    return {
        "edition": edition,
        "label": EDITION_LABELS[edition],
        "box_name": str(config.get("edition.box_name", "") or ""),
    }


# ---------------------------------------------------------------------- 分配包
async def export_bundle(scenics, scenic_ids: Sequence[str], *, box_name: str = "",
                        exported_by: str = "") -> Dict[str, Any]:
    """总部：把选中的景区（连同关键字、附加/过滤词、采集目标）打成一个分配包。"""
    ids = [s.strip() for s in scenic_ids if s and s.strip()]
    if not ids:
        raise ValueError("先选要分配的景区")
    items: List[Dict[str, Any]] = []
    missing: List[str] = []
    for scenic_id in dict.fromkeys(ids):
        row = await scenics.get_scenic(scenic_id)
        if not row:
            missing.append(scenic_id)
            continue
        keywords = await scenics.list_keywords(scenic_id, enabled_only=True)
        words = await scenics.list_filter_words(scenic_id, enabled_only=True)
        targets = await scenics.list_targets(scenic_id, enabled_only=True)
        items.append({
            "scenic_id": row["scenic_id"], "scenic_name": row["scenic_name"],
            "province": row.get("province"), "city": row.get("city"), "remark": row.get("remark"),
            "keywords": [k["keyword"] for k in keywords],
            "filter_words": {kind: [w["word"] for w in words if w["kind"] == kind]
                             for kind in ("aux", "exclude")},
            "targets": [{k: t.get(k) for k in ("channel", "target_type", "target_id",
                                               "target_name", "target_url", "extra")}
                        for t in targets],
        })
    if missing:
        raise ValueError(f"这些景区不存在：{'、'.join(missing)}")
    return {
        "kind": BUNDLE_KIND, "version": BUNDLE_VERSION,
        "box_name": box_name.strip(), "exported_by": exported_by,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scenics": items,
    }


def parse_bundle(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, (bytes, str)):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"分配包不是合法的 JSON：{exc}") from exc
    if not isinstance(raw, dict) or raw.get("kind") != BUNDLE_KIND:
        raise ValueError("这不是云听的景区分配包（请用总部「景区管理 → 分配到景区盒子」导出的文件）")
    if int(raw.get("version") or 0) > BUNDLE_VERSION:
        raise ValueError("分配包是新版本导出的，请先把盒子升级到和总部一样的版本")
    scenics = raw.get("scenics")
    if not isinstance(scenics, list) or not scenics:
        raise ValueError("分配包里没有景区")
    for item in scenics:
        if not isinstance(item, dict) or not str(item.get("scenic_id") or "").strip() \
                or not str(item.get("scenic_name") or "").strip():
            raise ValueError("分配包里有景区缺少编号或名称")
    return raw


async def import_bundle(db, scenics, settings, bundle: Dict[str, Any], *,
                        imported_by: str = "") -> Dict[str, Any]:
    """盒子：导入分配包。盒子上的景区和总部下发的**完全一致**：

    - 包里的景区：新建或更新，关键字 / 附加过滤词 / 采集目标都换成包里的那一份；
    - 以前分配过、这次包里没有的：停用（不删，已采集的数据还在，只是不能再建任务）。
    """
    from .db import tables as T

    bundle = parse_bundle(bundle)
    ids = [str(item["scenic_id"]).strip() for item in bundle["scenics"]]
    previous = await assignment(settings)
    for item in bundle["scenics"]:
        scenic_id = str(item["scenic_id"]).strip()
        await scenics.upsert_scenic({
            "scenic_id": scenic_id, "scenic_name": str(item["scenic_name"]).strip(),
            "province": item.get("province"), "city": item.get("city"),
            "remark": item.get("remark"), "enabled": 1,
        })
        async with db.transaction() as cur:
            await cur.execute("DELETE FROM `src_opinion_scenic_keyword` WHERE scenic_id = %s", [scenic_id])
            await cur.execute(f"DELETE FROM `{T.SCENIC_FILTER_WORD}` WHERE scenic_id = %s", [scenic_id])
        await scenics.add_keywords(scenic_id, [str(k) for k in item.get("keywords") or []])
        for kind, words in (item.get("filter_words") or {}).items():
            if kind in scenics.FILTER_KINDS:
                await scenics.add_filter_words(scenic_id, kind, [str(w) for w in words or []])
        keep = []
        for target in item.get("targets") or []:
            if not target.get("channel") or not target.get("target_id"):
                continue
            await scenics.upsert_target({**target, "scenic_id": scenic_id, "enabled": 1})
            keep.append((target["channel"], target.get("target_type") or "poi", str(target["target_id"])))
        for old in await scenics.list_targets(scenic_id):
            if (old["channel"], old["target_type"], str(old["target_id"])) not in keep:
                await scenics.delete_target(old["id"])
    dropped = [s for s in (previous or {}).get("scenic_ids", []) if s not in ids]
    if dropped:
        placeholders = ", ".join(["%s"] * len(dropped))
        await db.execute(
            f"UPDATE `src_opinion_social_scenic` SET enabled = 0 WHERE scenic_id IN ({placeholders})",
            dropped)
    record = {
        "scenic_ids": ids, "box_name": bundle.get("box_name") or "",
        "exported_by": bundle.get("exported_by") or "", "exported_at": bundle.get("exported_at") or "",
        "imported_by": imported_by, "imported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    await settings.set(ASSIGNMENT_KEY, record)
    return {**record, "dropped": dropped}


async def assignment(settings) -> Optional[Dict[str, Any]]:
    value = await settings.get(ASSIGNMENT_KEY)
    return value if isinstance(value, dict) else None
