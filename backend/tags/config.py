"""
配置管理模块
"""
import os
import yaml
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class MySQLConfig:
    """MySQL配置"""
    host: str = "localhost"
    port: int = 3306
    user: str = "opinionhub"
    password: str = ""
    database: str = "opinionhub"
    charset: str = "utf8mb4"
    pool_min: int = 2
    pool_max: int = 20
    auto_migrate: bool = True


@dataclass
class APIConfig:
    """API配置"""
    api_key: str = "ark-30981b8"
    api_base: str = "https://ark.cn-beijing.volces.com/api/v3"
    model: str = "deepseek-v3-250324"
    temperature: float = 0.3
    max_tokens: int = 2000


@dataclass
class ProcessConfig:
    """处理配置"""
    batch_size: int = 10
    max_workers: int = 7
    max_retries: int = 3
    retry_delay: int = 2


@dataclass
class Config:
    """主配置类"""
    mysql: MySQLConfig
    api: APIConfig
    process: ProcessConfig
    
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
    
    @classmethod
    def from_yaml(cls) -> 'Config':
        """从YAML文件加载配置"""
        yaml_path: str = os.path.abspath('../../config/config.yaml')
        with open(yaml_path, 'r', encoding='utf-8') as f:
            config_dict = yaml.safe_load(f)
        
        # 解析MySQL配置
        mysql_config = MySQLConfig(
            host=config_dict.get('mysql', {}).get('host', 'localhost'),
            port=config_dict.get('mysql', {}).get('port', 3306),
            user=config_dict.get('mysql', {}).get('user', 'opinionhub'),
            password=config_dict.get('mysql', {}).get('password', ''),
            database=config_dict.get('mysql', {}).get('database', 'opinionhub'),
            charset=config_dict.get('mysql', {}).get('charset', 'utf8mb4'),
            pool_min=config_dict.get('mysql', {}).get('pool_min', 2),
            pool_max=config_dict.get('mysql', {}).get('pool_max', 20),
            auto_migrate=config_dict.get('mysql', {}).get('auto_migrate', True)
        )
        
        # 解析API配置
        api_config = APIConfig(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            api_base=os.environ.get("OPENAI_API_BASE", "https://ark.cn-beijing.volces.com/api/v3"),
            model=config_dict.get('api', {}).get('model', 'deepseek-v3-250324'),
            temperature=config_dict.get('api', {}).get('temperature', 0.3),
            max_tokens=config_dict.get('api', {}).get('max_tokens', 2000)
        )
        
        # 解析处理配置
        process_config = ProcessConfig(
            batch_size=config_dict.get('process', {}).get('batch_size', 10),
            max_workers=config_dict.get('process', {}).get('max_workers', 7),
            max_retries=config_dict.get('process', {}).get('max_retries', 3),
            retry_delay=config_dict.get('process', {}).get('retry_delay', 2)
        )
        
        return cls(mysql=mysql_config, api=api_config, process=process_config)
    
    @classmethod
    def default(cls) -> 'Config':
        """创建默认配置"""
        return cls(
            mysql=MySQLConfig(),
            api=APIConfig(),
            process=ProcessConfig()
        )
