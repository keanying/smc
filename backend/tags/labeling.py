#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
八大处公园评论数据标注流水线
功能：对评论数据进行情感分析、维度标注、实体识别、关键词提取
作者：Manus AI
日期：2026-08-31
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from openai import OpenAI
from tqdm import tqdm

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('labeling.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class Config:
    """配置类"""
    # API配置
    api_key: str = os.environ.get("OPENAI_API_KEY", "ark-30981b8f")
    api_base: str = os.environ.get("OPENAI_API_BASE", "https://ark.cn-beijing.volces.com/api/v3")
    model: str = "deepseek-v4-flash-260425"

    # 处理配置
    batch_size: int = 10
    max_workers: int = 7
    max_retries: int = 3
    retry_delay: int = 2

    # 文件路径
    input_file: str = "src_opinion_social_work_comment_di.csv"
    output_file: str = "final_labeled_data.csv"
    progress_file: str = "progress.json"

    # 维度配置
    DIMENSIONS = {
        "游玩体验": ["景色观赏", "娱乐项目", "整体感受"],
        "服务质量": ["服务态度", "服务效率", "讲解服务"],
        "设施环境": ["基础设施", "环境卫生", "标识导览"],
        "餐饮购物": ["餐饮质量", "商品质量", "价格合理性"],
        "安全秩序": ["安全管理", "秩序维护", "应急处理"],
        "交通接驳": ["交通便利性", "停车设施", "接驳服务"]
    }

    # 实体类型配置
    ENTITY_TYPES = [
        "游客类型", "景点项目", "设施设备", "工作人员",
        "餐饮商品", "时间事件", "地理位置", "票务类型"
    ]


class ContentClassifier:
    """内容分类器：识别表情包、特殊符号、空内容等"""

    @staticmethod
    def is_empty(text: str) -> bool:
        """检查是否为空内容"""
        if pd.isna(text):
            return True
        text = str(text).strip()
        return len(text) == 0

    @staticmethod
    def is_bracket_emoji(text: str) -> bool:
        """
        检测方括号表情格式
        如：[心] [玫瑰] [赞] [感谢] 等
        """
        if pd.isna(text) or not text:
            return False

        text = str(text).strip()

        # 匹配单个 [xxx] 格式，xxx为1-3个中文字符
        pattern_single = r'^\[[\u4e00-\u9fff]{1,3}\]$'
        if re.match(pattern_single, text):
            return True

        # 匹配多个 [xxx] 组合
        pattern_multi = r'^(\[[\u4e00-\u9fff]{1,3}\])+$'
        if re.match(pattern_multi, text):
            return True

        return False

    @staticmethod
    def is_emoji_only(text: str) -> bool:
        """检测纯emoji表情"""
        if pd.isna(text) or not text:
            return False

        text = str(text).strip()

        # 常见emoji范围
        emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # 表情符号
            "\U0001F300-\U0001F5FF"  # 符号和象形文字
            "\U0001F680-\U0001F6FF"  # 交通和地图符号
            "\U0001F1E0-\U0001F1FF"  # 旗帜
            "\U00002702-\U000027B0"  # 装饰符号
            "\U0001F900-\U0001F9FF"  # 补充符号
            "\U0001FA00-\U0001FA6F"  # 国际象棋符号
            "\U0001FA70-\U0001FAFF"  # 扩展符号和象形文字
            "]+", flags=re.UNICODE
        )

        # 移除所有emoji和空白后如果为空，则是纯emoji
        text_no_emoji = emoji_pattern.sub('', text).strip()
        return len(text_no_emoji) == 0

    @staticmethod
    def is_tone_only(text: str) -> bool:
        """检测纯语气词（如"哈哈哈"、"呵呵"等）"""
        if pd.isna(text) or not text:
            return False

        text = str(text).strip()

        # 纯语气词模式
        tone_patterns = [
            r'^[哈嘿嘻哼呵嗯哦噢喔啊呀啦嘛呢吧]+$',  # 纯语气词
            r'^[哈]{2,}$',  # 哈哈哈
            r'^[嘿]{2,}$',  # 嘿嘿嘿
            r'^[嘻]{2,}$',  # 嘻嘻嘻
            r'^[呵]{2,}$',  # 呵呵呵
        ]

        for pattern in tone_patterns:
            if re.match(pattern, text):
                return True

        return False

    @staticmethod
    def is_symbol_only(text: str) -> bool:
        """检测纯特殊符号"""
        if pd.isna(text) or not text:
            return False

        text = str(text).strip()

        # 移除空白、非单词字符、数字后如果为空
        text_clean = re.sub(r'[\s\W\d_]+', '', text)
        if len(text_clean) == 0:
            return True

        # 纯字母且长度<=3
        if text.isalpha() and len(text) <= 3:
            return True

        # 常见无意义模式
        meaningless_patterns = [
            r'^[0-9]+$',  # 纯数字
            r'^[\.\!\?\~\-\+\*\=\(\)\[\]\{\}\<\>\,\;\:\'\"\`\|\/\\\@\#\$\%\^\&\_]+$',  # 纯标点
        ]

        for pattern in meaningless_patterns:
            if re.match(pattern, text):
                return True

        return False

    @classmethod
    def should_skip_api(cls, text: str) -> Tuple[bool, str]:
        """
        判断是否应该跳过API标注
        返回：(是否跳过, 原因)
        """
        if cls.is_empty(text):
            return True, "空内容"

        if cls.is_bracket_emoji(text):
            return True, "方括号表情"

        if cls.is_emoji_only(text):
            return True, "纯emoji表情"

        if cls.is_tone_only(text):
            return True, "纯语气词"

        if cls.is_symbol_only(text):
            return True, "纯特殊符号"

        return False, ""


