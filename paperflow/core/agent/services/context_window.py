"""in-context 窗口：本会话「这次要发给模型的那批消息」及其持久化 id 追踪。

窗口有两个视图，必须一起维护：``messages``（wire 消息，发给模型）与 ``ids``
（这些消息在 messages 表里的主键，跨轮回放与压缩映射靠它）。两者此前是 Agent 上
两个手对齐的列表，追加与落盘是分开的两句——漏写一句就出现「有消息没 id」或
「有 id 没消息」。收进这个对象后，动作只有四类，每类自己保证两边一致：

- ``load()``：按 AgentState.message_ids 回放窗口（跨轮），随后补齐孤儿 tool 消息；
- ``append()``：只进缓冲，不落盘（当前轮任务的载体在 head 里，不在缓冲）；
- ``append_and_persist()``：进缓冲并落盘取 id——run() 里每个消息分支都用它；
- ``replace()``：压缩后用新窗口整体替换，**先按旧窗口把已落盘消息的 id 认回来**
  再赋值。这个先后关系是硬的：认领靠 `id(消息对象)` 比对旧窗口，反过来做会把
  保留尾部当成新消息重复落盘，静默产生重复 SQL 行。

``persist()`` 单独留给「消息不在缓冲里、但必须落盘记账」的那一种情况：本轮用户
任务（它排在 head 末尾，从不出现在 messages 里，但要让下一轮回放得到）。因此
``ids`` 末尾可能比 ``messages`` 多出若干条——映射按**前缀**对齐（`zip` 天然如此），
不要假定两表等长。
"""
from __future__ import annotations

from paperflow.core.llm import (
    AssistantMessage, Message, ToolMessage, message_from_role,
)
from paperflow.core.llm.services.client import _message_to_openai

__all__ = ["ContextWindow", "CANCELLED_TOOL_MSG"]

#: 取消路径合成的 tool 消息（历史自愈）。用自解释措辞：裸的 "cancelled" 会让模型
#: 把中断编造成「子任务失败/被外部打断」等错误叙述——这里明确因果（用户主动
#: Ctrl+C）并禁止错误归因。
CANCELLED_TOOL_MSG = (
    '{"decision":"cancelled",'
    '"reason":"用户主动中断(Ctrl+C)，本轮工具调用作废。'
    '这不是子任务失败或超时，请勿将中断归因于其他原因。"}'
)


def _schema_to_wire(m) -> Message:
    """StoredMessage（Recall 持久化视图）→ wire 消息（回放进 in-context 窗口）。

    Args:
        m: StoredMessage，Recall 持久化视图

    Returns:
        对应角色的 wire 消息（空 content 归一为空串）。
    """
    return message_from_role(
        m.role,
        content=m.content or "",
        tool_calls=m.tool_calls or None,
        tool_call_id=m.tool_call_id,
    )


