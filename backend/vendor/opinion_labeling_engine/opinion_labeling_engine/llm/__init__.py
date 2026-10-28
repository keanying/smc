# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""大模型接入层：客户端、提示词、输出解析。不含业务规则。"""

from .client import ArkClient, LLMError, LLMResponse, RetryableLLMError, TokenBucket, extract_output_text
from .parser import ParseError, RawLabel, extract_json_object, parse_label_output
from .prompt import OUTPUT_SCHEMA_HINT, PromptBuilder

__all__ = [
    "ArkClient",
    "LLMError",
    "LLMResponse",
    "RetryableLLMError",
    "TokenBucket",
    "extract_output_text",
    "ParseError",
    "RawLabel",
    "extract_json_object",
    "parse_label_output",
    "OUTPUT_SCHEMA_HINT",
    "PromptBuilder",
]
