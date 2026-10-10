"""core 内跨子包共享的叶子能力。

被多个子包横向消费、自身不依赖任何 core 子包的通用能力放这里：

- ``frontmatter.py`` —— AGENT.md / SKILL.md 的 YAML frontmatter 解析（core.agent 与 core.skills 共用）
- ``tokenization.py`` —— tiktoken token 计数单点（rag 切块与 core.memory 上下文压缩共用）
- ``text.py`` —— 未配对 surrogate 清洗（信任边界统一清洗：llm 客户端、记忆落盘、终端、rag 编码器都过它）

判据是「**被多个子包横向消费**」：只服务单一消费方的模块留在消费方自己的包里。
"""
from paperflow.core.common.frontmatter import parse_frontmatter
from paperflow.core.common.text import sanitize_surrogates
from paperflow.core.common.tokenization import TOKEN_ENCODING, get_token_encoder

__all__ = ["parse_frontmatter", "sanitize_surrogates", "TOKEN_ENCODING", "get_token_encoder"]
