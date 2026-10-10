"""paperflow 核心记忆子系统。

对外统一导出记忆栈组件：schema 数据模型、SQLite 存储层、服务层
（块/消息/agent 管理器 + MemFS）、压缩配置、
MemoryConsolidator 后台整合与常量。分层依赖单向：constants/schemas → storage → services → tools。
"""
from paperflow.core.memory.constants import MessageRole
from paperflow.core.memory.schemas.block import BaseBlock, Block
from paperflow.core.memory.schemas.memory import Memory
from paperflow.core.memory.schemas.message import Message
from paperflow.core.memory.schemas.agent import AgentState
from paperflow.core.memory.storage.database import MemoryDB
from paperflow.core.memory.services.block_manager import BlockManager, GitEnabledBlockManager
from paperflow.core.memory.services.memfs import MemFS
from paperflow.core.memory.services.message_manager import MessageManager
from paperflow.core.memory.services.agent_manager import AgentManager
from paperflow.core.memory.services.compaction import CompactionSettings, SummarySchema
from paperflow.core.memory.services.consolidation import MemoryConsolidator
from paperflow.core.memory import constants

__all__ = [
    "BaseBlock", "Block", "Memory", "Message", "MessageRole",
    "AgentState", "MemoryDB",
    "BlockManager", "GitEnabledBlockManager", "MemFS", "MessageManager",
    "AgentManager",
    "CompactionSettings", "SummarySchema", "MemoryConsolidator", "constants",
]