class MourningDetector:
    """哀悼/慰问/社会事件检测器"""

    MOURNING_KEYWORDS = [
        '逝者安息', '生者坚强', '哀悼', '缅怀', '悼念', '默哀',
        '一路走好', '天堂', '安息', '祈祷', '愿.*安息', '愿.*平安',
        '泪目', '泪奔', '流泪', '哭泣', '难过', '心痛', '心碎',
        '残烈', '惨烈', '悲剧', '事故', '遇难', '牺牲', '殉职',
        '英雄', '烈士', '救援', '搜救', '失联', '失踪'
    ]

    SOCIAL_PATTERNS = [
        r'看过.*视频',
        r'看到.*新闻',
        r'转发微博',
        r'//@',
    ]

    @classmethod
    def is_mourning(cls, text: str) -> bool:
        """检测是否为哀悼/慰问/社会事件类内容"""
        if pd.isna(text) or not text:
            return False

        text = str(text)

        # 检查是否包含哀悼关键词
        for keyword in cls.MOURNING_KEYWORDS:
            if keyword in text:
                return True

        # 检查是否是转发/评论社会新闻
        for pattern in cls.SOCIAL_PATTERNS:
            if re.search(pattern, text):
                # 进一步检查是否包含哀悼词
                for keyword in cls.MOURNING_KEYWORDS:
                    if keyword in text:
                        return True

        return False


class APIClient:
    """API客户端：调用火山引擎API进行标注"""

    def __init__(self, config: Config):
        self.config = config
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.api_base
        )

    def build_prompt(self, content: str) -> str:
        """构建标注提示词"""
        prompt = f"""请对以下景区评论进行情感分析和多维度标注。

## 评论内容：
{content}

## 标注要求：

### 1. 整体情感判断
- 正面：1
- 中性：0
- 负面：-1

### 2. 维度标注（可多选）
请从以下维度体系中进行标注：

| 一级维度 | 二级维度 | 三级维度 |
|---------|---------|---------|
| 游玩体验 | 景色观赏 | 自然风光、人文景观、整体感受 |
| 游玩体验 | 娱乐项目 | 游乐设施、文化体验、休闲活动 |
| 服务质量 | 服务态度 | 工作人员态度、导游服务 |
| 服务质量 | 服务效率 | 购票效率、验票效率、响应速度 |
| 设施环境 | 基础设施 | 停车场、卫生间、休息区 |
| 设施环境 | 环境卫生 | 清洁度、垃圾处理 |
| 设施环境 | 标识导览 | 路牌、导览图、指示标识 |
| 餐饮购物 | 餐饮质量 | 口味、价格、卫生 |
| 餐饮购物 | 商品质量 | 纪念品、特产、价格 |
| 安全秩序 | 安全管理 | 护栏、警示、巡逻 |
| 安全秩序 | 秩序维护 | 排队、人流控制 |
| 交通接驳 | 交通便利性 | 公交、地铁、自驾 |
| 交通接驳 | 停车设施 | 停车场、停车费 |
| 交通接驳 | 接驳服务 | 电瓶车、摆渡车 |

### 3. 实体识别
请识别以下类型的实体：
- 游客类型：亲子家庭、情侣/夫妻、朋友结伴、独自旅行、老年游客、摄影爱好者、文化研学客、宗教信仰者
- 景点项目：具体景点名称、游乐设施名称
- 设施设备：停车场、卫生间、休息区、缆车、滑道等
- 工作人员：导游、讲解员、售票员、保安等
- 餐饮商品：餐饮类型、商品类型
- 时间事件：节假日、季节活动、时间段
- 地理位置：出入口、游览区域、周边地点
- 票务类型：门票、加购项目、其他票务

### 4. 关键词提取
提取3-5个核心关键词或短语。

## 输出格式（JSON）：
{{
    "sentiment_score": 1或0或-1,
    "sentiment_label": "正面"或"中性"或"负面",
    "dimension_tags": [
        {{"dim1": "一级维度", "dim2": "二级维度", "dim3": "三级维度", "sentiment": 1或0或-1}}
    ],
    "entity_tags": [
        {{"type": "实体类型", "value": "实体值"}}
    ],
    "keyword_tags": ["关键词1", "关键词2", "关键词3"]
}}

请直接输出JSON，不要其他内容。"""
        return prompt

    def call_api(self, content: str) -> Optional[Dict]:
        """调用API进行标注"""
        prompt = self.build_prompt(content)

        for attempt in range(self.config.max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system",
                         "content": "你是一个专业的旅游评论分析专家，擅长情感分析、维度标注、实体识别和关键词提取。"},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.3,
                    max_tokens=2000
                )

                result_text = response.choices[0].message.content.strip()

                # 尝试解析JSON
                try:
                    result = json.loads(result_text)
                    return result
                except json.JSONDecodeError:
                    # 尝试提取JSON部分
                    json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
                    if json_match:
                        result = json.loads(json_match.group())
                        return result
                    else:
                        logger.warning(f"无法解析API返回结果: {result_text[:100]}")
                        return None

            except Exception as e:
                logger.error(f"API调用失败 (尝试 {attempt + 1}/{self.config.max_retries}): {e}")
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay)
                else:
                    return None

        return None


