# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""推流画面回传的鼠标事件取值：登录窗口（live_session）和任务实时画面（live_view）共用。

前端（frontend/src/composables/remoteInput.ts）只发按下 / 松开，不再另发 click；
连击次数放在 clickCount 里，Chromium 按它产生 click / dblclick。
"""
from __future__ import annotations

from typing import Any, Dict

__all__ = ["mouse_button", "click_count"]


def mouse_button(event: Dict[str, Any]) -> str:
    """鼠标键只认 left / middle / right，别的一律当左键。"""
    button = str(event.get("button") or "left")
    return button if button in ("left", "middle", "right") else "left"


def click_count(event: Dict[str, Any]) -> int:
    """连击次数：1 单击、2 双击、3 三击（选中一整段），越界的夹回来。"""
    try:
        return min(max(int(event.get("clickCount", 1)), 1), 3)
    except (TypeError, ValueError):
        return 1
