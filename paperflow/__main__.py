"""python -m paperflow — Layer 0 demo: ReAct loop with _demo agent.

这是 paperFlow 的唯一 CLI 入口。Layer 0 仅做最简 demo 验证：
    1. 加载配置（环境变量 + config.yaml）
    2. 初始化 LLM 客户端
    3. 扫描 agents/ 目录 → 加载 _demo Agent
    4. 启动 ReAct 循环 → 调用 EchoTool → 输出结果

后续 Layer 将替换为交互式 REPL 模式（读用户输入 → 意图识别 → Supervisor 调度）。
"""

import asyncio

from paperflow.config import PaperFlowConfig
from paperflow.core.llm import LLMClient
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.agent import Agent


async def main() -> None:
    """
    主入口协程：按顺序串联 配置 → LLM → Registry → Agent → ReAct 循环。

    第一步：加载配置（环境变量 PAPERFLOW_* > config.yaml > 默认值）
    第二步：基于 LLMConfig 创建 OpenAI-compatible 客户端
    第三步：扫描 agents/ 目录，发现并加载所有 Agent
    第四步：按 agent_type="_demo" 创建 Agent 实例（pull 模式）
    第五步：传入任务文本，启动 ReAct 循环，LLM 决定调用 EchoTool
    第六步：打印最终结果
    """
    # 加载配置：环境变量 > config.yaml > 默认值
    config = PaperFlowConfig.from_env()

    # 创建 LLM 客户端（OpenAI SDK 封装，async 接口）
    llm = LLMClient(config.llm)

    # 扫描 agents/ 目录 → 加载所有 SKILL.md + tools.py
    registry = AgentRegistry(config.agents_dir)

    # Pull 模式：按 agent_type 从注册表加载配置和工具
    agent = Agent(llm=llm, agent_registry=registry, agent_type="_demo")

    # 启动 ReAct 循环：LLM 收到任务 → 决定调用 echo tool → 整理结果 → 返回
    result = await agent.run("Hello, echo this message!")
    print(result)


if __name__ == "__main__":
    # asyncio.run() 管理事件循环的创建、运行和关闭
    asyncio.run(main())