class ContextWindow:
    """本会话的 in-context 窗口与持久化 id 追踪。

    无 MessageManager 装配（无记忆的子 agent）时整类退化为纯内存缓冲：load 清空、
    append 生效、persist 与 replace 只动缓冲不碰 SQL——调用方行为与从前一致。

    Attributes:
        messages: list[Message]，当前窗口的 wire 消息
        ids: list[str]，已落盘消息的 id（与 messages 按前缀对齐，末尾可能多出
            「只落盘不进缓冲」的消息 id）
    """

    def __init__(self, message_manager=None, agent_manager=None, session_id: str = ""):
        """
        Args:
            message_manager: MessageManager | None，对话落盘与窗口回放；None 时
                所有 SQL 动作降级为空操作
            agent_manager: AgentManager | None，窗口 id 列表（AgentState.message_ids）
                的持久化；None 时只维护内存
            session_id: str，会话标识（MessageManager 侧就是 agent_id）
        """
        self._mm = message_manager
        self._am = agent_manager
        self._session_id = session_id
        self.messages: list[Message] = []
        self.ids: list[str] = []

    def wire(self) -> list[dict]:
        """窗口的只读 wire 视图（外部可观察不可改）。

        Returns:
            OpenAI wire 格式的消息 dict 列表。
        """
        return [_message_to_openai(m) for m in self.messages]

    def load(self) -> None:
        """清空并按该会话的 message_ids 回放窗口（跨轮），随后补齐孤儿 tool 消息。

        无 MessageManager 时只是清空缓冲。回放的来源是 AgentState.message_ids——
        有它按 id 精确查询（压缩后的窗口 = 摘要 + 保留尾部），没有则降级全量查询
        该 agent 的所有消息（首轮/未压缩）。
        """
        self.messages = []
        self.ids = []
        if self._mm is None:
            return
        loaded = self._mm.get_in_context_messages(self._session_id)
        self.messages = [_schema_to_wire(m) for m in loaded]
        self.ids = [m.id for m in loaded]
        self._patch_orphans()

    def append(self, msgs) -> None:
        """把消息追加进缓冲（不落盘）。

        接受单条 Message 或列表——run() 各分支两种风格混用（合成 tool 消息是列表，
        截断续写是单条），统一归一。

        Args:
            msgs: Message | list[Message]，待追加的消息
        """
        if isinstance(msgs, Message):
            msgs = [msgs]
        self.messages.extend(msgs)

    def persist(self, msgs) -> None:
        """只落盘并记账 id，不追加进缓冲。

        专供「消息不在缓冲里但必须落盘」的那一种情况：本轮用户任务排在 head 末尾
        参与每次请求，从不出现在 messages 里，却要被下一轮回放得到。

        Args:
            msgs: Message | list[Message]，待落盘的消息
        """
        if self._mm is None:
            return
        if isinstance(msgs, Message):
            msgs = [msgs]
        added = [self._mm.add_message(self._session_id, m).id for m in msgs]
        if self._am is None:
            return
        for mid in added:
            if mid not in self.ids:
                self.ids.append(mid)
        self._am.update_agent(self._session_id, message_ids=self.ids)

    def append_and_persist(self, msgs) -> None:
        """进缓冲并落盘取 id——run() 里每个「新消息」分支的唯一入口。

        Args:
            msgs: Message | list[Message]，待追加并落盘的消息
        """
        self.append(msgs)
        self.persist(msgs)

    def replace(self, new_window: list[Message]) -> None:
        """压缩后用新窗口整体替换：先认回已落盘消息的 id，再更新两表。

        新窗口里的「保留尾部」是原缓冲里的同一批对象（`run_compaction` 不改它们），
        摘要消息是新造的。按 `id(对象)` 从旧窗口认领 id 正是为了区分这两者：认到的
        沿用旧 id，认不到的新建一条——**认领必须发生在替换之前**，否则新窗口全被
        当成新消息，尾部会重复落盘。

        无 AgentManager（未追踪窗口状态）时只动内存缓冲，SQL 保持原样。

        Args:
            new_window: list[Message]，压缩后的新窗口
        """
        if self._am is None:
            self.messages = new_window
            return
        old_id_by_msg = {id(m): mid for m, mid in zip(self.messages, self.ids)}
        new_ids: list[str] = []
        for m in new_window:
            mid = old_id_by_msg.get(id(m))
            if mid is None:
                mid = self._mm.add_message(self._session_id, m).id
            new_ids.append(mid)
        self.messages = new_window
        self.ids = new_ids
        self._am.update_agent(self._session_id, message_ids=new_ids)

    def _patch_orphans(self) -> None:
        """回放校验：为孤儿 tool_calls 就地补合成 tool 消息（历史自愈兜底）。

        历史数据里可能存在 assistant(tool_calls) 无配对 tool 消息的坏记录（早期版本
        在取消路径直接崩溃、没来得及合成），原样回放给 OpenAI 会报 400「tool_calls
        后必须紧跟 tool 消息」。这里扫描整个窗口，对每个未被响应的 tool_call 在其
        assistant 消息后补一条 cancelled tool 消息，并落盘保持 id 对齐。
        """
        if self._mm is None or not self.messages:
            return
        answered = {
            m.tool_call_id for m in self.messages
            if m.role == "tool" and m.tool_call_id
        }
        new_msgs: list[Message] = []
        new_ids: list[str] = []
        changed = False
        for m, mid in zip(self.messages, self.ids):
            new_msgs.append(m)
            new_ids.append(mid)
            # 只有 assistant 消息可能携带 tool_calls——其余角色没有这个字段
            tcs = (m.tool_calls or []) if isinstance(m, AssistantMessage) else []
            missing = [
                tc for tc in tcs
                if isinstance(tc, dict) and tc.get("id") and tc["id"] not in answered
            ]
            if missing:
                changed = True
                for tc in missing:
                    synth = ToolMessage(content=CANCELLED_TOOL_MSG,
                                        tool_call_id=tc["id"])
                    new_ids.append(
                        self._mm.add_message(self._session_id, synth).id)
                    new_msgs.append(synth)
        if changed:
            self.messages = new_msgs
            self.ids = new_ids
            if self._am is not None:
                self._am.update_agent(self._session_id, message_ids=new_ids)
