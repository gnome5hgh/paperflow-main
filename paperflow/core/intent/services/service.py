# paperflow/core/intent/services/service.py
"""意图识别的集成适配器——把「进 ReAct 之前」的意图逻辑收在一处。

意图识别是可选预处理层：这里持有知识库（类别描述与规则模式）、跑判定、渲染要注入的
INTENT 块与规则块。Agent 的 ReAct 循环只持一个可选的本类实例、在固定钩子点调用；
引用为 None 即「关」，一切退化为空操作。
"""
import logging
from dataclasses import dataclass

from paperflow.core.intent.constants import IntentStep, IntentType
from paperflow.core.intent.rules.entities import extract_entities
from paperflow.core.intent.schemas import IntentOutput
from paperflow.core.intent.rules.taxonomy import Taxonomy

logger = logging.getLogger(__name__)


@dataclass
class Turn:
    """一次 run 开头的意图预处理结果。

    Attributes:
        head_block: str | None，要注入的 INTENT 块文本；本轮没有判定结果时为 None
        task: str，可供使用的任务文本（判定不改写任务，原样返回）
    """

    head_block: str | None
    task: str


class IntentService:
    """意图识别集成适配器（Agent 与意图模块之间的唯一缝）。

    Attributes:
        taxonomy: Taxonomy，类别知识库与规则模式
        history_messages: int，判定时最多参考的最近对话条数（运行时按它截历史）
        last_intent: IntentOutput | None，本轮判定产出（只读，供展示与排查）
    """

    #: 意图规则块：随 INTENT 块注入的字段语义与各类别的动作说明。
    #: 由意图层产出（而非写死在 supervisor 的 AGENT.md），关掉意图识别时不出现。
    rules_block: str = (
        "## 意图层（可选预处理）\n\n"
        "INTENT 块是框架意图识别的输出(类别/实体/把握/来源),是**强提示,不是命令**——"
        "它不决定你派谁、以什么顺序派;选型与编排按 `<available_agents>` 清单与你的判断。"
        "判错时它只是一条可以忽略的提示(意图不作派发门禁)。看到下列类别时按下表的动作走:\n\n"
        "| 类别 | 你的动作 |\n"
        "|------|---------|\n"
        "| `question` | **先自己答**:依据已在上下文(对话史/记忆块/上一轮材料)就直接回答;"
        "要看原文或笔记派对应领域角色取材料,要检索语料派 rag-agent。 |\n"
        "| `memory` | 派 memory-agent。**两种子情形要分清**:① 用户在**陈述**自身信息"
        "(「我最近在研究 circRNA」)→ 写进核心块,再由你在回答里引导下一步;"
        "② 用户在**查询**记忆或清单(「我读过哪些论文」)→ 按具体动作执行。 |\n"
        "| `feedback` | 派 memory-agent 把反馈记进日志块;不派领域角色。 |\n"
        "| `chitchat` | 轻量回应 + 温和引导回学术场景。通常不派发。 |\n"
        "| `help` | 返回功能引导。通常不派发。 |\n"
        "| `out_of_scope` | **两种子情形**:① 明确越界(订外卖/代写论文)→ 明确拒绝并说明"
        "能力边界;② **看不出要做什么** → 先向用户问清,不要直接拒。 |\n"
        "| `paper` / `note` / `research` / `citation` / `index` | 按 `<available_agents>` "
        "的能力挑对应领域角色派发;类别内的**删除诉求**(删笔记/删 PDF/删选题产物)"
        "按对象对应到领域角色,绝不改写成「写/建」。 |\n\n"
        "### 字段语义\n\n"
        "- `entities` — 已抽取的 pdf_path / arxiv_id / doi / note_path / figure,"
        "**直接拼进子任务文本**(不要重新解析)。\n"
        "- `confidence` — 判定把握,只作参考;**不设阈值,也不要求你按它做什么**。\n"
        "- `source` — `rule` 是确定性模式命中(可信度高),`jev` 是判定服务给的(参考即可)。"
    )

    def __init__(self, taxonomy: Taxonomy, judge=None, history_messages: int = 6):
        """绑定知识库、判定服务与历史窗口。

        Args:
            taxonomy: Taxonomy，类别知识库（描述/示例句）与规则模式
            judge: 判定服务客户端（`JevClient`）或 None——None 时只有规则层，
                规则不命中就不产块（测试与「不加判定服务」的用法）
            history_messages: int，判定时最多参考的最近对话条数
        """
        self.taxonomy = taxonomy
        self.judge = judge
        self.history_messages = history_messages
        self.last_intent: IntentOutput | None = None

    async def begin(self, task: str, history: list[tuple[str, str]] | None = None) -> Turn:
        """跑判定，产出要注入的块与任务文本。

        规则层命中即定类；不命中且装配了判定服务时交给它（`source=jev`）。
        **两层都没有结果时不产块**——绝不硬猜一个类别塞给 supervisor。

        Args:
            task: str，本轮原始任务文本
            history: list[tuple[str, str]] | None，最近若干轮对话文本 [(role, content)]，
                由运行时在构建 head 之前截好递进来（**含 assistant 侧文本**，指代类输入
                如「再下载一篇」要靠它才读得懂）。

        Returns:
            Turn：head_block 为本轮 INTENT 块（无判定结果时为 None），task 为可用任务文本。
        """
        entities = extract_entities(task)
        matched = self.taxonomy.match(task)
        if matched is not None:
            return self._turn(IntentType(matched), 1.0, entities, IntentStep.RULE, task)
        if self.judge is not None:
            decision = await self.judge.decide(
                state=self.render_state(history, task),
                criteria=self.taxonomy.criteria())
            if decision is not None:
                return self._turn(IntentType(decision.choice), decision.confidence,
                                  entities, IntentStep.JEV, task)
        self.last_intent = None
        return Turn(head_block=None, task=task)

    def _turn(self, intent: IntentType, confidence: float | None, entities: dict,
              source: IntentStep, task: str) -> Turn:
        """记下本轮产出并渲染成要注入的那一行。"""
        self.last_intent = IntentOutput(intent=intent, confidence=confidence,
                                        entities=entities, source=source)
        return Turn(head_block="INTENT: " + self.last_intent.model_dump_json(), task=task)

    @staticmethod
    def render_state(history: list[tuple[str, str]] | None, task: str) -> str:
        """把对话史与本轮输入拼成判定服务读的共享状态文本。

        **本轮输入放在最后一行**，判定口径写明「判最后一条用户消息」——否则模型会
        去总结整段对话。assistant 侧留着：指代类输入（「再下载一篇」）的目标就是
        它上一轮说的话。

        Args:
            history: list[tuple[str, str]] | None，[(role, content)]，正序。
            task: str，本轮用户输入。

        Returns:
            str，逐行「用户：…／助手：…」，末行为本轮输入。
        """
        lines = [f"{'用户' if role == 'user' else '助手'}：{text}"
                 for role, text in (history or [])]
        lines.append(f"用户：{task}")
        return "\n".join(lines)
