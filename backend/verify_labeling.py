"""标注引擎接入的端到端验证：塞一条评论 → 标注 → 检查五个字段和复核标记。

为什么要有这个脚本
------------------
自检（`/api/labeling/check`）只证明"连得上"，证明不了"写对了"。
实跑第一次就抓到两个自检看不见的问题：

1. `label_review_flag` 落库是 **0**（应为 4）。原因是引擎的
   `label_sync(write=True)` 内部调 `update_one(record, result)`，
   review_flag 走默认值 0——只有队列那条路（consumer）会算好再写。
   页面上点「AI 再标注」，五个字段都更新了，复核标记却退回"未标注"。
2. 自检 9 项全绿，`ensure_started()` 却抛「失败池连不上 Redis」——
   任务队列和失败池是**两个**组件，队列切 memory 不代表失败池也是。

用法
----
    SMC_LABELING__ARK__API_KEY=... SMC_LABELING__ARK__MODEL=... \
        python verify_labeling.py

⚠️ 环境变量嵌套超过一层必须用**双下划线**：
   `SMC_LABELING__ARK__API_KEY`。写成 `SMC_LABELING_ARK_API_KEY`
   会被解析成 labeling.ark_api_key，安静地不生效（现在会有 WARNING）。

模型调用是**打桩**的（不花钱、不依赖外网），但引擎代码一行没改：
只在运行时替换 `ArkClient.complete`，走的还是引擎自己的
prompt / parser / taxonomy 校验 / repository 写回。
桩的形状照抄 `llm/prompt.py` 的输出模板——编一个"看着像"的
形状会让整条链路假绿（dimensions 写成 "一级/二级" 字符串时，
解析器把整条路径塞进 dim1，一条都匹配不上，落库是 []）。
"""
import asyncio, sys, json, uuid
sys.path.insert(0, ".")
from app.core.config import load_config
from app.labeling import config_bridge
from app.core.db import Database

CHANNEL, SCENIC = "douyin", "E2E001"
CID = "e2e_" + uuid.uuid4().hex[:12]

async def main():
    cfg = load_config()
    db = Database(cfg); await db.connect()

    # 1. 先造一条未标注的评论
    await db.execute(
        "INSERT INTO `src_opinion_social_work_comment_di` "
        "(channel, scenic_id, work_id, comment_id, content, label_review_flag) "
        "VALUES (%s,%s,%s,%s,%s,0)",
        [CHANNEL, SCENIC, "e2e_work", CID, "盘山的红叶太美了，就是停车场太少，排了一小时队"])
    print(f"造了一条评论 comment_id={CID}")

    # 2. 打桩模型（不改引擎文件，只换运行时对象）
    config_bridge.ensure_on_path()
    from opinion_labeling_engine.llm import client as ark
    # 桩的形状**照抄引擎自己的 prompt 模板**（llm/prompt.py 第 30 行），
    # 不是我凭印象编的——之前编的那份 dimensions 写成 "一级/二级/三级"
    # 字符串，解析器把整条路径塞进 dim1，一条都匹配不上，
    # 结果 dimension_tags 落库是 []，而流程"看着是通的"。
    fake = {"relevant": True, "confidence": 0.92,
            "sentiment": "负向",
            "dimensions": [
                {"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光",
                 "sentiment": "正向"},
                {"dim1": "游玩体验", "dim2": "排队时长", "dim3": "",
                 "sentiment": "负向"},
            ],
            "entities": [{"type": "景区地名", "value": "盘山"}],
            "keywords": [{"word": "红叶"}, {"word": "停车难"}]}
    def stub(self, messages, *, max_output_tokens=None):
        return ark.LLMResponse(text=json.dumps(fake, ensure_ascii=False),
                               model="stub", latency_ms=1)
    ark.ArkClient.complete = stub
    print("已打桩 ArkClient.complete（引擎文件未修改）")

    # 3. 用 smc 的 manager 走完整流程
    from app.labeling.manager import LabelingManager
    mgr = LabelingManager(cfg)
    ok = await mgr.ensure_started()
    print("引擎启动：", ok, mgr._start_error or "")
    if not ok:
        await db.close(); return 1

    row = await db.fetch_one(
        "SELECT * FROM `src_opinion_social_work_comment_di` WHERE comment_id=%s", [CID])
    res = await mgr.relabel_one(row)
    print("relabel_one 返回：", type(res).__name__)

    after = await db.fetch_one(
        "SELECT sentiment_label, sentiment_score, dimension_tags, entity_tags, "
        "keyword_tags, label_review_flag FROM `src_opinion_social_work_comment_di` "
        "WHERE comment_id=%s", [CID])
    print("\n写回结果：")
    for k, v in after.items():
        print(f"  {k:20s} = {v!r}")

    bad = []
    if not after["sentiment_label"]: bad.append("sentiment_label 没写")
    if after["label_review_flag"] != 4: bad.append(f"label_review_flag={after['label_review_flag']}，应为 4（AI标注成功）")
    if after["dimension_tags"] in (None,"","[]"): bad.append("dimension_tags 没写")
    if after["entity_tags"] in (None,"","[]"): bad.append("entity_tags 没写")
    if after['sentiment_label'] != '负向': bad.append(f"sentiment_label={after['sentiment_label']!r}，应为 负向（引擎的规范写法）")

    await db.execute("DELETE FROM `src_opinion_social_work_comment_di` WHERE comment_id=%s", [CID])
    await db.close()
    print("\n" + ("❌ " + "；".join(bad) if bad else "✅ 全部写回正确，label_review_flag=4"))
    return 1 if bad else 0

sys.exit(asyncio.run(main()))
