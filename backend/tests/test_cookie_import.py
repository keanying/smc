"""Cookie 解析与登录态判定。

这两件事一起测，是因为它们串起来才是用户遇到的那个 bug：
profile 里只有游客 Cookie -> 被判成已登录 -> 任务跑起来 ->
抖音回 status_code=2483「请先登录再继续搜索吧」。
"""
from __future__ import annotations

import json

import pytest

from app.browser import cookie_import as ci
from app.browser.specs import get_spec, missing_login_cookies

# 用户真实抖音 Cookie 里的片段（值已截短），保留原样的分隔方式：分号后没有空格
REAL_DOUYIN_HEADER = (
    "UIFID=a3682da019905bd2868511de77147b86;__security_server_data_status=1;"
    "SEARCH_RESULT_LIST_TYPE=%22single%22;is_staff_user=false;"
    "sessionid_ss=679ceb84f2c9cdfb179222274a40041a;login_time=1787564273587;"
    "passport_csrf_token=0ee9d9fb904cfa642d9377c992f51dfa;"
    "sid_guard=679ceb84f2c9cdfb179222274a40041a%7C1787564274%7C5184000;"
    "ttwid=1%7CnSMWL4VWA5MVTar2YBibt1pz587lv2XNoEFUXN9d5gM;"
    "sessionid=679ceb84f2c9cdfb179222274a40041a;"
    "sid_tt=679ceb84f2c9cdfb179222274a40041a;"
    "uid_tt=f2e6d7021ce30a74de485c4d42053743;"
    "bd_ticket_guard_client_data=eyJiZC10aWNrZXQtZ3VhcmQtdmVyc2lvbiI6Mn0%3D;"
    "__ac_nonce=06a8f99c100abcd7c8c4d;"
    "x_tt_token=00679ceb84f2c9cdfb179222274a40041a001f5d79ddfea3f081ea"
)

# 用户报告问题时 profile 里的那 4 个 Cookie —— 全是游客态
ANONYMOUS_DOUYIN_HEADER = (
    "ttwid=1%7CnSMWL4VWA5MVTar2YBibt1pz587lv2XNoEFUXN9d5gM; "
    "passport_csrf_token=0ee9d9fb904cfa642d9377c992f51dfa; "
    "passport_csrf_token_default=0ee9d9fb904cfa642d9377c992f51dfa; "
    "s_v_web_id=verify_mt71lg7f_0O4prmvM"
)

COOKIE_EDITOR_JSON = json.dumps([
    {
        "domain": ".douyin.com", "expirationDate": 1792748274.5, "hostOnly": False,
        "httpOnly": True, "name": "sessionid", "path": "/",
        "sameSite": "no_restriction", "secure": True, "session": False,
        "storeId": "0", "value": "679ceb84f2c9cdfb179222274a40041a",
    },
    {
        "domain": ".douyin.com", "hostOnly": False, "httpOnly": False,
        "name": "ttwid", "path": "/", "sameSite": "unspecified",
        "secure": False, "session": True, "storeId": "0", "value": "1%7CnSMWL4",
    },
    {
        "domain": "www.douyin.com", "hostOnly": True, "httpOnly": False,
        "name": "uid_tt", "path": "/", "sameSite": "lax",
        "secure": False, "session": False, "expirationDate": 1792748274,
        "storeId": "0", "value": "f2e6d7021ce30a74",
    },
])


# ---------------------------------------------------------------------------
# 登录态判定：这条测试就是用户那个 bug 的复现
# ---------------------------------------------------------------------------

def test_anonymous_douyin_cookies_are_not_logged_in():
    """4 个游客 Cookie 不能被当成已登录。

    修之前 passport_csrf_token 在 session_cookies 里，而抖音对**未登录访客**
    也会下发这个 key，于是判定通过、任务照跑，直到接口回 2483 才暴露。
    """
    missing = missing_login_cookies("douyin", ANONYMOUS_DOUYIN_HEADER)
    assert missing, "只有游客 Cookie 却被判成已登录了"
    assert "sessionid" in missing


