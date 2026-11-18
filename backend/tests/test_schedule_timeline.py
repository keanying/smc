# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""任务管理页「调度时间分布」：按调度设置推算一天里的启动时刻（planned_starts）。

纯函数，不连库。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.repositories.task_repo import planned_starts

DAY = datetime(2026, 11, 18)
NEXT_DAY = DAY + timedelta(days=1)
NOW = datetime(2026, 11, 18, 7, 20)


def _hm(result):
    return [s["time"].strftime("%H:%M") for s in result["starts"]]


def test_cron_only_future_part_of_today():
    task = {"schedule_type": "cron", "schedule_enabled": 1, "cron_expression": "0 2,14 * * *"}
    assert _hm(planned_starts(task, NOW, NEXT_DAY, now=NOW)) == ["14:00"]


def test_cron_whole_future_day_includes_midnight():
    task = {"schedule_type": "cron", "schedule_enabled": 1, "cron_expression": "0 */6 * * *"}
    result = planned_starts(task, DAY, NEXT_DAY, now=DAY - timedelta(days=1))
    assert _hm(result) == ["00:00", "06:00", "12:00", "18:00"]
    assert not any(s["approx"] for s in result["starts"])


def test_cron_six_fields_seconds_first():
    task = {"schedule_type": "cron", "schedule_enabled": 1, "cron_expression": "0 30 9 * * *"}
    assert _hm(planned_starts(task, DAY, NEXT_DAY, now=DAY)) == ["09:30"]


def test_interval_first_point_exact_rest_approx():
    task = {"schedule_type": "interval", "schedule_enabled": 1,
            "schedule_interval_seconds": 3 * 3600, "next_run_time": datetime(2026, 11, 18, 8)}
    result = planned_starts(task, NOW, NEXT_DAY, now=NOW)
    assert _hm(result) == ["08:00", "11:00", "14:00", "17:00", "20:00", "23:00"]
    assert [s["approx"] for s in result["starts"]] == [False] + [True] * 5


def test_interval_running_estimates_from_now():
    task = {"schedule_type": "interval", "schedule_enabled": 1,
            "schedule_interval_seconds": 6 * 3600, "next_run_time": datetime(2026, 11, 18, 6)}
    assert _hm(planned_starts(task, NOW, NEXT_DAY, now=NOW)) == ["13:20", "19:20"]


def test_interval_future_day_steps_into_window():
    task = {"schedule_type": "interval", "schedule_enabled": 1,
            "schedule_interval_seconds": 7 * 3600, "next_run_time": datetime(2026, 11, 18, 8)}
    result = planned_starts(task, NEXT_DAY, NEXT_DAY + timedelta(days=1), now=NOW)
    assert _hm(result) == ["05:00", "12:00", "19:00"]
    assert all(s["approx"] for s in result["starts"])


def test_waiting_resume_then_cron():
    task = {"schedule_type": "cron", "schedule_enabled": 1, "status": "waiting",
            "next_run_time": datetime(2026, 11, 18, 9, 30), "cron_expression": "0 */4 * * *"}
    result = planned_starts(task, NOW, NEXT_DAY, now=NOW)
    assert _hm(result) == ["09:30", "12:00", "16:00", "20:00"]
    assert result["starts"][0]["resume"] is True


def test_at_and_paused():
    at_task = {"schedule_type": "at", "schedule_enabled": 1, "schedule_at": datetime(2026, 11, 18, 21)}
    assert _hm(planned_starts(at_task, NOW, NEXT_DAY, now=NOW)) == ["21:00"]
    paused = {"schedule_type": "cron", "schedule_enabled": 0, "cron_expression": "0 * * * *"}
    assert planned_starts(paused, NOW, NEXT_DAY, now=NOW)["starts"] == []


def test_dense_cron_is_truncated():
    task = {"schedule_type": "cron", "schedule_enabled": 1, "cron_expression": "*/10 * * * * *"}
    result = planned_starts(task, NOW, NEXT_DAY, now=NOW, limit=100)
    assert len(result["starts"]) == 100 and result["truncated"] is True


def test_bad_cron_draws_nothing():
    task = {"schedule_type": "cron", "schedule_enabled": 1, "cron_expression": "not a cron"}
    assert planned_starts(task, NOW, NEXT_DAY, now=NOW) == {"starts": [], "truncated": False}


def test_past_window_is_empty():
    task = {"schedule_type": "cron", "schedule_enabled": 1, "cron_expression": "0 * * * *"}
    assert planned_starts(task, NEXT_DAY, NEXT_DAY, now=NOW)["starts"] == []
