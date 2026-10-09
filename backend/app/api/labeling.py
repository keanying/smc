"""AI 标注：状态自检、人工审核、一键补标。

设计取舍
--------
* 引擎在 vendor 里，**一行不改**。这里只调它 README 里写明的公开契约。
* 会阻塞的调用全部在 manager 里走 to_thread，路由这一层是纯 async。
* 「边采边标」的开关走 /api/settings（labeling 段），不在这儿另做一套——
  一个开关两个地方能改，迟早对不上。这里只提供**自检**和**状态**。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query

from ..core.constants import ALL_CHANNELS, CHANNEL_LABELS
from ..labeling import get_manager
from ..repositories.labeling_repo import (
    FLAG_HUMAN_FIXED, FLAG_HUMAN_RIGHT, FLAG_HUMAN_WRONG, FLAG_LABELS,
    FLAG_UNLABELED, HUMAN_FLAGS, LabelingRepository,
)
from .common import bad_request, not_found, ok
from .deps import AppState, get_state

router = APIRouter(prefix="/api/labeling", tags=["AI 标注"])


def _repo(state: AppState) -> LabelingRepository:
    return LabelingRepository(state.db)


# --------------------------------------------------------------------- 状态
@router.get("/status")
async def status(state: AppState = Depends(get_state)):
    """开关状态 + 引擎运行情况。页面顶部那条横幅用它。"""
    manager = get_manager(state.config)
    data = await manager.status()
    data["channels"] = [{"value": c, "label": CHANNEL_LABELS[c]} for c in ALL_CHANNELS]
    # 把七档标记的中文名一起给前端，省得两边各维护一份、改了对不上
    data["review_flags"] = [{"value": k, "label": v} for k, v in FLAG_LABELS.items()]
    data["human_flags"] = list(HUMAN_FLAGS)
    return ok(data)


@router.post("/check")
async def check(state: AppState = Depends(get_state)):
    """逐项自检：引擎 / 配置 / 模型 / 队列 / 数据库 / 表结构。

    ⚠️ 这也是开启「边采边标」的前置条件——用户的要求是
    "开启了要检测模型配置正确，数据库联调这些，存在异常不标注"。
    页面上开开关之前应该先按这个按钮，但**即使不按，
    引擎启动时也会自己跑一遍**，不过就不标（见 manager.ensure_started）。
    """
    manager = get_manager(state.config)
    items = await manager.check()
    passed = all(i.ok for i in items)
    return ok({
        "passed": passed,
        "items": [{"name": i.name, "ok": i.ok, "detail": i.detail} for i in items],
        "summary": ("全部通过，可以开启边采边标" if passed
                    else "有未通过项，开启后不会标注——先按提示修好"),
    })


# --------------------------------------------------------------------- 审核
@router.get("/stats")
async def stats(scenic_id: str = Query("", description="景区ID"),
                channel: str = Query(""),
                state: AppState = Depends(get_state)):
    return ok(await _repo(state).stats(scenic_id, channel))


@router.get("/comments")
async def list_comments(
    scenic_id: str = Query("", description="景区ID"),
    channel: str = Query(""),
    label_state: str = Query("", description="unlabeled / labeled / 空=全部"),
    sentiment: str = Query("", description="正向 / 中性 / 负向"),
    review_flag: str = Query(
        "", description="0未标注 1人工复核正确 2人工复核错误 3复核成功 "
                        "4AI标注成功 5AI标注错误 6未人工复核"),
    keyword: str = Query("", description="按评论正文模糊搜"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    state: AppState = Depends(get_state),
):
    flag = int(review_flag) if review_flag not in ("", None) else None
    return ok(await _repo(state).list_comments(
        scenic_id=scenic_id, channel=channel, label_state=label_state,
        sentiment=sentiment, review_flag=flag, keyword=keyword,
        page=page, page_size=page_size))


@router.get("/comments/{row_id}")
async def get_comment(row_id: int, state: AppState = Depends(get_state)):
    item = await _repo(state).get_one(row_id)
    if item is None:
        raise not_found("评论不存在")
    return ok(item)


@router.put("/comments/{row_id}")
async def save_review(row_id: int, payload: dict = Body(...),
                      state: AppState = Depends(get_state)):
    """人工复核：确认 / 判错 / 改完确认。

    三种意图，对应三个标记（这是 label_review_flag 里人工能写的全部）：

      * 不传字段、不传 flag        → 1 人工复核正确（AI 标对了）
      * 传 flag=2                  → 2 人工复核错误（标错了，但先记下来不改）
      * 传任一标注字段             → 3 复核成功（人改过并确认）

    4/5/6 是标注引擎写的状态，接口**拒绝**人工写进去——
    覆盖了就分不清一条数据是 AI 标的还是人改的。
    """
    repo = _repo(state)
    if await repo.get_one(row_id) is None:
        raise not_found("评论不存在")

    fields = {k: payload.get(k) for k in
              ("sentiment_label", "sentiment_score",
               "dimension_tags", "entity_tags", "keyword_tags")}
    edited = any(v is not None for v in fields.values())
    if edited and fields.get("sentiment_label") and fields.get("sentiment_score") is None:
        # 分数和标签必须一致，否则下游按分数聚合会和标签对不上。
        # 用户在页面上只改了标签时，这里补出分数。
        fields["sentiment_score"] = {"正向": 1, "中性": 0, "负向": -1}.get(
            str(fields["sentiment_label"]).strip())
        if fields["sentiment_score"] is None:
            raise bad_request(
                f"情感标签只能是 正向/中性/负向，收到 {fields['sentiment_label']!r}")

    if edited:
        flag = FLAG_HUMAN_FIXED           # 3 复核成功（改过并确认）
    elif payload.get("flag") is not None:
        flag = int(payload["flag"])
        if flag not in HUMAN_FLAGS:
            raise bad_request(
                f"人工只能写 1（复核正确）/ 2（复核错误）/ 3（复核成功），"
                f"收到 {flag}。4/5/6 是标注引擎的状态")
    else:
        flag = FLAG_HUMAN_RIGHT           # 1 复核正确

    try:
        okk = await repo.save_review(
            row_id, flag=flag,
            reviewer=str(payload.get("reviewer") or "人工"), **fields)
    except ValueError as exc:
        raise bad_request(str(exc))
    if not okk:
        raise bad_request("保存失败，可能这条记录刚被删掉了")
    return ok(await repo.get_one(row_id), f"已标记为「{FLAG_LABELS[flag]}」")


@router.post("/comments/{row_id}/reset")
async def reset_review(row_id: int, state: AppState = Depends(get_state)):
    """撤销复核：标记回到 0（未标注），下一轮补标会重新捞到它。"""
    if not await _repo(state).reset_review(row_id):
        raise not_found("评论不存在")
    return ok(await _repo(state).get_one(row_id), "已撤销复核")


@router.post("/comments/{row_id}/relabel")
async def relabel(row_id: int, state: AppState = Depends(get_state)):
    """AI 再标注这一条，立刻返回结果。

    走 label_sync 而不是入队：用户点了要马上看到结果。
    这一条会**同步等模型**（几秒），所以前端要给个 loading。
    """
    repo = _repo(state)
    item = await repo.get_one(row_id)
    if item is None:
        raise not_found("评论不存在")

    manager = get_manager(state.config)
    try:
        result = await manager.relabel_one({
            "channel": item["channel"], "work_id": item["work_id"],
            "scenic_id": item["scenic_id"], "scenic_name": item["scenic_name"],
            "comment_id": item["comment_id"], "content": item["content"] or "",
            "likes": item.get("likes"), "publish_time": item.get("publish_time"),
            "commenter_name": item.get("commenter_name"),
        })
    except Exception as exc:  # noqa: BLE001
        raise bad_request(f"AI 标注失败：{exc}")

    # label_sync(write=True) 已经写回了那五列**和** label_review_flag
    # （新版引擎自己写 4/5/6）。人工复核状态不用我们动——
    # 重标之后它就是"AI 标过、等人看"，正是 4/6 的语义。
    return ok({"result": result, "comment": await repo.get_one(row_id)},
              "已重新标注，请复核")


# --------------------------------------------------------------------- 补历史
@router.post("/backfill")
async def start_backfill(payload: dict = Body(default={}),
                         state: AppState = Depends(get_state)):
    """一键补标：把历史上没标过的评论捞出来跑一遍。

    后台跑，立刻返回 job_id；前端轮询 /backfill/{job_id} 看进度。
    几千条要跑十几分钟，同步等会把请求打超时。
    """
    manager = get_manager(state.config)
    try:
        job = await manager.start_backfill(
            scenic_id=str(payload.get("scenic_id") or ""),
            channel=str(payload.get("channel") or ""),
            limit=int(payload.get("limit") or 1000))
    except Exception as exc:  # noqa: BLE001
        raise bad_request(str(exc))
    return ok(job.to_dict(), f"补标任务已启动（{job.job_id}）")


@router.get("/backfill")
async def list_backfill(state: AppState = Depends(get_state)):
    """所有补标任务，最新的在前。

    页面刷新后靠它把「补标进行中」恢复出来——任务本来就在服务端跑着，
    以前只是前端刷新后没人去问，看着像任务没了。
    """
    return ok(get_manager(state.config).list_jobs())


@router.post("/backfill/{job_id}/cancel")
async def cancel_backfill(job_id: str, state: AppState = Depends(get_state)):
    """停止补标。已经喂进队列的那一批还会跑完，不会半路撕掉。"""
    if not get_manager(state.config).cancel_backfill(job_id):
        raise not_found("任务不存在或已经结束了")
    return ok(None, "已请求停止，正在跑的那一批结束后收工")


@router.get("/backfill/{job_id}")
async def get_backfill(job_id: str, state: AppState = Depends(get_state)):
    job = get_manager(state.config).get_job(job_id)
    if job is None:
        raise not_found("任务不存在（服务重启后进度会丢）")
    return ok(job)