def test_real_douyin_cookies_are_logged_in():
    assert missing_login_cookies("douyin", REAL_DOUYIN_HEADER) == []


def test_csrf_token_alone_is_not_enough():
    assert missing_login_cookies("douyin", "passport_csrf_token=abc") != []
    assert missing_login_cookies("douyin", "passport_csrf_token_default=abc") != []


def test_empty_value_does_not_count():
    """sessionid= 空值不算登录。"""
    assert missing_login_cookies("douyin", "sessionid=; ttwid=1") != []


@pytest.mark.parametrize("channel,anonymous_key", [
    ("douyin", "passport_csrf_token"),
    ("kuaishou", "userId"),
    ("xiaohongshu", "customerClientId"),
])
def test_known_anonymous_keys_are_not_credentials(channel, anonymous_key):
    """这些 key 游客也会拿到，都曾经（或差点）被当成登录凭据。"""
    spec = get_spec(channel)
    assert anonymous_key not in spec.session_cookies, (
        f"{channel} 把游客也有的 {anonymous_key} 当成了登录凭据"
    )


def test_channels_without_login_never_report_missing():
    assert missing_login_cookies("ctrip", "") == []
    assert missing_login_cookies("tongcheng", "anything=1") == []


# ---------------------------------------------------------------------------
# 请求头串解析
# ---------------------------------------------------------------------------

def test_parses_header_without_spaces_after_semicolon():
    """用户贴过来的抖音 Cookie 分号后面没有空格，必须能认。"""
    cookies, fmt = ci.parse(REAL_DOUYIN_HEADER, fallback_domain=".douyin.com")
    assert fmt == "header"
    names = {c["name"] for c in cookies}
    assert {"sessionid", "sid_tt", "uid_tt", "x_tt_token"} <= names
    assert all(c["domain"] == ".douyin.com" for c in cookies)
    assert all(c["path"] == "/" for c in cookies)


def test_parses_header_with_spaces():
    cookies, _ = ci.parse("a=1; b=2; c=3", fallback_domain=".douyin.com")
    assert [c["name"] for c in cookies] == ["a", "b", "c"]


def test_keeps_equals_inside_value():
    """base64 值里的 = 不能被当成分隔符切掉。"""
    cookies, _ = ci.parse(
        "bd_ticket_guard_client_data=eyJhIjoxfQ%3D%3D; token=abc=def==",
        fallback_domain=".douyin.com",
    )
    by_name = {c["name"]: c["value"] for c in cookies}
    assert by_name["bd_ticket_guard_client_data"] == "eyJhIjoxfQ%3D%3D"
    assert by_name["token"] == "abc=def=="


def test_strips_cookie_prefix_from_devtools_copy():
    cookies, _ = ci.parse("Cookie: a=1; b=2", fallback_domain=".x.com")
    assert [c["name"] for c in cookies] == ["a", "b"]


def test_drops_volatile_keys():
    """__ac_nonce / __ac_signature 每次访问都变，贴进来只会添乱。"""
    cookies, _ = ci.parse(
        "sessionid=abc; __ac_nonce=06a8; __ac_signature=_02B4",
        fallback_domain=".douyin.com",
    )
    assert [c["name"] for c in cookies] == ["sessionid"]


def test_header_without_domain_is_rejected():
    with pytest.raises(ci.CookieParseError, match="域名"):
        ci.parse("a=1; b=2", fallback_domain="")


def test_blank_input_is_rejected():
    with pytest.raises(ci.CookieParseError):
        ci.parse("   ", fallback_domain=".douyin.com")


def test_garbage_input_is_rejected():
    with pytest.raises(ci.CookieParseError):
        ci.parse("这不是 cookie", fallback_domain=".douyin.com")


# ---------------------------------------------------------------------------
# Cookie-Editor 的 JSON 导出
# ---------------------------------------------------------------------------

