# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""按景区的标注补充规则。

不同景区有自己的标注需求（比如 IP 景区要把剧名、角色名、演员都当关键词抽出来）。
采集系统在「景区管理 → 标注提示词」里给每个景区配：

    prompt          补充提示词，拼进这个景区每条评论的用户段（llm/prompt.py）
    broad_keywords  关键词放宽：不要求和整体情感同向、允许书名号全称、
                    长度和个数上限放大（labeling/postprocess.py）

这里只是一个进程内的登记表，由采集系统定时刷新（app/labeling/scenic_rules_sync.py）。
worker 线程只读，刷新时整表替换，不需要加锁。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional

__all__ = ["ScenicRule", "set_rules", "rule_for", "BROAD_KEYWORD_MAX_LENGTH", "BROAD_KEYWORD_MAX_COUNT"]

#: 放宽后每个关键词最长多少字、每条最多多少个
BROAD_KEYWORD_MAX_LENGTH = 30
BROAD_KEYWORD_MAX_COUNT = 20


@dataclass(frozen=True)
class ScenicRule:
    prompt: str = ""
    broad_keywords: bool = False


_rules: Dict[str, ScenicRule] = {}


def set_rules(rules: Mapping[str, ScenicRule]) -> None:
    global _rules
    _rules = {k: v for k, v in rules.items() if v.prompt.strip() or v.broad_keywords}


def rule_for(scenic_id: str) -> Optional[ScenicRule]:
    return _rules.get(scenic_id or "")
