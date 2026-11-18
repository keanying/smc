# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""任务管理页底部「今日调度」：按调度设置列出一天里的启动时刻（planned_starts）。

只要设置了调度就画，不管暂停没暂停、跑没跑过。纯函数，不连库。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.repositories.task_repo import planned_starts

DAY = datetime(2026, 11, 18)
NEXT_DAY = DAY + timedelta(days=1)


def _hm(result):
    return [s["time"].strftime("%H:%M") for s in result["starts"]]


def test_cron_whole_day_includes_midnight():
    task = {"schedule_type": "cron", "schedule_enabled": 1, "cron_expression": "0 */6 * * *"}
    result = planned_starts(task, DAY, NEXT_DAY)
    assert _hm(result) == ["00:00", "06:00", "12:00", "18:00"]
    assert not any(s["approx"] for s in result["starts"])


def test_cron_paused_still_listed():
    """暂停了定时、上次跑完很久了，也照样按设置画出来"""
    task = {"schedule_type": "cron", "schedule_enabled": 0, "status": "canceled",
            "cron_expression": "0 5,16 * * *", "next_run_time": datetime(2026, 9, 10, 16)}
    assert _hm(planned_starts(task, DAY, NEXT_DAY)) == ["05:00", "16:00"]


def test_cron_six_fields_seconds_first():
    task = {"schedule_type": "cron", "cron_expression": "0 30 9 * * *"}
    assert _hm(planned_starts(task, DAY, NEXT_DAY)) == ["09:30"]


def test_interval_spreads_both_ways_from_next_run():
    task = {"schedule_type": "interval", "schedule_interval_seconds": 6 * 3600,
            "next_run_time": datetime(2026, 11, 18, 8)}
    result = planned_starts(task, DAY, NEXT_DAY)
    assert _hm(result) == ["02:00", "08:00", "14:00", "20:00"]
    assert [s["approx"] for s in result["starts"]] == [True, False, True, True]


def test_interval_anchor_on_another_day():
    task = {"schedule_type": "interval", "schedule_interval_seconds": 7 * 3600,
            "next_run_time": datetime(2026, 11, 17, 8)}
    assert _hm(planned_starts(task, DAY, NEXT_DAY)) == ["05:00", "12:00", "19:00"]


def test_interval_without_next_run_uses_last_run():
    task = {"schedule_type": "interval", "schedule_interval_seconds": 12 * 3600,
            "next_run_time": None, "last_run_time": datetime(2026, 11, 10, 3, 15)}
    assert _hm(planned_starts(task, DAY, NEXT_DAY)) == ["03:15", "15:15"]


def test_at_only_on_its_day():
    task = {"schedule_type": "at", "schedule_enabled": 0, "schedule_at": datetime(2026, 11, 18, 21)}
    assert _hm(planned_starts(task, DAY, NEXT_DAY)) == ["21:00"]
    assert planned_starts(task, NEXT_DAY, NEXT_DAY + timedelta(days=1))["starts"] == []


def test_once_has_no_schedule():
    assert planned_starts({"schedule_type": "once"}, DAY, NEXT_DAY)["starts"] == []


def test_dense_cron_is_truncated():
    task = {"schedule_type": "cron", "cron_expression": "*/10 * * * * *"}
    result = planned_starts(task, DAY, NEXT_DAY, limit=100)
    assert len(result["starts"]) == 100 and result["truncated"] is True


def test_bad_cron_draws_nothing():
    task = {"schedule_type": "cron", "cron_expression": "not a cron"}
    assert planned_starts(task, DAY, NEXT_DAY) == {"starts": [], "truncated": False}