def test_parses_cookie_editor_json():
    cookies, fmt = ci.parse(COOKIE_EDITOR_JSON, fallback_domain=".douyin.com")
    assert fmt == "json"
    by_name = {c["name"]: c for c in cookies}

    session = by_name["sessionid"]
    assert session["value"] == "679ceb84f2c9cdfb179222274a40041a"
    assert session["domain"] == ".douyin.com"
    assert session["httpOnly"] is True
    assert session["secure"] is True
    # no_restriction -> None（Playwright 的写法）
    assert session["sameSite"] == "None"
    assert session["expires"] == pytest.approx(1792748274.5)


def test_session_cookie_has_no_expiry():
    """session=true 的 Cookie 不写 expires，跟着浏览器上下文走。"""
    cookies, _ = ci.parse(COOKIE_EDITOR_JSON, fallback_domain=".douyin.com")
    ttwid = next(c for c in cookies if c["name"] == "ttwid")
    assert "expires" not in ttwid


def test_host_only_cookie_drops_leading_dot():
    """hostOnly 的 Cookie 域名不能带前导点，Playwright 会拒绝。"""
    raw = json.dumps([{
        "name": "a", "value": "1", "domain": ".example.com",
        "hostOnly": True, "path": "/",
    }])
    cookies, _ = ci.parse(raw, fallback_domain="")
    assert cookies[0]["domain"] == "example.com"


def test_same_site_mapping():
    raw = json.dumps([
        {"name": "a", "value": "1", "domain": ".x.com", "sameSite": "no_restriction"},
        {"name": "b", "value": "1", "domain": ".x.com", "sameSite": "lax"},
        {"name": "c", "value": "1", "domain": ".x.com", "sameSite": "strict"},
        {"name": "d", "value": "1", "domain": ".x.com", "sameSite": "unspecified"},
    ])
    cookies, _ = ci.parse(raw, fallback_domain="")
    assert [c["sameSite"] for c in cookies] == ["None", "Lax", "Strict", "Lax"]


def test_millisecond_expiry_is_converted():
    raw = json.dumps([{
        "name": "a", "value": "1", "domain": ".x.com", "expirationDate": 1792748274000,
    }])
    cookies, _ = ci.parse(raw, fallback_domain="")
    assert cookies[0]["expires"] == pytest.approx(1792748274)


def test_accepts_wrapped_and_flat_json_shapes():
    wrapped = json.dumps({"cookies": [{"name": "a", "value": "1", "domain": ".x.com"}]})
    assert ci.parse(wrapped, fallback_domain="")[0][0]["name"] == "a"

    flat = json.dumps({"sessionid": "abc", "sid_tt": "def"})
    cookies, _ = ci.parse(flat, fallback_domain=".douyin.com")
    assert {c["name"] for c in cookies} == {"sessionid", "sid_tt"}


def test_broken_json_reports_clearly():
    with pytest.raises(ci.CookieParseError, match="JSON"):
        ci.parse('[{"name": "a", ', fallback_domain=".x.com")


def test_json_without_domain_and_no_fallback_is_rejected():
    raw = json.dumps([{"name": "a", "value": "1"}])
    with pytest.raises(ci.CookieParseError, match="domain"):
        ci.parse(raw, fallback_domain="")


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.douyin.com", ".douyin.com"),
    ("https://www.kuaishou.com", ".kuaishou.com"),
    ("https://m.weibo.cn", ".weibo.cn"),
    ("https://www.xiaohongshu.com", ".xiaohongshu.com"),
])
def test_default_domain(url, expected):
    assert ci.default_domain(url) == expected


def test_round_trip_header_to_cookies_to_header():
    """串 -> 结构 -> 串，登录凭据必须原样活下来。"""
    cookies, _ = ci.parse(REAL_DOUYIN_HEADER, fallback_domain=".douyin.com")
    header = ci.to_header(cookies)
    assert missing_login_cookies("douyin", header) == []
    assert "sessionid=679ceb84f2c9cdfb179222274a40041a" in header


