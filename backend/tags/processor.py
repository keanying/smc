"""
标注处理器模块
"""
import json
import logging
import re
from typing import Dict, List, Optional
from datetime import datetime

import pandas as pd

from .config import Config
from .filters import PhysicalFilter, MourningDetector
from .api_client import APIClient
from .database import MySQLManager

logger = logging.getLogger(__name__)


class LabelingProcessor:
    """标注处理器：完整的标注处理流程"""
    
    def __init__(self, config: Config):
        self.config = config
        self.api_client = APIClient(config.api)
        self.db_manager = MySQLManager(config.mysql)
        self.filter = PhysicalFilter()
        self.mourning_detector = MourningDetector()
    
    def _default_label(self, row: pd.Series, reason: str) -> Dict:
        """默认标注（中评）"""
        return {
            'comment_id': str(row['comment_id']),
            'scenic_id': str(row['scenic_id']),
            'scenic_name': str(row['scenic_name']),
            'channel': str(row['channel']),
            'content': str(row['content']),
            'sentiment_label': '中性',
            'sentiment_score': 0.0,
            'dimension_tags': '[]',
            'entity_tags': '[]',
            'keyword_tags': '[]',
            'label_reason': reason,
            'label_type': 'default',
            'publish_time': row.get('publish_time'),
            'crawl_time': row.get('crawl_time'),
            'create_time': datetime.now(),
            'update_time': datetime.now()
        }
    
    def _mourning_label(self, row: pd.Series) -> Dict:
        """哀悼/慰问类标注"""
        content = str(row['content'])
        
        # 提取关键词
        words = re.findall(r'[\u4e00-\u9fff]{2,4}', content)
        keywords = words[:5] if words else []
        
        return {
            'comment_id': str(row['comment_id']),
            'scenic_id': str(row['scenic_id']),
            'scenic_name': str(row['scenic_name']),
            'channel': str(row['channel']),
            'content': content,
            'sentiment_label': '中性',
            'sentiment_score': 0.0,
            'dimension_tags': json.dumps([{
                "dim1": "其他",
                "dim2": "社会事件",
                "dim3": "哀悼慰问",
                "sentiment": 0
            }], ensure_ascii=False),
            'entity_tags': json.dumps([{
                "type": "事件类型",
                "value": "社会新闻"
            }], ensure_ascii=False),
            'keyword_tags': json.dumps(keywords, ensure_ascii=False),
            'label_reason': '哀悼/慰问类',
            'label_type': 'mourning',
            'publish_time': row.get('publish_time'),
            'crawl_time': row.get('crawl_time'),
            'create_time': datetime.now(),
            'update_time': datetime.now()
        }
    
    def _api_label(self, row: pd.Series) -> Dict:
        """API标注"""
        content = str(row['content'])
        
        # 调用API
        result = self.api_client.call_api(content)
        
        if result:
            # 解析API结果
            return {
                'comment_id': str(row['comment_id']),
                'scenic_id': str(row['scenic_id']),
                'scenic_name': str(row['scenic_name']),
                'channel': str(row['channel']),
                'content': content,
                'sentiment_label': result.get('sentiment_label', '中性'),
                'sentiment_score': float(result.get('sentiment_score', 0)),
                'dimension_tags': json.dumps(result.get('dimension_tags', []), ensure_ascii=False),
                'entity_tags': json.dumps(result.get('entity_tags', []), ensure_ascii=False),
                'keyword_tags': json.dumps(result.get('keyword_tags', []), ensure_ascii=False),
                'label_reason': 'API标注',
                'label_type': 'api',
                'publish_time': row.get('publish_time'),
                'crawl_time': row.get('crawl_time'),
                'create_time': datetime.now(),
                'update_time': datetime.now()
            }
        else:
            # API失败，使用默认标注
            return self._default_label(row, 'API失败')
    
    def process_row(self, row: pd.Series) -> Dict:
        """处理单行数据"""
        content = row['content']
        
        # 1. 物理规则过滤
        should_filter, reason = self.filter.should_filter(content)
        if should_filter:
            return self._default_label(row, reason)
        
        # 2. 哀悼/慰问类检测
        if self.mourning_detector.is_mourning(content):
            return self._mourning_label(row)
        
        # 3. API标注
        return self._api_label(row)
    
    def process_dataframe(self, df: pd.DataFrame) -> List[Dict]:
        """处理DataFrame数据"""
        results = []
        
        for _, row in df.iterrows():
            try:
                result = self.process_row(row)
                results.append(result)
            except Exception as e:
                logger.error(f"处理数据失败: {e}")
                # 使用默认标注
                results.append(self._default_label(row, '处理异常'))
        
        return results
    
    def process_and_save(self, df: pd.DataFrame) -> int:
        """处理数据并保存到MySQL"""
        # 处理数据
        results = self.process_dataframe(df)
        
        # 保存到MySQL
        success_count = self.db_manager.batch_insert_labels(results)
        
        return success_count
    
    def get_statistics(self, scenic_id: str) -> Dict:
        """获取统计信息"""
        return self.db_manager.get_statistics(scenic_id)
    
    def close(self):
        """关闭资源"""
        self.db_manager.close()
