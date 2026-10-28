# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""用户与登录：第一次创建管理员、登录、锁定、三种角色的权限、改密码踢掉其他登录、WebSocket 也要登录。

起一个真实的应用（TestClient，带生命周期），连独立测试库，跑完丢掉。
"""
from __future__ import annotations

import os

import pytest

TEST_DB = "scenic_media_auth_test"


@pytest.fixture(scope="module")
def app_client():
    saved = {k: os.environ.get(k) for k in
             ("SMC_MYSQL_DATABASE", "SMC_AUTH_ENABLED", "SMC_SCHEDULER_ENABLED",
              "SMC_AUTH_MAX_FAILED_LOGINS")}
    os.environ.update({"SMC_MYSQL_DATABASE": TEST_DB, "SMC_AUTH_ENABLED": "true",
                       "SMC_SCHEDULER_ENABLED": "false", "SMC_AUTH_MAX_FAILED_LOGINS": "3"})
    from app.core.config import load_config, reset_cache
    reset_cache()
    from fastapi.testclient import TestClient
    from app.main import create_app

    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        import pymysql
        cfg = load_config(use_cache=False)
        try:
            conn = pymysql.connect(host=cfg.get("mysql.host"), port=int(cfg.get("mysql.port")),
                                   user=cfg.get("mysql.user"), password=cfg.get("mysql.password"))
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_cache()


class _Browsers:
    """几个人用同一个客户端轮流操作：切换 Cookie 就是换了个浏览器。

    ⚠️ 不能再开第二个 TestClient：它会起自己的事件循环，而数据库连接池绑在第一个上。
    """

    def __init__(self, client):
        self.c = client
        self.tokens = {}

    def login(self, who, username, password):
        self.c.cookies.clear()
        r = self.c.post("/api/auth/login", json={"username": username, "password": password})
        if r.json()["code"] == 0:
            self.tokens[who] = self.c.cookies.get("smc_session")
        return r

    def use(self, who):
        self.c.cookies.clear()
        if who in self.tokens:
            self.c.cookies.set("smc_session", self.tokens[who])
        return self.c


def test_flow(app_client):
    c = app_client
    b = _Browsers(c)
    # 第一次：没有用户，接口都要登录，状态里说要创建管理员
    assert c.get("/api/tasks").status_code == 401
    assert c.get("/api/health").status_code == 200, "健康检查不用登录"
    status = c.get("/api/auth/status").json()["data"]
    assert status["enabled"] and status["needs_setup"] and status["user"] is None

    assert c.post("/api/auth/setup", json={"username": "admin", "password": "123"}).json()["code"] != 0, \
        "弱密码不行"
    r = c.post("/api/auth/setup", json={"username": "admin", "password": "Admin#2026",
                                        "display_name": "管理员甲"})
    assert r.json()["code"] == 0 and c.cookies.get("smc_session")
    b.tokens["admin"] = c.cookies.get("smc_session")
    assert c.post("/api/auth/setup", json={"username": "x2", "password": "Admin#2026"}).json()["code"] != 0, \
        "已经有用户了，setup 不能再用来建管理员"
    assert c.get("/api/tasks").status_code == 200

    # 管理员建两个用户
    assert c.post("/api/users", json={"username": "op", "password": "Operator#1",
                                      "role": "operator", "display_name": "操作员乙"}).json()["code"] == 0
    assert c.post("/api/users", json={"username": "ro", "password": "Viewer#123",
                                      "role": "viewer"}).json()["code"] == 0
    users = {u["username"]: u for u in c.get("/api/users").json()["data"]}
    assert set(users) == {"admin", "op", "ro"}
    assert "password_hash" not in users["op"]

    # 操作员：能干活，不能管用户、改系统设置
    assert b.login("op", "op", "Operator#1").json()["code"] == 0
    assert c.get("/api/users").status_code == 403
    assert c.put("/api/settings", json={"section": "crawl", "values": {}}).status_code == 403
    assert c.get("/api/settings").status_code == 200
    r = c.post("/api/tasks", json={"task_name": "操作员建的", "channels": ["ctrip"],
                                   "collect_type": "poi", "targets": [{"poi_id": "1"}],
                                   "schedule_type": "cron", "cron_expression": "0 3 * * *"})
    assert r.status_code == 200, r.text
    task = c.get(f"/api/tasks/{r.json()['data']['task_id']}").json()["data"]
    assert task["created_by"] == "操作员乙", "任务创建人记登录用户的显示名"

    # 只读：只能看
    assert b.login("ro", "ro", "Viewer#123").json()["code"] == 0
    assert c.get("/api/tasks").status_code == 200
    assert c.post("/api/tasks", json={"task_name": "x", "channels": ["ctrip"]}).status_code == 403
    assert c.put("/api/auth/password", json={"old_password": "Viewer#123",
                                             "new_password": "Viewer#456"}).json()["code"] == 0, \
        "只读用户也能改自己的密码"
    with c.websocket_connect("/ws/metrics/runs/none/logs") as ws:
        assert ws.receive_json()["type"] == "end", "登录了就能连 WebSocket"

    # WebSocket 没登录不让连
    from starlette.websockets import WebSocketDisconnect
    b.use("nobody")
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/tasks/whatever/logs") as ws:
            ws.receive_json()

    # 停用：马上踢下线
    b.use("admin")
    uid = users["op"]["id"]
    assert c.put(f"/api/users/{uid}", json={"enabled": False}).json()["code"] == 0
    b.use("op")
    assert c.get("/api/tasks").status_code == 401

    # 不能把最后一个管理员弄没
    b.use("admin")
    admin_id = users["admin"]["id"]
    assert c.put(f"/api/users/{admin_id}", json={"role": "viewer"}).json()["code"] != 0
    assert c.delete(f"/api/users/{admin_id}").json()["code"] != 0


def test_lockout_and_password_change(app_client):
    c = app_client
    b = _Browsers(c)
    for _ in range(3):
        assert b.login("bad", "admin", "wrong-pass").json()["code"] != 0
    r = b.login("bad", "admin", "Admin#2026")
    assert "锁" in r.json()["message"], "连续输错 3 次后，密码对了也要等解锁"

    # 用数据库解锁（管理员都被锁在外面时就是这么救的），再登两个地方
    import pymysql
    from app.core.config import load_config
    cfg = load_config(use_cache=False)
    conn = pymysql.connect(host=cfg.get("mysql.host"), port=int(cfg.get("mysql.port")),
                           user=cfg.get("mysql.user"), password=cfg.get("mysql.password"),
                           database=TEST_DB, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("UPDATE src_opinion_sys_user SET failed_count = 0, locked_until = NULL")
    conn.close()
    assert b.login("a1", "admin", "Admin#2026").json()["code"] == 0
    assert b.login("a2", "admin", "Admin#2026").json()["code"] == 0

    # 改密码：别的登录失效，当前这个保留
    b.use("a1")
    assert c.put("/api/auth/password", json={"old_password": "Admin#2026",
                                             "new_password": "Admin#2027x"}).json()["code"] == 0
    assert c.get("/api/tasks").status_code == 200
    b.use("a2")
    assert c.get("/api/tasks").status_code == 401

    # 退出
    b.use("a1")
    assert c.post("/api/auth/logout").json()["code"] == 0
    assert c.get("/api/tasks").status_code == 401


def test_role_rules():
    from app.auth.roles import denied_reason

    assert denied_reason("admin", "DELETE", "/api/users/3") == ""
    assert denied_reason("operator", "GET", "/api/users") != ""
    assert denied_reason("operator", "PUT", "/api/cluster/nodes/a/accept") != ""
    assert denied_reason("operator", "POST", "/api/accounts/douyin/a/login-session") == ""
    assert denied_reason("viewer", "POST", "/api/tasks") != ""
    assert denied_reason("viewer", "PUT", "/api/auth/password") == ""
    assert denied_reason("viewer", "", "/ws/browser/x", websocket=True) != ""
    assert denied_reason("viewer", "", "/ws/tasks/x/logs", websocket=True) == ""
