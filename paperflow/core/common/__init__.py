"""core 内跨子包共享的叶子能力与取值。

被多个子包横向消费、自身不依赖任何 core 子包的通用能力放这里：

- ``services/frontmatter.py`` —— AGENT.md / SKILL.md 的 YAML frontmatter 解析（core.agent 与 core.skills 共用）
- ``services/tokenization.py`` —— tiktoken token 计数单点（rag 切块与 core.memory 上下文压缩共用）
- ``services/text.py`` —— 未配对 surrogate 清洗（信任边界统一清洗：llm 客户端、记忆落盘、终端、rag 编码器都过它）
- ``constants/`` —— 没有子包归属、又被两个以上子包消费的纯取值与枚举
  （重试退避基数；消息角色 `MessageRole`）

判据是「**被多个子包横向消费**」：只服务单一消费方的模块留在消费方自己的包里。
三个叶子进 ``services/`` 与 ``core/security/services/`` 同一口径——那里放的就是
「被消费方直接调用的纯能力」。
"""
from paperflow.core.common.constants import RETRY_BACKOFF_BASE, MessageRole
from paperflow.core.common.services import (
    TOKEN_ENCODING,
    get_token_encoder,
    parse_frontmatter,
    sanitize_surrogates,
)

__all__ = [
    "parse_frontmatter",
    "sanitize_surrogates",
    "TOKEN_ENCODING",
    "get_token_encoder",
    "RETRY_BACKOFF_BASE",
    "MessageRole",
]
