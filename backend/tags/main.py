"""
主入口文件
"""
import logging
import sys
from pathlib import Path

import pandas as pd

from .config import Config
from .processor import LabelingProcessor

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('labeling.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def load_config(config_path: str = None) -> Config:
    """加载配置"""
    if config_path and Path(config_path).exists():
        logger.info(f"从配置文件加载: {config_path}")
        return Config.from_yaml(config_path)
    else:
        logger.info("使用默认配置")
        return Config.default()


def process_csv_file(csv_path: str, config_path: str = None):
    """处理CSV文件"""
    # 加载配置
    config = load_config(config_path)
    
    # 创建处理器
    processor = LabelingProcessor(config)
    
    try:
        # 读取CSV文件
        logger.info(f"读取CSV文件: {csv_path}")
        df = pd.read_csv(csv_path)
        logger.info(f"总数据量: {len(df)} 条")
        
        # 处理并保存
        logger.info("开始处理数据...")
        success_count = processor.process_and_save(df)
        logger.info(f"处理完成: 成功{success_count}/{len(df)}条")
        
        # 获取统计信息
        if len(df) > 0:
            scenic_id = df.iloc[0]['scenic_id']
            stats = processor.get_statistics(scenic_id)
            logger.info(f"统计信息: {stats}")
        
    except Exception as e:
        logger.error(f"处理失败: {e}")
        raise
    finally:
        processor.close()


def process_dataframe(df: pd.DataFrame, config_path: str = None):
    """处理DataFrame数据"""
    # 加载配置
    config = load_config(config_path)
    
    # 创建处理器
    processor = LabelingProcessor(config)
    
    try:
        # 处理并保存
        logger.info("开始处理数据...")
        success_count = processor.process_and_save(df)
        logger.info(f"处理完成: 成功{success_count}/{len(df)}条")
        
        return success_count
        
    except Exception as e:
        logger.error(f"处理失败: {e}")
        raise
    finally:
        processor.close()


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='八大处公园评论数据标注系统')
    parser.add_argument('--input', '-i', required=True, help='输入CSV文件路径')
    parser.add_argument('--config', '-c', help='配置文件路径（YAML格式）')
    
    args = parser.parse_args()
    
    # 处理CSV文件
    process_csv_file(args.input, args.config)


if __name__ == '__main__':
    main()
