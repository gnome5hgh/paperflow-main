"""意图模块的常量：类别词汇、元数据映射与知识库资产路径。

分组决定消费方式（业务类按需派发、系统类不派发领域角色），不决定派发顺序，也
不作门禁——意图只往 head 注入一条提示，派不派、派谁由 supervisor 自主决定。
"""
from pathlib import Path

from .enums import IntentCategory, IntentType

__all__ = ["INTENT_CLASSES", "INTENT_META", "INTENT_KB_DIR", "RULES_PATH", "TAXONOMY_PATH"]

#: 类别词汇：**由枚举派生**（枚举是唯一声明点，知识库必须与它逐项一致，装载期校验）。
#: 顺序即枚举声明顺序；规则表按自己的声明顺序匹配，与本元组无关。
INTENT_CLASSES: tuple[str, ...] = tuple(t.value for t in IntentType)

#: 仓库安装根（本文件位于 core/intent/constants/，向上四级即仓库根）。
#: 随仓库发布的知识资产恒锚此处，不随 PAPERFLOW_RUNTIME_WORKSPACE 重定向。
_INSTALL_ROOT = Path(__file__).resolve().parents[4]

#: 知识库目录：随仓库发布的意图知识资产（类别口径/示例句、规则模式）
INTENT_KB_DIR = _INSTALL_ROOT / "data" / "intent"

#: 类别知识库与规则表的默认路径（单测与生产共用；显式传参可指向替身文件）
TAXONOMY_PATH = INTENT_KB_DIR / "taxonomy.yaml"
RULES_PATH = INTENT_KB_DIR / "rules.yaml"

#: 意图 → 类别分组。键必须与 IntentType 逐项一致（枚举 = 契约 = 实现集）。
INTENT_META: dict[IntentType, IntentCategory] = {
    IntentType.PAPER: IntentCategory.BUSINESS,
    IntentType.NOTE: IntentCategory.BUSINESS,
    IntentType.RESEARCH: IntentCategory.BUSINESS,
    IntentType.CITATION: IntentCategory.BUSINESS,
    IntentType.INDEX: IntentCategory.BUSINESS,
    IntentType.MEMORY: IntentCategory.BUSINESS,
    IntentType.QUESTION: IntentCategory.BUSINESS,
    IntentType.CHITCHAT: IntentCategory.SYSTEM,
    IntentType.OUT_OF_SCOPE: IntentCategory.SYSTEM,
    IntentType.HELP: IntentCategory.SYSTEM,
    IntentType.FEEDBACK: IntentCategory.SYSTEM,
}
