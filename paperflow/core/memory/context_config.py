"""上下文压缩的配置项与摘要数据结构。

ContextConfig 用 dataclass 保存压缩触发比例、保留比例、上下文窗口等参数，并提供
把模型窗口换算成实际压缩用窗口的方法；SummarySchema 定义模型提取结构化摘要时
输出的字段结构。
"""
from dataclasses import dataclass

from pydantic import BaseModel


@dataclass
class ContextConfig:
    """上下文压缩配置。context_size=0 时从 model_window 自动推导（取半窗口）。"""

    trigger_ratio: float = 0.8      # token 估算超过 context_size × 0.8 即触发压缩
    reserve_ratio: float = 0.1      # 压缩后保留最近约 10% token 的近期对话
    context_size: int = 0           # 0 = 自动推导（取模型窗口的一半）
    compression_prompt: str = (
        # 压缩时发给模型的摘要提取提示词
        "你是对话上下文压缩器。请阅读以下对话，用 JSON 输出结构化摘要："
        "task_overview（用户核心请求和成功标准）、current_state（已完成进度）、"
        "important_discoveries（关键技术约束/决策/错误）、next_steps（待办和优先级）、"
        "context_to_preserve（用户偏好/领域细节/承诺）。只输出 JSON。"
    )
    summary_template: str = (
        # 摘要落盘成文本的模板，占位符与 SummarySchema 字段一一对应
        "[对话摘要]\n任务：{task_overview}\n进度：{current_state}\n"
        "发现：{important_discoveries}\n下一步：{next_steps}\n"
        "保留：{context_to_preserve}"
    )

    def resolve_context_size(self, model_window: int) -> int:
        """返回实际用于压缩的上下文窗口大小。

        context_size 配置大于 0 时直接用配置值；否则取模型窗口的一半作为默认值，
        让压缩预算随模型容量自适应，无需手动配置。
        """
        if self.context_size > 0:
            return self.context_size
        return model_window // 2    # 默认取半窗口，压缩预算随模型窗口自适应


class SummarySchema(BaseModel):
    """模型输出的结构化摘要字段。"""

    task_overview: str              # 用户核心请求与成功标准
    current_state: str              # 已完成进度
    important_discoveries: str      # 关键技术约束/决策/错误
    next_steps: str                 # 待办与优先级
    context_to_preserve: str        # 用户偏好/领域细节/承诺
