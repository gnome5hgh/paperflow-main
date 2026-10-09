"""意图类型 / 类别 / 产出阶段枚举（值即路由名，对应 routes.yaml）。"""

from enum import StrEnum

__all__ = ["IntentType", "IntentCategory", "IntentStep"]


class IntentType(StrEnum):
    """意图类型枚举，value 与路由名一致（routes.yaml 中的 name）。

    枚举 = 契约 = 当前实现集——不允许"枚举允许但系统无处理路径"的悬空值。
    18 值按三类组织（category 见 INTENT_META），类别是消费分组不是路由层级。
    历史收敛：switch_topic 并入 set_research_topic、refine_query 并入
    search_paper——两者与近邻意图的边界是对话史信号，路由器原理上不可学，
    且派发行为与保留值完全一致。
    """

    SET_RESEARCH_TOPIC = "set_research_topic"  # 研究 topic 管理：设定/切换（业务；记录+归档，不派发）
    MENU_SELECTION = "menu_selection"          # 菜单选项答复（对话管理；选择动作不重分类，派发权在 supervisor 对照菜单）
    SEARCH_PAPER = "search_paper"              # 搜索/查找论文（业务；含对上轮检索的修正重搜；槽位 query/source/year/download）
    ASK_QUESTION = "ask_question"              # 具体问答：即问即答的单点问题（业务）
    GENERATE_NOTE = "generate_note"            # 撰写笔记（业务）
    RESEARCH_DISCOVERY = "research_discovery"  # 选题发现：交付方向/课题建议（业务；搜文献只是其手段）
    ANALYZE_PAPER = "analyze_paper"            # 精读分析：交付分析报告的长任务（业务）
    MANAGE_MEMORY = "manage_memory"            # 记忆查询 + 待读清单操作（业务）
    MANAGE_CITATIONS = "manage_citations"      # 引用库管理：references.bib 的批量同步/单篇添加/删除/查询导出（业务）。
                                               # 边界：管的是 bib 引用库不是待读清单（那归 manage_memory）；
                                               # 管的是已有语料的元数据整理不是找论文（那归 search_paper）。
                                               # 判据：请求的落点是 references.bib 发生变化或被读取。
    MANAGE_INDEX = "manage_index"              # 索引库管理：把语料入库/重建索引/索引体检（业务）。
                                               # 边界：管的是检索索引不是 bib 引用库（那归 manage_citations）。
                                               # 判据：请求的落点是向量库/关键词索引发生变化或被重建。
    DELETE_NOTE = "delete_note"                # 删除笔记文件（业务）。判据：落点是磁盘上的笔记文件被删除。
                                               # 边界：删的是磁盘文件不是记忆里的清单条目（那归 manage_memory）。
    DELETE_RESEARCH = "delete_research"        # 删除研究产物：survey/gaps/idea 卡/研究计划（业务）。
                                               # 边界：删的是已落盘产物，不是新做选题（那归 research_discovery）。
    DELETE_PDF = "delete_pdf"                  # 删除论文 PDF 文件（业务）。
                                               # 边界：落点是磁盘 PDF 文件；从 bib 移除条目归 manage_citations。
    CHITCHAT = "chitchat"                      # 闲聊与应答语（系统；直接回复）
    OUT_OF_SCOPE = "out_of_scope"              # 超出能力范围：含与论文工作无关的请求（系统；明确拒绝）
    HELP = "help"                              # 本系统的使用方法/功能引导（系统）；系统无关请求归 out_of_scope
    FEEDBACK = "feedback"                      # 结果反馈（系统；记忆日志）
    UNCLASSIFIED = "unclassified"              # 未分类兜底：路由未命中 / LLM 解析失败（系统）。仅 LLM 兜底产出，不在路由知识库


class IntentCategory(StrEnum):
    """意图类别——消费分组（三类组织），非路由层级。"""

    BUSINESS = "business"        # 业务：派发领域 agent 或记忆操作
    DIALOGUE = "dialogue"        # 对话管理：会话状态操作（继承/归档/重派）
    SYSTEM = "system"            # 系统：直接回复，永不 spawn


class IntentStep(StrEnum):
    """产出阶段枚举——让审计/监控能看出意图由哪一级产出。"""

    ENTITIES = "entities"                  # 实体提取阶段
    OPTION = "option"                      # 选项答复检测阶段（纯编号菜单选择，确定性正则）
    FOLLOWUP = "followup"                  # 追问检测阶段（依赖会话上下文）
    ROUTER = "router"                      # 混合路由阶段
    LLM = "llm"                            # LLM 兜底阶段
    USER = "user"                          # 用户确认阶段——澄清回路的用户选择代码级落地
                                           # （管线澄清编号选择 / ask_user 带 intent_options），
                                           # 不经路由器复判（同一句话复判只会复现同一误判）
