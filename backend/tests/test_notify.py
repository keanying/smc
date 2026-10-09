"""飞书通知：格式是用户定的，别自己加戏；失败绝不能带崩采集。"""
from __future__ import annotations

import pytest

from app.notify.feishu import EVENT_LABELS, FeishuNotifier, NotifyEvent


class _Cfg:
    def __init__(self, **notify):
        base = {"enabled": True, "feishu_webhook": "https://open.feishu.cn/x",
                "remote_account": "11111", "remote_password": "1111",
                "cooldown_seconds": 600}
        base.update(notify)
        self._notify = base

    def get(self, path, default=None):
        return {"notify": self._notify}.get(path, default)


def test_正文严格按用户给的格式():
    """用户给的模板：

        舆情采集需人工验证：
        平台：抖音
        验证项：验证码或登录/采集错误（错误信息）
        请使用向日葵远程登录操作：
        远程账号：11111
        远程密码：1111
    """
    n = FeishuNotifier(_Cfg())
    text = n.render(NotifyEvent(channel="douyin", kind="captcha"))
    lines = text.splitlines()
    assert lines[0] == "舆情采集需人工验证："
    assert lines[1] == "平台：抖音", "平台要用中文名，不是 douyin"
    assert lines[2] == "验证项：验证码"
    assert "请使用向日葵远程登录操作：" in lines
    assert "远程账号：11111" in lines
    assert "远程密码：1111" in lines


def test_采集错误要把错误信息带上():
    """不带的话人得去翻日志才知道出了什么事。"""
    n = FeishuNotifier(_Cfg())
    text = n.render(NotifyEvent(channel="kuaishou", kind="error",
                                detail="Locator.click: Timeout 8000ms"))
    assert "采集错误（Locator.click: Timeout 8000ms）" in text


@pytest.mark.parametrize("kind,expect", list(EVENT_LABELS.items()))
def test_每种事件都有中文验证项(kind, expect):
    n = FeishuNotifier(_Cfg())
    assert expect in n.render(NotifyEvent(channel="douyin", kind=kind))


def test_没配远程账号要写明而不是留空():
    """留空的话收到通知的人不知道是"没配"还是"消息坏了"。"""
    n = FeishuNotifier(_Cfg(remote_account="", remote_password=""))
    text = n.render(NotifyEvent(channel="douyin", kind="captcha"))
    assert "远程账号：（未配置）" in text


def test_没开开关就不发():
    n = FeishuNotifier(_Cfg(enabled=False))
    assert n.enabled is False


def test_没配地址就算开了也不发():
    """开关开着但地址空着，enabled 要是 False——否则每次都去 POST 一个空地址。"""
    n = FeishuNotifier(_Cfg(feishu_webhook=""))
    assert n.enabled is False


@pytest.mark.asyncio
async def test_同一个问题在冷却期内只发一次():
    """一轮采集能撞上同一个问题几十次，全发出去真出事反而被淹掉。"""
    sent = []

    class _N(FeishuNotifier):
        async def _post(self, text):
            sent.append(text)
            return True

    n = _N(_Cfg())
    # 直接操作冷却表来验语义，不打网络
    event = NotifyEvent(channel="douyin", kind="captcha")
    assert not n._cooling(event.dedup_key)
    n._last_sent[event.dedup_key] = __import__("time").time()
    assert n._cooling(event.dedup_key), "刚发过却不在冷却期"

    # 换一类问题就不该被冷却挡住
    other = NotifyEvent(channel="douyin", kind="error")
    assert not n._cooling(other.dedup_key)
    # 换平台也不该
    another = NotifyEvent(channel="kuaishou", kind="captcha")
    assert not n._cooling(another.dedup_key)


@pytest.mark.asyncio
async def test_发送失败不抛异常():
    """调用方在采集的关键路径上，通知失败不能中断它。"""
    n = FeishuNotifier(_Cfg(feishu_webhook="http://127.0.0.1:1/不存在"))
    result = await n.send(NotifyEvent(channel="douyin", kind="captcha"))
    assert result is False       # 返回 False，而不是抛


@pytest.mark.asyncio
async def test_未初始化时采集侧拿到的是空壳():
    from app.notify.feishu import get_notifier

    n = get_notifier()           # 不传 config
    assert await n.send(NotifyEvent(channel="douyin", kind="captcha")) is False
