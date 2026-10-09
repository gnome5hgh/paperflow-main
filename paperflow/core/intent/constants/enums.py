"""意图层的词汇：类别、类别分组、判定来源。

类别回答的是「用户要哪一类工作」，按**产物主人**划分——一个类别对应一类产物/事务
的责任人，而不是一个具体动作。枚举值即类别名，与知识库 `data/intent/taxonomy.yaml`
的键逐项一致（装载期 fail-closed 校验，见 `taxonomy.py`）。
"""
from enum import StrEnum

__all__ = ["IntentType", "IntentCategory", "IntentStep"]


class IntentType(StrEnum):
    """11 个意图类别（业务 7 + 系统 4）。

    值得单列的区别要两条同时成立：**supervisor 的动作确实不同**，且**这个区别从用户
    原句里读不出来**。凡是能从文本读出的动作（读/写/删/查）与类别正交，单列只会制造
    误差——三个删除类因此并入了各自的领域类别。
    """

    # ── 业务：各自对应一个领域责任人 ──
    PAPER = "paper"                # 论文事务：检索/下载/读指定论文/分析图表/删 PDF
    NOTE = "note"                  # 笔记事务：撰写/删除笔记
    RESEARCH = "research"          # 选题事务：选题发现/研究计划/删选题产物
    CITATION = "citation"          # 引用库事务：references.bib 同步/增删/查询导出
    INDEX = "index"                # 语料索引事务：入库/重建/索引体检
    MEMORY = "memory"              # 记忆事务：记忆与清单查询、记账；**用户陈述自身信息**也归此类
    QUESTION = "question"          # 即问即答：先自答，需依据时取材料

    # ── 系统：不派发领域角色（feedback 例外，只派 memory-agent）──
    CHITCHAT = "chitchat"          # 寒暄应答
    OUT_OF_SCOPE = "out_of_scope"  # 非本系统任务：明确越界，或看不出要做什么
    HELP = "help"                  # 功能与方法询问
    FEEDBACK = "feedback"          # 对系统表现的评价与纠正


class IntentCategory(StrEnum):
    """类别的消费分组——是分组，不是路由层级。

    Attributes:
        BUSINESS: 对应领域责任人的业务类别，按需派发。
        SYSTEM: 不派发领域角色的类别（feedback 只派记忆域）。
    """

    BUSINESS = "business"
    SYSTEM = "system"


class IntentStep(StrEnum):
    """本轮判定来自哪一层。

    Attributes:
        RULE: 规则层的确定性模式命中。
        JEV: 判定服务（结合对话史的模型判定）。
    """

    RULE = "rule"
    JEV = "jev"
