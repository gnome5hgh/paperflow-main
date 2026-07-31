# paperflow/core/memory/context_config.py
from dataclasses import dataclass

from pydantic import BaseModel


@dataclass
class ContextConfig:
    """上下文压缩配置。context_size=0 时从 model_window 自动推导（半窗口）。"""

    trigger_ratio: float = 0.8      # token > context_size × 0.8 触发压缩
    reserve_ratio: float = 0.1      # 压缩后保留最近 10% token
    context_size: int = 0           # 0 = 自动推导（model_window // 2）
    compression_prompt: str = (
        "你是对话上下文压缩器。请阅读以下对话，用 JSON 输出结构化摘要："
        "task_overview（用户核心请求和成功标准）、current_state（已完成进度）、"
        "important_discoveries（关键技术约束/决策/错误）、next_steps（待办和优先级）、"
        "context_to_preserve（用户偏好/领域细节/承诺）。只输出 JSON。"
    )
    summary_template: str = (
        "[对话摘要]\n任务：{task_overview}\n进度：{current_state}\n"
        "发现：{important_discoveries}\n下一步：{next_steps}\n"
        "保留：{context_to_preserve}"
    )

    def resolve_context_size(self, model_window: int) -> int:
        if self.context_size > 0:
            return self.context_size
        return model_window // 2    # 默认半窗口：DeepSeek 64K → 32K


class SummarySchema(BaseModel):
    task_overview: str
    current_state: str
    important_discoveries: str
    next_steps: str
    context_to_preserve: str
