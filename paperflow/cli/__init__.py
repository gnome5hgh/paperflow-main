# paperflow/cli/__init__.py
"""CLI 装配组合根 + 启动预检：装配全部依赖（LLM/记忆/安全/意图）并启动 REPL。

交互半区（REPL 循环、确认/提问回调、横幅）在 `paperflow.terminal.repl`——本包负责
两件事：装配前启动预检（依赖服务自动拉起，见 `bootstrap.py`）与组装对象图（装配顺序
与依赖方向见 `main.py` 的 `main()` docstring），不承载终端交互逻辑。

包内按角色分两个模块：`bootstrap.py`（依赖服务预检）+ `assembly.py`（装配组合根）；
本文件只做再导出 + 一处**必须在任何 grpc 导入之前执行**的环境守卫。
"""
import os

# 必须在任何可能拉起 grpc 的导入（paperflow.rag → pymilvus）之前设置——gRPC C 核心在
# 初始化时读取。不设时 gRPC 打 INFO 级日志：MCP server 子进程 fork 时，pymilvus
# 已建连的轮询 fd 残留会让每个子进程打一行 "FD from fork parent still in poll
# list"（ev_poll_posix.cc），纯噪音。setdefault 不覆盖用户显式配置。
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

from paperflow.cli.assembly import main

__all__ = ["main"]
