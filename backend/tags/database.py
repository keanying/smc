"""
MySQL数据库模块
"""
import json
import logging
from typing import Dict, List, Optional
from contextlib import contextmanager

import pandas as pd
from sqlalchemy import create_engine, Column, String, Float, Text, DateTime, Integer
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from .config import MySQLConfig

logger = logging.getLogger(__name__)

Base = declarative_base()


class CommentLabel(Base):
    """评论标注表"""
    __tablename__ = 'comment_labels'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    comment_id = Column(String(64), unique=True, nullable=False, index=True)
    scenic_id = Column(String(32), nullable=False, index=True)
    scenic_name = Column(String(128), nullable=False)
    channel = Column(String(32), nullable=False, index=True)
    content = Column(Text, nullable=False)
    
    # 标注结果
    sentiment_label = Column(String(16), nullable=False)
    sentiment_score = Column(Float, nullable=False)
    dimension_tags = Column(Text, nullable=False)  # JSON格式
    entity_tags = Column(Text, nullable=False)     # JSON格式
    keyword_tags = Column(Text, nullable=False)    # JSON格式
    
    # 标注元数据
    label_reason = Column(String(64), nullable=False)  # 标注原因
    label_type = Column(String(32), nullable=False)    # 标注类型：default/mourning/api
    
    # 时间戳
    publish_time = Column(DateTime, nullable=True)
    crawl_time = Column(DateTime, nullable=True)
    create_time = Column(DateTime, nullable=False)
    update_time = Column(DateTime, nullable=False)
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'comment_id': self.comment_id,
            'scenic_id': self.scenic_id,
            'scenic_name': self.scenic_name,
            'channel': self.channel,
            'content': self.content,
            'sentiment_label': self.sentiment_label,
            'sentiment_score': self.sentiment_score,
            'dimension_tags': json.loads(self.dimension_tags) if self.dimension_tags else [],
            'entity_tags': json.loads(self.entity_tags) if self.entity_tags else [],
            'keyword_tags': json.loads(self.keyword_tags) if self.keyword_tags else [],
            'label_reason': self.label_reason,
            'label_type': self.label_type,
            'publish_time': self.publish_time.strftime('%Y-%m-%d %H:%M:%S') if self.publish_time else None,
            'crawl_time': self.crawl_time.strftime('%Y-%m-%d %H:%M:%S') if self.crawl_time else None,
            'create_time': self.create_time.strftime('%Y-%m-%d %H:%M:%S') if self.create_time else None,
            'update_time': self.update_time.strftime('%Y-%m-%d %H:%M:%S') if self.update_time else None,
        }


class MySQLManager:
    """MySQL管理器"""
    
    def __init__(self, config: MySQLConfig):
        self.config = config
        self.engine = None
        self.Session = None
        self._connect()
    
    def _connect(self):
        """建立数据库连接"""
        try:
            # 构建连接URL
            url = (
                f"mysql+pymysql://{self.config.user}:{self.config.password}"
                f"@{self.config.host}:{self.config.port}/{self.config.database}"
                f"?charset={self.config.charset}"
            )
            
            # 创建引擎
            self.engine = create_engine(
                url,
                poolclass=QueuePool,
                pool_size=self.config.pool_min,
                max_overflow=self.config.pool_max - self.config.pool_min,
                pool_pre_ping=True,  # 自动检测连接是否有效
                pool_recycle=3600,   # 1小时后回收连接
                echo=False
            )
            
            # 创建会话工厂
            self.Session = sessionmaker(bind=self.engine)
            
            logger.info(f"MySQL连接成功: {self.config.host}:{self.config.port}/{self.config.database}")
            
            # 自动建表
            if self.config.auto_migrate:
                self._create_tables()
                
        except Exception as e:
            logger.error(f"MySQL连接失败: {e}")
            raise
    
    def _create_tables(self):
        """创建数据表"""
        try:
            Base.metadata.create_all(self.engine)
            logger.info("数据表创建成功")
        except Exception as e:
            logger.error(f"数据表创建失败: {e}")
            raise
    
    @contextmanager
    def get_session(self):
        """获取数据库会话（上下文管理器）"""
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"数据库操作失败: {e}")
            raise
        finally:
            session.close()
    
    def insert_label(self, label_data: Dict) -> bool:
        """插入单条标注数据"""
        try:
            with self.get_session() as session:
                # 检查是否已存在
                existing = session.query(CommentLabel).filter_by(
                    comment_id=label_data['comment_id']
                ).first()
                
                if existing:
                    # 更新现有记录
                    for key, value in label_data.items():
                        if hasattr(existing, key):
                            setattr(existing, key, value)
                    logger.info(f"更新记录: {label_data['comment_id']}")
                else:
                    # 插入新记录
                    label = CommentLabel(**label_data)
                    session.add(label)
                    logger.info(f"插入记录: {label_data['comment_id']}")
                
                return True
        except Exception as e:
            logger.error(f"插入标注数据失败: {e}")
            return False
    
    def batch_insert_labels(self, labels_data: List[Dict]) -> int:
        """批量插入标注数据"""
        success_count = 0
        
        for label_data in labels_data:
            if self.insert_label(label_data):
                success_count += 1
        
        logger.info(f"批量插入完成: 成功{success_count}/{len(labels_data)}条")
        return success_count
    
    def get_label_by_comment_id(self, comment_id: str) -> Optional[Dict]:
        """根据comment_id查询标注数据"""
        try:
            with self.get_session() as session:
                label = session.query(CommentLabel).filter_by(
                    comment_id=comment_id
                ).first()
                
                if label:
                    return label.to_dict()
                return None
        except Exception as e:
            logger.error(f"查询标注数据失败: {e}")
            return None
    
    def get_labels_by_scenic_id(self, scenic_id: str, limit: int = 100) -> List[Dict]:
        """根据scenic_id查询标注数据"""
        try:
            with self.get_session() as session:
                labels = session.query(CommentLabel).filter_by(
                    scenic_id=scenic_id
                ).limit(limit).all()
                
                return [label.to_dict() for label in labels]
        except Exception as e:
            logger.error(f"查询标注数据失败: {e}")
            return []
    
    def get_statistics(self, scenic_id: str) -> Dict:
        """获取统计信息"""
        try:
            with self.get_session() as session:
                # 总数据量
                total = session.query(CommentLabel).filter_by(
                    scenic_id=scenic_id
                ).count()
                
                # 情感分布
                sentiment_dist = {}
                for label in session.query(CommentLabel).filter_by(scenic_id=scenic_id).all():
                    sentiment = label.sentiment_label
                    sentiment_dist[sentiment] = sentiment_dist.get(sentiment, 0) + 1
                
                # 渠道分布
                channel_dist = {}
                for label in session.query(CommentLabel).filter_by(scenic_id=scenic_id).all():
                    channel = label.channel
                    channel_dist[channel] = channel_dist.get(channel, 0) + 1
                
                return {
                    'total': total,
                    'sentiment_distribution': sentiment_dist,
                    'channel_distribution': channel_dist
                }
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {}
    
    def close(self):
        """关闭数据库连接"""
        if self.engine:
            self.engine.dispose()
            logger.info("MySQL连接已关闭")
