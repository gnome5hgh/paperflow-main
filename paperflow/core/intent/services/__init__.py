"""服务层：外部判定服务的客户端 + 与 Agent 的集成适配器。

`jev.py` 是意图层唯一依赖外部服务的地方；`service.py` 是 Agent 与整个意图模块之间
唯一的那道缝（`begin` 一个钩子）。两者合在一处，是因为「怎么问外部服务」与
「怎么把答案变成 head 里的一条提示」是同一件事的两半。
"""
from paperflow.core.intent.services.jev import JevClient, JevDecision, JevUnavailable
from paperflow.core.intent.services.service import IntentService, Turn

__all__ = ["IntentService", "JevClient", "JevDecision", "JevUnavailable", "Turn"]
