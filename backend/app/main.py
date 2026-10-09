"""服务入口：FastAPI 应用装配与生命周期管理。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import collectors  # noqa: F401  导入即注册全部采集器
from .api import (
    accounts, archive as archive_api, data, labeling as labeling_api, live, media,
    qunar as qunar_api, scenic,
    settings as settings_api, tasks, ws,
)
from .api.common import fail
from .api.deps import AppState, set_state
from .browser.live_session import close_all as close_live_sessions
from .browser.live_view import close_all as close_live_views
from .browser.manager import BrowserSessionManager
from .collectors.base import CollectorRegistry
from .core.config import PROJECT_ROOT, load_config
from .core.db import init_db
from .core.logging import get_logger, setup_logging
from .core.redis_client import init_redis
from .proxy.manager import ProxyManager
from .repositories.account_repo import AccountRepository
from .repositories.data_repo import DataRepository
from .repositories.scenic_repo import ScenicRepository
from .repositories.setting_repo import SettingRepository
from .repositories.task_repo import TaskRepository
from .scheduler.runner import TaskRunner
from .scheduler.scheduler import TaskScheduler
from .utils.exporter import CsvExporter

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    setup_logging(
        config.get("server.log_level", "INFO"),
        Path(config.get("server.data_dir")) / "logs",
    )
    config.ensure_dirs()
    logger.info("配置文件：%s", config.source_path or "（未找到，使用内置默认值）")

    db = init_db(config)
    await db.connect()
    redis = init_redis(config)

    scenic_repo = ScenicRepository(db)
    # 一次性把任务里的「补充关键字」搬到景区的「附关键字」（幂等，搬完留记号）
    from .db import migrate_filter_words
    try:
        await migrate_filter_words.run(db, scenic_repo)
    except Exception as exc:      # noqa: BLE001
        # 搬迁失败不该拦住服务启动：老任务的配置还在 params 里，
        # runner 两边取并集，最多是景区页上看不到那些词
        logger.warning("关键字搬迁没跑成（不影响启动）：%s", exc)
    # 账号轮换锁：一个号采过之后，锁定期内先让别的号上（core/account_rotation.py）。
    # 挂在 repo 上，是因为"挑哪个号"这件事全都收口在 pick_active / active_names
    # 这两个方法里——挂在这儿，四个挑号入口一次全覆盖。
    from .core.account_rotation import AccountRotation
    account_repo = AccountRepository(db, rotation=AccountRotation(config, redis))
    task_repo = TaskRepository(db)
    data_repo = DataRepository(db)
    setting_repo = SettingRepository(db)

    # 页面上改的设置优先级最高，启动时先合并进来
    await setting_repo.apply_overrides(config)

    proxy_manager = ProxyManager(config, redis)
    browser_manager = BrowserSessionManager(config, account_repo)
    runner = TaskRunner(
        config,
        task_repo=task_repo, scenic_repo=scenic_repo, data_repo=data_repo,
        proxy_manager=proxy_manager, browser_manager=browser_manager,
    )
    scheduler = TaskScheduler(config, task_repo, runner, browser_manager=browser_manager)
    exporter = CsvExporter(config, data_repo)

    set_state(AppState(
        config=config, db=db, redis=redis,
        proxy_manager=proxy_manager, browser_manager=browser_manager,
        scenics=scenic_repo, accounts=account_repo, tasks=task_repo,
        data=data_repo, settings=setting_repo,
        runner=runner, scheduler=scheduler, exporter=exporter,
    ))

    # Playwright 需要能创建子进程的事件循环。uvicorn 在 Windows 上带 --reload
    # 或 --workers>1 时会把策略切成 SelectorEventLoop（不支持子进程），
    # 所以浏览器一律跑在自己的专用循环上，这里提前把它拉起来。
    from .browser.capacity import BROWSER_CAPACITY
    from .core.subprocess_loop import SUBPROCESS_LOOP, current_loop_supports_subprocess

    SUBPROCESS_LOOP.start()
    # 浏览器并发闸门：一个 Chromium 实测约 700MB / 10 个进程，
    # 没有上限的话 8 条浏览器任务能把一台 8G 机器直接压进换页。
    BROWSER_CAPACITY.configure(config)
    if not current_loop_supports_subprocess():
        logger.info(
            "当前服务器事件循环不支持创建子进程（常见于 Windows + --reload），"
            "浏览器已改由专用循环承载，功能不受影响"
        )

    await scheduler.start()
    logger.info("已注册采集器：%s", "、".join(CollectorRegistry.available()))
    logger.info(
        "服务就绪，监听 http://%s:%s（接口文档 /docs）",
        config.get("server.host"), config.get("server.port"),
    )

    # 标注管理器建成单例（此后 runner 里 get_manager() 拿到的就是它）。
    # 只是建对象，不连库不起线程——真正启动推迟到第一次要用的时候，
    # 而且启动前会先跑自检，不过就不标。
    from .labeling import get_manager as _get_labeling_manager

    _get_labeling_manager(config)
    # 通知器也建成单例：冷却状态要跨调用保持，每次新建就等于没有冷却
    from .notify import get_notifier as _get_notifier

    _notifier = _get_notifier(config)
    if _notifier.enabled:
        logger.info("通知：飞书机器人已开启（需要人工介入或采集报错时会推送）")
    if (config.get("labeling") or {}).get("enabled"):
        logger.info("边采边标：已开启（首次使用时会先自检，任一项不过就不标注）")

    try:
        yield
    finally:
        logger.info("正在关闭服务…")
        await close_live_sessions()
        await close_live_views()
        await scheduler.shutdown()
        from .api.media import close_media_client
        from .utils.jsvm import shutdown_js_runtime

        # 图片代理复用了一个全局 httpx 客户端，退出时要关掉
        await close_media_client()

        await shutdown_js_runtime()

        # 标注引擎有自己的 worker 线程池和 MySQL 连接池，
        # 不显式停机的话进程退不干净（线程是非守护的），
        # Ctrl+C 之后要等到超时才真的结束
        try:
            from .labeling import get_manager
            await get_manager(config).stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("标注引擎停机时出错（忽略）：%s", exc)

        SUBPROCESS_LOOP.stop()
        await db.close()
        logger.info("服务已关闭")


def create_app() -> FastAPI:
    config = load_config()
    app = FastAPI(
        title="景区社媒采集系统",
        description="按景区维度采集抖音/快手/小红书/微博/携程/同程的作品与评论",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.get("server.cors_origins", ["*"]),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for router in (
        scenic.router, tasks.router, data.router, media.router,
        accounts.router, settings_api.router, ws.router,
        live.router, live.ws_router, labeling_api.router, qunar_api.router,
        archive_api.router,
    ):
        app.include_router(router)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=fail(str(exc.detail), code=exc.status_code),
        )

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content=fail(str(exc), code=400))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("未处理异常：%s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=fail(f"服务内部错误：{exc}", code=500),
        )

    @app.get("/api/health", tags=["健康检查"])
    async def health():
        from .api.deps import get_state
        from .browser.capacity import BROWSER_CAPACITY
        state = get_state()
        return {
            "code": 0,
            "data": {
                "status": "ok",
                "mysql": await state.db.ping(),
                # ⚠️ redis.ping() 在**退化成进程内缓存时也返回 True**
                #    （MemoryStore.ping 恒为 True），所以它回答不了
                #    "Redis 到底连上没有"。顶栏的健康齿轮要的是后者：
                #    配了 Redis 却连不上，必须变红，而不是显示"已连接"。
                "redis": state.redis.ping(),
                "redis_enabled": bool(state.redis.enabled),
                "redis_is_real": state.redis.is_real_redis,
                "collectors": CollectorRegistry.available(),
                "scheduler": state.scheduler.status(),
                "proxy_enabled": bool(state.config.get("proxy.enabled")),
                # 浏览器闸门：现在开着几个、上限多少、内存还剩多少。
                # 卡机那次事后完全查不到"当时开了几个浏览器"，只能看监控猜。
                "browser_capacity": BROWSER_CAPACITY.stats(),
                # 用户反馈过"设置了无头还是弹窗口"，这里直接把生效值摆出来
                "browser_headless": {
                    "collect": state.browser_manager.headless_for_collect(),
                    "login": state.browser_manager.headless_for_login(),
                },
            },
            "message": "",
        }

    # 前端构建产物存在时一并托管，单端口即可访问整个系统
    frontend_dist = PROJECT_ROOT / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
        logger.info("已挂载前端静态资源：%s", frontend_dist)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    config = load_config()
    uvicorn.run(
        "app.main:app",
        host=config.get("server.host", "0.0.0.0"),
        port=int(config.get("server.port", 8000)),
        log_level=str(config.get("server.log_level", "info")).lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
