"""
API客户端模块
"""
import json
import logging
import re
import time
from typing import Dict, Optional

from openai import OpenAI

from .config import APIConfig

logger = logging.getLogger(__name__)


class APIClient:
    """API客户端：调用火山引擎API进行标注"""
    
    def __init__(self, config: APIConfig):
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
                        {"role": "system", "content": "你是一个专业的旅游评论分析专家，擅长情感分析、维度标注、实体识别和关键词提取。"},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens
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
