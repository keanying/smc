"""各平台的站点与登录态判定规则。

判定登录只看两类信号：
  1. Cookie 里是否出现**会话类** key（最可靠，且不用执行页面 JS）
  2. localStorage 里的登录标记（部分平台 Cookie 名会变，多一层保险）
新增平台时只需在这里加一条，浏览器管理器本身不用改。

⚠️ session_cookies 里只能放「登录后才会出现」的 key。
   踩过的坑：抖音的 passport_csrf_token / passport_csrf_token_default
   对**匿名访客也会下发**，把它们当登录凭据，结果是 profile 里只有 4 个
   游客 Cookie 也被判成"已登录"，任务照常跑起来，直到抖音接口回
   status_code=2483「请先登录再继续搜索吧」才暴露——而且那时报错信息
   指向的是采集器，不是登录态，很难查。

   判断某个 key 能不能放进来的方法：开一个全新的无痕窗口，什么都不登，
   打开站点首页，看这个 key 在不在。在，就不能放。

⚠️ 但 key 判定有个**天生治不好的毛病**：它只知道字段在不在，不知道值还有没有效。
   浏览器 profile 会把上次登录的 Cookie 一直留着，过期了 key 也还在。
   所以平台只要提供了"我登录了吗"这类接口，就用 verify_url 直接问它——
   微博的 m.weibo.cn/api/config 就是这么个接口，取自用户自己那套跑通的脚本。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from ..core.constants import (
    CHANNEL_DOUYIN,
    CHANNEL_KUAISHOU,
    CHANNEL_WEIBO,
    CHANNEL_XHS,
)


@dataclass
class LoginProbe:
    """问平台一句"我登录了吗"的一次探测。

    两种判据（取自用户那套跑通的微博脚本的 check_cookie）：
      1. `truthy_path` —— 返回 JSON 里按路径取值，真值就是已登录
         （m.weibo.cn/api/config 的 data.login 就是这种）
      2. 没给路径时 —— HTTP 200 **且没被重定向到登录页**
         （weibo.com/ajax/profile/info 这种，登录失效会跳 passport）
    """

    url: str
    truthy_path: List[str] = field(default_factory=list)
    #: 最终 URL 里出现这些片段就算没登录（被踢回登录页了）
    reject_url_contains: List[str] = field(
        default_factory=lambda: ["login", "passport", "signin"]
    )


@dataclass
class LoginSpec:
    channel: str
    home_url: str
    login_url: str = ""
    #: 出现任意一个（且值非空）即视为已登录。只放登录后才有的 key，见模块开头说明
    session_cookies: List[str] = field(default_factory=list)
    #: 采集器发请求前会校验这些 key 至少命中一个，缺失就直接报"未登录"，
    #: 不去撞平台接口。通常和 session_cookies 一致，留空表示复用它。
    required_cookies: List[str] = field(default_factory=list)
    #: localStorage 键 -> 期望值（值为 None 表示只要存在即可）
    local_storage_flags: Dict[str, str] = field(default_factory=dict)
    #: 精确判定：cookie 名 -> 期望值
    cookie_equals: Dict[str, str] = field(default_factory=dict)
    #: 判定登录态和保存 Cookie 时，只看这些 URL 能收到的 Cookie。
    #: 留空则用 home_url。
    #:
    #: ⚠️ 为什么不能"把所有 Cookie 都收下"：判定登录态和保存 Cookie 都必须
    #: 只看**采集实际会用到的那个域**。抖音的登录态散在 www / creator / live
    #: 几个子域上，取并集才完整；而跨注册域的情况（曾经的微博：登录在
    #: .weibo.com、采集在 .weibo.cn）更要命——两个域**各有一个同名的 SUB**，
    #: 不限定域就会拿另一个域那份去判"已登录"，判过了但采集根本用不了，
    #: 表现就是"登录完又让我登录"。微博现在全走 weibo.com，不再有这个问题，
    #: 但这个机制本身还留着：它是通用的，也有测试盯着。
    cookie_urls: List[str] = field(default_factory=list)
    #: 登录成功后先跳到这里再取 Cookie。
    #: 跨域登录（微博的 SSO）要靠这一跳，目标站才会把 Cookie 种到采集域上。
    #: 留空则用 home_url。
    post_login_url: str = ""
    #: ⚠️ 最可靠的一种判定：直接问平台"我登录了吗"。
    #: Cookie key 判定只能看出"有没有这个字段"，看不出**它还有没有效**。
    #: 微博就栽在这上面：profile 里留着上次过期的 .weibo.cn Cookie，
    #: key 一个不少，于是登录窗口一开就判定"已登录"（日志里 7 秒就成功了，
    #: 根本来不及扫码），存下这份废 Cookie，采集时接口退化成推荐卡，
    #: 用户被反复要求重新登录。
    #: 取自用户自己那套跑通的脚本：m.weibo.cn/api/config 的 data.login。
    verify_url: str = ""
    #: 在 verify_url 的 JSON 返回里按这个路径取值，真值 = 已登录
    verify_truthy_path: List[str] = field(default_factory=list)
    #: 多个探测点，**任意一个说"登录着"就算登录**。
    #:
    #: ⚠️ 微博为什么需要"任意一个"：登录发生在 weibo.com（PC 站），
    #: 采集走 m.weibo.cn（移动站），两个不同的注册域，各有各的会话。
    #: 移动站的会话是靠 SSO 从 PC 站换过来的，**换过来需要时间、也可能暂时失败**。
    #: 只认移动站的话，PC 明明还登录着，系统却判定"登录态已失效"、
    #: 反复要求用户重新登录——用户的原话是"我其实已经登录的呢，老出现重新登录"。
    verify_probes: List[LoginProbe] = field(default_factory=list)
    #: 判定登录态之前先访问这些地址，让平台把会话"换"到采集域上。
    #: 微博：在 weibo.com 登录后访问 m.weibo.cn，微博会自动走一次 SSO，
    #: 把 .weibo.cn 的 SUB / MLOGIN 种下来。
    handoff_urls: List[str] = field(default_factory=list)
    #: 页面加载完成的等待条件
    wait_until: str = "domcontentloaded"
    #: 登录二维码所在选择器（交互式登录时截取给前端显示）
    qrcode_selector: str = ""

    @property
    def login_entry(self) -> str:
        return self.login_url or self.home_url

    @property
    def must_have(self) -> List[str]:
        return self.required_cookies or self.session_cookies

    @property
    def cookie_scope(self) -> List[str]:
        return self.cookie_urls or [self.home_url]

    @property
    def settle_url(self) -> str:
        return self.post_login_url or self.home_url

    @property
    def probes(self) -> List[LoginProbe]:
        """所有探测点。verify_url 是单探测点的简写，合并进来。"""
        result = list(self.verify_probes)
        if self.verify_url:
            result.insert(0, LoginProbe(
                url=self.verify_url, truthy_path=list(self.verify_truthy_path),
            ))
        return result


LOGIN_SPECS: Dict[str, LoginSpec] = {
    CHANNEL_DOUYIN: LoginSpec(
        channel=CHANNEL_DOUYIN,
        home_url="https://www.douyin.com",
        # 这些都只在登录后下发。passport_csrf_token / ttwid / s_v_web_id /
        # __ac_nonce 游客也有，绝不能放进来。
        session_cookies=[
            "sessionid", "sessionid_ss", "sid_tt", "sid_guard",
            "uid_tt", "uid_tt_ss", "x_tt_token", "session_tlb_tag",
        ],
        cookie_equals={"LOGIN_STATUS": "1"},
        local_storage_flags={"HasUserLogin": "1"},
        # 抖音的登录态散在几个子域上，取并集（对齐 MediaCrawler 的 cookie_urls）
        cookie_urls=[
            "https://www.douyin.com",
            "https://douyin.com",
            "https://creator.douyin.com",
            "https://live.douyin.com",
        ],
        qrcode_selector="//div[@id='animate_qrcode_container']//img",
    ),
    CHANNEL_KUAISHOU: LoginSpec(
        channel=CHANNEL_KUAISHOU,
        home_url="https://www.kuaishou.com",
        # userId 快手对游客也会给，已从判定里去掉。
        # ⚠️ 2026-08 实测：快手网页端现在发的是 **webday7_st / webday7_ph**
        #    （7 天有效期的服务票据），老的 web_st / web_ph 不一定还有。
        #    只认老名字的话，明明登录着的账号会被判成"没有登录凭据"，
        #    连手动导入 Cookie 都会被拒绝——用户看着满屏 Cookie 一头雾水。
        #    两套名字都收，将来再变也只是往这个列表里加一个。
        session_cookies=[
            "kuaishou.server.webday7_st", "kuaishou.server.webday7_ph",
            "kuaishou.server.web_st", "kuaishou.server.web_ph",
            "kuaishou.web.cp.api_st",   # 创作者后台那套，登录后才有
            "passToken",
        ],
        cookie_urls=["https://www.kuaishou.com", "https://kuaishou.com"],
        qrcode_selector="//div[contains(@class,'qrcode')]//img",
    ),
    CHANNEL_XHS: LoginSpec(
        channel=CHANNEL_XHS,
        home_url="https://www.xiaohongshu.com",
        # customerClientId 游客也有；web_session 才是登录凭据
        session_cookies=[
            "web_session", "galaxy_creator_session_id",
            "customer-sso-sid", "access-token-creator.xiaohongshu.com",
        ],
        cookie_urls=["https://www.xiaohongshu.com", "https://xiaohongshu.com"],
        qrcode_selector="//div[contains(@class,'qrcode-img')]//img",
    ),
    # ------------------------------------------------------------------
    # 微博：**只走 PC 网页端**，不碰 m.weibo.cn
    #
    # 用户两次明确要求，而且这也是他那套跑通的采集链路的做法
    # （opinion-hub-all 的配置里 base_url 就是 https://weibo.com、
    #   search_url 就是 s.weibo.com）。
    #
    # 曾经把 home_url 设成 m.weibo.cn，代价是：登录在 .weibo.com、
    # 采集在 .weibo.cn，**两个不同的注册域、两套独立会话**，
    # 移动站那套靠 SSO 换过来、有自己的有效期，换不过来就被判成
    # "登录态已失效"——用户的原话是"我其实已经登录的呢，老出现重新登录"。
    # 全走 PC 端就没有这个问题：登录、Cookie、采集在同一个域上。
    # ------------------------------------------------------------------
    CHANNEL_WEIBO: LoginSpec(
        channel=CHANNEL_WEIBO,
        home_url="https://weibo.com",
        login_url="https://weibo.com/login.php",
        # ⚠️ 微博的 SUB 是登录凭证，游客没有（游客只有 SUBP）。
        # 用户参考项目 weibo_opinion 就是靠 SUB 判定，长期稳定。
        # SSOLoginState / ALF 是 SSO 会话的辅助字段，有时登录后也不一定有，
        # 把它们当必需字段会误判"登录态已失效"。
        session_cookies=["SUB"],
        required_cookies=["SUB"],
        # ⚠️ 两个域的 Cookie 都要收。它们是**两套独立的会话**：
        #    weibo.com（.weibo.com）  —— 登录态的源头
        #    m.weibo.cn（.weibo.cn）  —— 采集接口认的那一份
        #    存的时候各自带着 domain，取的时候按目标站点过滤（header_for_host），
        #    所以两个同名的 SUB 不会串。
        # 登录、Cookie、采集**全在 weibo.com 这一个域**上。
        # 登录过程会经过 login.sina.com.cn / passport.weibo.com，
        # 所以 sina.com.cn 的那几个也一并收下（对齐参考项目的域名白名单）。
        cookie_urls=["https://weibo.com", "https://login.sina.com.cn"],
        post_login_url="https://weibo.com",
        # ⚠️ 不用 verify_probes 探测 /ajax/profile/info：
        # context.request.get 不跟随重定向，未登录时微博 302 到 passport，
        # 但已登录的 profile 也可能因为 SSO 会话未同步而拿到 302，
        # 造成"明明登录着却被判失效"。
        # 微博的 SUB 是登录凭证，游客没有，靠 session_cookies 判定足够可靠。
        # 用户参考项目 weibo_opinion 就是这么做的，长期稳定。
        verify_probes=[],
        qrcode_selector="//img[contains(@class,'qrcode')]",
    ),
}


def get_spec(channel: str) -> LoginSpec:
    if channel not in LOGIN_SPECS:
        raise KeyError(
            f"平台 {channel} 不需要登录，或尚未定义登录规则；"
            f"已定义：{sorted(LOGIN_SPECS)}"
        )
    return LOGIN_SPECS[channel]


def needs_login(channel: str) -> bool:
    return channel in LOGIN_SPECS


def cookie_names(cookie_header: str) -> List[str]:
    """从 Cookie 串里挑出所有**有值**的 key 名。只回名字，绝不回值。"""
    names: List[str] = []
    for part in (cookie_header or "").split(";"):
        key, sep, value = part.partition("=")
        key = key.strip()
        if key and sep and value.strip() and key not in names:
            names.append(key)
    return names


def missing_login_cookies(channel: str, cookie_header: str) -> List[str]:
    """检查 Cookie 串里有没有该平台的登录凭据。

    返回缺失说明（空列表 = 没问题）。采集器在发第一个请求前调它，
    宁可在本地报"未登录"，也不要拿游客 Cookie 去撞平台接口——
    那样拿回来的是 status_code=2483 这种平台自己的错误码，
    用户看不出到底是账号问题还是采集器坏了。
    """
    if channel not in LOGIN_SPECS:
        return []
    spec = LOGIN_SPECS[channel]
    names = {
        key for key, _, value in
        ((p.partition("=")[0].strip(), None, p.partition("=")[2])
         for p in (cookie_header or "").split(";"))
        if key and value
    }
    if names & set(spec.must_have):
        return []
    return sorted(spec.must_have)
