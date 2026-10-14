"""core 内被多处直接调用的纯能力（叶子实现）。

判据与 `core/security/services/` 一致：**被消费方直接调用的纯能力**放 `services/`——
这里是 frontmatter 解析、token 计数与未配对字符清洗三个叶子，它们都不是某个领域的
模型或状态，而是横跨多个子包的函数。
"""
from paperflow.core.common.services.frontmatter import parse_frontmatter
from paperflow.core.common.services.text import sanitize_surrogates
from paperflow.core.common.services.tokenization import TOKEN_ENCODING, get_token_encoder

__all__ = ["parse_frontmatter", "sanitize_surrogates", "TOKEN_ENCODING", "get_token_encoder"]