def test_summarize_reports_counts_and_domains():
    cookies, _ = ci.parse(COOKIE_EDITOR_JSON, fallback_domain="")
    summary = ci.summarize(cookies)
    assert summary["count"] == 3
    assert ".douyin.com" in summary["domains"]
    assert "sessionid" in summary["names"]


# ---------------------------------------------------------------------------
# ensure_persistent：会话 Cookie 进不了 profile，必须补过期时间
# ---------------------------------------------------------------------------

def test_ensure_persistent_fills_missing_expiry():
    """请求头串解析出来一个过期时间都没有，全是会话 Cookie。

    会话 Cookie 只活在内存里，Chromium 不写进 profile。
    不补的话导入会"成功"，但下次打开 profile 一个 Cookie 都没有——
    表现和没导入一样，极难查。
    """
    import time

    cookies, _ = ci.parse(REAL_DOUYIN_HEADER, fallback_domain=".douyin.com")
    assert all("expires" not in c for c in cookies)

    persistent = ci.ensure_persistent(cookies, ttl_days=30)
    now = time.time()
    assert all(c["expires"] > now for c in persistent)
    # 大约 30 天后，允许几秒误差
    assert persistent[0]["expires"] == pytest.approx(now + 30 * 86400, abs=10)


def test_ensure_persistent_keeps_existing_expiry():
    cookies, _ = ci.parse(COOKIE_EDITOR_JSON, fallback_domain=".douyin.com")
    persistent = {c["name"]: c for c in ci.ensure_persistent(cookies)}
    # 原本就有过期时间的不动
    assert persistent["sessionid"]["expires"] == pytest.approx(1792748274.5)
    # Cookie-Editor 标了 session=true 的那个也要补上，否则同样进不了 profile
    assert persistent["ttwid"]["expires"] > 0


def test_ensure_persistent_does_not_mutate_input():
    cookies, _ = ci.parse("a=1", fallback_domain=".x.com")
    ci.ensure_persistent(cookies)
    assert "expires" not in cookies[0]


# ---------------------------------------------------------------------------
# 快手换了 Cookie 名字（2026-08 实测）
# ---------------------------------------------------------------------------

def test_kuaishou_webday7_tokens_count_as_logged_in():
    """快手网页端现在发的是 webday7_st / webday7_ph，不是老的 web_st / web_ph。

    只认老名字的话，明明登录着的账号会被判成"没有登录凭据"，
    连手动导入都会被拒——用户对着满屏 Cookie 完全不知道差在哪。
    这是用户实际粘过来的那份（值已换成占位）。
    """
    from app.browser.specs import missing_login_cookies

    real_shape = (
        "kwssectoken=<略>;kwpsecproductname=kuaishou-vision;clientid=3;"
        "did=web_<略>;kpf=PC_WEB;kpn=KUAISHOU_VISION;"
        "kuaishou.server.webday7_ph=<略>;kuaishou.server.webday7_st=<略>;"
        "kwfv1=<略>;userId=3044101213"
    )
    assert missing_login_cookies("kuaishou", real_shape) == []


def test_kuaishou_visitor_cookies_still_rejected():
    """放宽名单不能放宽到把游客也放进来。

    did / kpf / kpn / userId 快手对未登录访客一样下发。
    """
    from app.browser.specs import missing_login_cookies

    visitor = "did=web_abc;kpf=PC_WEB;kpn=KUAISHOU_VISION;userId=123;clientid=3"
    assert missing_login_cookies("kuaishou", visitor), "游客 Cookie 不能算登录"


def test_cookie_names_lists_keys_without_values():
    """报错要能列出实际收到的 key —— 但**只列名字，绝不带值**。

    平台改 Cookie 名是常事（快手就刚改过）。只说"需要 web_st"的话，
    用户根本不知道差在哪，只能来回问；把 key 列出来一眼就看得出。
    """
    from app.browser.specs import cookie_names

    names = cookie_names("a=1; b=秘密值; c=; =nokey; d=2")
    assert names == ["a", "b", "d"], names
    assert all("秘密" not in n for n in names)
