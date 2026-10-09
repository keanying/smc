# 八大处公园评论数据标注系统

## 项目简介

本系统用于对八大处公园评论数据进行自动化标注，包括情感分析、维度标注、实体识别和关键词提取。系统支持物理规则过滤、LLM标注、MySQL存储等功能。

## 项目结构

```
labeling_system/
├── __init__.py          # 包初始化文件
├── config.py            # 配置管理模块
├── filters.py           # 物理规则过滤器模块
├── database.py          # MySQL数据库模块
├── api_client.py        # API客户端模块
├── processor.py         # 标注处理器模块
├── main.py              # 主入口文件
├── config.yaml          # 配置文件示例
├── example.py           # 使用示例
└── README.md            # 项目说明文档
```

## 核心功能

### 1. 物理规则过滤
在LLM打标前，使用物理规则过滤无效内容：
- 空内容
- 方括号表情：[心] [玫瑰] [赞] 等
- 纯emoji表情
- 纯语气词：哈哈哈、呵呵等
- 纯特殊符号
- 无意义内容：转发微博等

### 2. 哀悼/慰问类检测
自动检测哀悼/慰问/社会事件类内容，标记为中性：
- 包含"逝者安息"、"生者坚强"等关键词
- 包含"泪目"、"泪奔"、"流泪"等关键词
- 包含"残烈"、"惨烈"、"悲剧"等关键词

### 3. LLM标注
调用火山引擎API进行标注：
- 情感分析：正面/中性/负面
- 维度标注：6大维度体系
- 实体识别：8大实体类型
- 关键词提取：3-5个核心关键词

### 4. MySQL存储
处理完成后直接存储到MySQL数据库：
- 支持自动建表
- 支持批量插入
- 支持更新已存在记录
- 支持统计查询

## 安装依赖

```bash
pip install pandas openai sqlalchemy pymysql pyyaml tqdm
```

## 配置说明

### 1. 配置文件（config.yaml）

```yaml
# MySQL数据库配置
mysql:
  host: localhost
  port: 3306
  user: opinionhub
  password: '8drftttttttttt'
  database: opinionhub
  charset: utf8mb4
  pool_min: 2
  pool_max: 20
  auto_migrate: true

# API配置
api:
  model: deepseek-v3-250324
  temperature: 0.3
  max_tokens: 2000

# 处理配置
process:
  batch_size: 10
  max_workers: 7
  max_retries: 3
  retry_delay: 2
```

### 2. 环境变量

```bash
export OPENAI_API_KEY="your_api_key"
export OPENAI_API_BASE="https://ark.cn-beijing.volces.com/api/v3"
```

## 使用方法

### 1. 命令行使用

```bash
# 处理CSV文件
python -m labeling_system.main --input data.csv --config config.yaml
```

### 2. Python代码使用

#### 方式1：处理CSV文件

```python
from labeling_system.main import process_csv_file

process_csv_file("data.csv", "config.yaml")
```

#### 方式2：处理DataFrame

```python
import pandas as pd
from labeling_system.main import process_dataframe

df = pd.read_csv("data.csv")
success_count = process_dataframe(df, "config.yaml")
print(f"处理完成: 成功{success_count}/{len(df)}条")
```

#### 方式3：自定义处理

```python
from labeling_system.config import Config
from labeling_system.processor import LabelingProcessor

# 加载配置
config = Config.from_yaml("config.yaml")

# 创建处理器
processor = LabelingProcessor(config)

try:
    # 处理单行数据
    result = processor.process_row(row)
    
    # 处理批量数据
    results = processor.process_dataframe(df)
    
    # 获取统计信息
    stats = processor.get_statistics("PFTSCE01001721")
    
finally:
    processor.close()
```

## 数据库表结构

### comment_labels表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INT | 主键，自增 |
| comment_id | VARCHAR(64) | 评论ID，唯一索引 |
| scenic_id | VARCHAR(32) | 景区ID，索引 |
| scenic_name | VARCHAR(128) | 景区名称 |
| channel | VARCHAR(32) | 渠道，索引 |
| content | TEXT | 评论内容 |
| sentiment_label | VARCHAR(16) | 情感标签 |
| sentiment_score | FLOAT | 情感得分 |
| dimension_tags | TEXT | 维度标签（JSON） |
| entity_tags | TEXT | 实体标签（JSON） |
| keyword_tags | TEXT | 关键词标签（JSON） |
| label_reason | VARCHAR(64) | 标注原因 |
| label_type | VARCHAR(32) | 标注类型 |
| publish_time | DATETIME | 发布时间 |
| crawl_time | DATETIME | 抓取时间 |
| create_time | DATETIME | 创建时间 |
| update_time | DATETIME | 更新时间 |

## 标注规则

### 1. 默认中评（不调用API）
- 空内容
- 方括号表情：[心] [玫瑰] [赞] 等
- 纯emoji表情
- 纯语气词：哈哈哈、呵呵等
- 纯特殊符号
- 无意义内容：转发微博等

### 2. 哀悼/慰问类（中性）
- 包含"逝者安息"、"生者坚强"等关键词
- 包含"泪目"、"泪奔"、"流泪"等关键词
- 包含"残烈"、"惨烈"、"悲剧"等关键词

### 3. API标注
- 其他所有内容调用火山引擎API进行标注
- 返回情感得分、维度标签、实体标签、关键词标签

## 注意事项

1. **API密钥安全**：请妥善保管API密钥，不要提交到代码仓库
2. **数据库安全**：请妥善保管数据库密码，不要提交到代码仓库
3. **并发控制**：默认7线程并发，可根据API限流调整
4. **错误处理**：API失败自动重试3次，仍失败则标记为中性
5. **数据备份**：建议处理前备份原始数据

## 版本历史

- v1.0 (2026-08-31): 初始版本，支持基本标注功能
- v1.1 (2026-08-31): 添加物理规则过滤、哀悼/慰问类检测
- v1.2 (2026-08-31): 集成MySQL存储、完善错误处理

## 联系方式

如有问题，请联系开发团队。