class LabelingPipeline:
    """标注流水线：完整的标注处理流程"""

    def __init__(self, config: Config):
        self.config = config
        self.api_client = APIClient(config)
        self.content_classifier = ContentClassifier()
        self.mourning_detector = MourningDetector()

        # 加载进度
        self.progress = self._load_progress()

    def _load_progress(self) -> Dict:
        """加载处理进度"""
        if os.path.exists(self.config.progress_file):
            with open(self.config.progress_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"processed_ids": [], "last_index": 0}

    def _save_progress(self):
        """保存处理进度"""
        with open(self.config.progress_file, 'w', encoding='utf-8') as f:
            json.dump(self.progress, f, ensure_ascii=False, indent=2)

    def _default_label(self, row: pd.Series, reason: str) -> pd.Series:
        """默认标注（中评）"""
        row['sentiment_label'] = '中性'
        row['sentiment_score'] = 0.0
        row['dimension_tags'] = '[]'
        row['entity_tags'] = '[]'
        row['keyword_tags'] = '[]'
        row['label_reason'] = reason
        return row

    def _mourning_label(self, row: pd.Series) -> pd.Series:
        """哀悼/慰问类标注"""
        row['sentiment_label'] = '中性'
        row['sentiment_score'] = 0.0

        # 添加维度标签
        row['dimension_tags'] = json.dumps([{
            "dim1": "其他",
            "dim2": "社会事件",
            "dim3": "哀悼慰问",
            "sentiment": 0
        }], ensure_ascii=False)

        # 添加实体标签
        row['entity_tags'] = json.dumps([{
            "type": "事件类型",
            "value": "社会新闻"
        }], ensure_ascii=False)

        # 提取关键词
        content = str(row['content'])
        words = re.findall(r'[\u4e00-\u9fff]{2,4}', content)
        keywords = words[:5] if words else []
        row['keyword_tags'] = json.dumps(keywords, ensure_ascii=False)

        row['label_reason'] = "哀悼/慰问类"
        return row

    def _api_label(self, row: pd.Series) -> pd.Series:
        """API标注"""
        content = str(row['content'])

        # 调用API
        result = self.api_client.call_api(content)

        if result:
            # 解析API结果
            row['sentiment_score'] = float(result.get('sentiment_score', 0))
            row['sentiment_label'] = result.get('sentiment_label', '中性')

            # 处理维度标签
            dimension_tags = result.get('dimension_tags', [])
            row['dimension_tags'] = json.dumps(dimension_tags, ensure_ascii=False)

            # 处理实体标签
            entity_tags = result.get('entity_tags', [])
            row['entity_tags'] = json.dumps(entity_tags, ensure_ascii=False)

            # 处理关键词标签
            keyword_tags = result.get('keyword_tags', [])
            row['keyword_tags'] = json.dumps(keyword_tags, ensure_ascii=False)

            row['label_reason'] = "API标注"
        else:
            # API失败，使用默认标注
            row = self._default_label(row, "API失败")

        return row

    def process_row(self, row: pd.Series) -> pd.Series:
        """处理单行数据"""
        content = row['content']

        # 1. 检查是否跳过API标注
        should_skip, reason = self.content_classifier.should_skip_api(content)
        if should_skip:
            return self._default_label(row, reason)

        # 2. 检查是否为哀悼/慰问类
        if self.mourning_detector.is_mourning(content):
            return self._mourning_label(row)

        # 3. 调用API标注
        return self._api_label(row)

    def process_batch(self, batch_df: pd.DataFrame) -> pd.DataFrame:
        """处理批量数据"""
        results = []

        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            futures = {executor.submit(self.process_row, row): idx for idx, row in batch_df.iterrows()}

            for future in as_completed(futures):
                idx = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    logger.error(f"处理第{idx}行时出错: {e}")
                    # 使用默认标注
                    row = batch_df.loc[idx]
                    results.append(self._default_label(row, "处理异常"))

        return pd.DataFrame(results)

    def run(self):
        """运行标注流水线"""
        logger.info("=" * 60)
        logger.info("开始运行标注流水线")
        logger.info("=" * 60)

        # 读取数据
        logger.info(f"读取数据文件: {self.config.input_file}")
        df = pd.read_csv(self.config.input_file)
        logger.info(f"总数据量: {len(df)} 条")

        # 清洗content
        df['content'] = df['content'].apply(lambda x: str(x).strip() if pd.notna(x) else '')

        # 解析extra_content
        logger.info("解析extra_content字段...")
        extra_data = df['extra_content'].apply(self._parse_extra_content)
        df = pd.concat([df, extra_data], axis=1)

        # 分批处理
        total_batches = (len(df) + self.config.batch_size - 1) // self.config.batch_size
        logger.info(f"分批处理: 每批{self.config.batch_size}条，共{total_batches}批")

        processed_count = 0
        start_index = self.progress.get("last_index", 0)

        for i in range(start_index, len(df), self.config.batch_size):
            batch_df = df.iloc[i:i + self.config.batch_size].copy()

            # 处理批量数据
            batch_results = self.process_batch(batch_df)

            # 更新原数据
            for idx, row in batch_results.iterrows():
                df.loc[idx] = row

            processed_count += len(batch_df)

            # 保存进度
            self.progress["last_index"] = i + self.config.batch_size
            self.progress["processed_ids"] = list(range(i + self.config.batch_size))
            self._save_progress()

            # 记录日志
            if (i // self.config.batch_size + 1) % 10 == 0:
                logger.info(f"已处理 {processed_count}/{len(df)} 条 ({processed_count / len(df) * 100:.1f}%)")

        # 保存结果
        logger.info(f"保存结果到: {self.config.output_file}")
        df.to_csv(self.config.output_file, index=False, encoding='utf-8-sig')

        logger.info("=" * 60)
        logger.info("标注流水线运行完成")
        logger.info("=" * 60)

        # 生成统计报告
        self._generate_report(df)

    def _parse_extra_content(self, extra_str: str) -> pd.Series:
        """解析extra_content字段"""
        try:
            if pd.isna(extra_str) or extra_str == '':
                return pd.Series({
                    'score': None,
                    'landscape_score': None,
                    'fun_score': None,
                    'price_quality_score': None,
                    'tourist_type': None
                })

            data = json.loads(extra_str)
            return pd.Series({
                'score': data.get('score'),
                'landscape_score': data.get('landscape_score'),
                'fun_score': data.get('fun_score'),
                'price_quality_score': data.get('price_quality_score'),
                'tourist_type': data.get('tourist_type')
            })
        except:
            return pd.Series({
                'score': None,
                'landscape_score': None,
                'fun_score': None,
                'price_quality_score': None,
                'tourist_type': None
            })

    def _generate_report(self, df: pd.DataFrame):
        """生成统计报告"""
        logger.info("生成统计报告...")

        # 情感分布
        sentiment_dist = df['sentiment_label'].value_counts().to_dict()

        # 标注原因分布
        reason_dist = df['label_reason'].value_counts().to_dict()

        # 渠道分布
        channel_dist = df['channel'].value_counts().to_dict()

        report = {
            "数据概览": {
                "总数据量": len(df),
                "景区": "八大处公园",
                "处理时间": time.strftime("%Y-%m-%d %H:%M:%S")
            },
            "情感分布": sentiment_dist,
            "标注原因分布": reason_dist,
            "渠道分布": channel_dist
        }

        # 保存报告
        report_file = "labeling_report.json"
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        logger.info(f"统计报告已保存至: {report_file}")


def main():
    """主函数"""
    # 创建配置
    config = Config()

    # 检查API密钥
    if not config.api_key:
        logger.error("未设置API密钥，请设置环境变量 OPENAI_API_KEY")
        return

    # 创建标注流水线
    pipeline = LabelingPipeline(config)

    # 运行流水线
    pipeline.run()


if __name__ == '__main__':
    main()