"""CSV 导出。需求明确只要 CSV，所以这里不做 xlsx。

两个要点：
  1. 默认 utf-8-sig（带 BOM），否则 Excel 打开中文是乱码
  2. 流式写：按主键游标分批取数，几十万行也不会把内存吃满
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from ..core.config import Config
from ..core.logging import get_logger
from ..db import tables
from ..repositories.data_repo import DataRepository

logger = get_logger(__name__)

# 导出列与列名：键是数据库列，值是表头。顺序即导出顺序。
WORKS_HEADERS: Dict[str, str] = {
    "scenic_id": "景区ID",
    "scenic_name": "景区名字",
    "channel": "平台",
    "work_id": "作品ID",
    "work_url": "作品链接",
    "author_id": "作者ID",
    "author_name": "作者名称",
    "title": "发布的文字内容",
    "description": "描述",
    "label": "标签",
    "image_list": "图片列表",
    "video_list": "视频列表",
    "likes": "点赞数",
    "collection_cnt": "收藏数",
    "comment_cnt": "评论数",
    "shares": "分享数",
    "location": "发布地址",
    "publish_time": "发布时间",
    "crawl_time": "采集时间",
    "source_keyword": "来源关键字",
    "extra_content": "其他内容",
    "create_time": "创建时间",
    "update_time": "更新时间",
}

COMMENTS_HEADERS: Dict[str, str] = {
    "scenic_id": "景区ID",
    "scenic_name": "景区名字",
    "channel": "平台",
    "work_id": "作品ID",
    "comment_level": "评论等级",
    "comment_parent_id": "评论父ID",
    "comment_id": "评论ID",
    "commenter_id": "评论用户ID",
    "commenter_name": "评论用户名称",
    "image_list": "图片列表",
    "video_list": "视频列表",
    "location": "发布地址",
    "content": "评论内容",
    "likes": "点赞数",
    "extra_content": "其他内容",
    "sentiment_label": "整体情感标签",
    "sentiment_score": "整体情感得分",
    "dimension_tags": "维度标签数组",
    "entity_tags": "实体标签数组",
    "keyword_tags": "关键词数组",
    "publish_time": "发布时间",
    "crawl_time": "采集时间",
    "create_time": "创建时间",
    "update_time": "更新时间",
}

TABLE_HEADERS = {
    tables.WORKS: WORKS_HEADERS,
    tables.COMMENTS: COMMENTS_HEADERS,
}


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    text = str(value)
    # Excel 会把以 = + - @ 开头的单元格当公式执行，导出前加个前导单引号挡掉
    if text[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


class CsvExporter:
    def __init__(self, config: Config, data_repo: DataRepository):
        self.config = config
        self.data = data_repo
        self.encoding = config.get("export.encoding", "utf-8-sig")
        self.chunk_size = int(config.get("export.chunk_size", 5000))
        self.export_dir = Path(config.get("export.dir"))

    # ---------------- 流式输出（给 HTTP 下载用） ----------------
    async def stream(
        self, table: str, filters: Dict[str, Any], *, use_chinese_headers: bool = True
    ) -> AsyncIterator[bytes]:
        headers = TABLE_HEADERS.get(table)
        if headers is None:
            raise ValueError(f"不支持导出的表：{table}")

        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(
            list(headers.values()) if use_chinese_headers else list(headers.keys())
        )
        yield self._take(buffer)

        count = 0
        async for row in self.data.iter_export_rows(table, filters, self.chunk_size):
            writer.writerow([_format_cell(row.get(column)) for column in headers])
            count += 1
            if count % 500 == 0:
                yield self._take(buffer)
        tail = self._take(buffer)
        if tail:
            yield tail
        logger.info("导出 %s 完成，共 %d 行", table, count)

    def _take(self, buffer: io.StringIO) -> bytes:
        text = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return text.encode(self.encoding)

    # ---------------- 落盘（给定时导出/大批量用） ----------------
    async def export_to_file(
        self, table: str, filters: Dict[str, Any], filename: str = ""
    ) -> Path:
        self.export_dir.mkdir(parents=True, exist_ok=True)
        if not filename:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            scope = filters.get("scenic_id") or "all"
            channel = filters.get("channel") or "all"
            short = "works" if table.endswith("works") else "comments"
            filename = f"{short}_{scope}_{channel}_{stamp}.csv"
        path = self.export_dir / filename

        with path.open("wb") as handle:
            async for chunk in self.stream(table, filters):
                handle.write(chunk)
        logger.info("导出文件已生成：%s", path)
        return path


def build_filename(table: str, filters: Dict[str, Any]) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short = "作品" if table.endswith("works") else "评论"
    scope = filters.get("scenic_id") or "全部景区"
    return f"{short}_{scope}_{stamp}.csv"
