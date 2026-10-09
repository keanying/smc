"""
物理规则过滤器模块
在LLM打标前过滤特殊字符、表情包等无效内容
"""
import re
import pandas as pd
from typing import Tuple


class PhysicalFilter:
    """物理规则过滤器"""
    
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
    
    @staticmethod
    def is_meaningless(text: str) -> bool:
        """检测无意义内容（如"转发微博"、"//@xxx"等）"""
        if pd.isna(text) or not text:
            return False
        
        text = str(text).strip()
        
        # 无意义模式
        meaningless_patterns = [
            r'^转发微博$',
            r'^//@.*',
            r'^Repost$',
            r'^repost$',
        ]
        
        for pattern in meaningless_patterns:
            if re.match(pattern, text):
                return True
        
        return False
    
    @classmethod
    def should_filter(cls, text: str) -> Tuple[bool, str]:
        """
        判断是否应该过滤（跳过LLM标注）
        返回：(是否过滤, 原因)
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
        
        if cls.is_meaningless(text):
            return True, "无意义内容"
        
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
