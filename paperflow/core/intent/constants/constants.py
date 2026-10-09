"""意图模块的常量：类别词汇与知识库资产路径。

只有跨**文件**共享、且不属于某个消费方的常量才放这里——类别名与知识库位置符合
这条；结构常量与算法契约（正则、重试退避基数、提示词文案）留在各自的消费处就地
声明。类别**不分组**：曾经那张「意图 → 业务/系统」的映射只服务已删的派发过滤，
没有运行时消费者；每个类别该派给谁由 `rules_block` 的文案与 supervisor 的判断承担。
"""
from pathlib import Path

from .enums import IntentType

__all__ = ["INTENT_CLASSES", "INTENT_KB_DIR", "RULES_PATH", "TAXONOMY_PATH"]

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
