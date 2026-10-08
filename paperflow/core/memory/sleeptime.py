"""Sleeptime 记忆整合后台：把对话增量沉淀进核心记忆块。

CLI REPL 每轮循环顶部调 run_once_if_due()：读取未消费历史 → LLM 用记忆编辑
工具语义输出编辑指令（append/replace）→ 全量预验证 → 经 BlockManager
应用进核心块。写入前必须过类型枚举白名单（system/ 精确枚举 profile/assistant，
顶层仅 feedback_/project_/reference_ 三前缀），动作只允许 append/replace，
因为 LLM 输出不可信；应用期连败 3 次强制推进游标，防止同一批坏编辑被无限重放。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

__all__ = ["Sleeptime", "MemoryEditBatch", "MemoryEdit",
           "MemoryEditValidationError"]

# ============================================================================
# 常量：可写记忆文件白名单（类型枚举，正则表达式）
# ============================================================================
# LLM 输出不可信，写入前必须校验目标文件是否在允许列表中。
# 允许的模式（与 _build_prompt 定向表一一对应）：
#   - system/profile.md、system/assistant.md（system/ 下精确枚举，不得新增）
#   - feedback_<topic>.md / project_<topic>.md / reference_<topic>.md
#     （类型前缀封闭，topic 由 LLM 起名但仅限字母数字下划线）
# 使用正则确保路径安全，防止目录遍历或非法后缀。
_EDIT_FILE_PATTERN = re.compile(
    r"^(system/(profile|assistant)|"
    r"feedback_[A-Za-z0-9_]+|project_[A-Za-z0-9_]+|reference_[A-Za-z0-9_]+)\.md\Z")


class MemoryEdit(BaseModel):
    """单条记忆编辑指令：目标文件 + 动作 + 内容/钩子。

    content 与 hook 都带长度上限（防 LLM 输出爆炸）；file 须命中白名单。

    Attributes:
        file: str，目标文件名（须命中类型枚举白名单：system/profile|assistant、feedback_*、project_*、reference_*）
        action: Literal["append", "replace"]，编辑动作
        content: str，写入内容（上限 8000 字符）
        hook: str，可选的钩子说明（上限 500 字符）
    """

    file: str
    action: Literal["append", "replace"]
    content: str = Field(default="", max_length=8000)
    hook: str = Field(default="", max_length=500)


class MemoryEditBatch(BaseModel):
    """一次整合会话的编辑指令集合（上限 20 条，防单轮过量写入）。

    Attributes:
        edits: list[MemoryEdit]，本轮整合的编辑指令（上限 20 条）
    """

    edits: list[MemoryEdit] = Field(default_factory=list, max_length=20)


class MemoryEditValidationError(ValueError):
    """编辑指令未通过阶段 1 校验（目标不在类型枚举白名单内）。

    与阶段 2 的应用期错误（如块写入因超限/read_only 抛的 ValueError）区分：只有
    校验错误被上抛（原子性——一条不写）；应用期错误计入连败计数，连败 3 次强制
    前进游标，避免同一批编辑被无限重放。
    """


class Sleeptime:
    """后台记忆整合器：把对话增量沉淀进核心记忆块。

    整合节奏由触发参数（frequency/min_interval_s）控制；进度由游标跟踪——
    游标值基于 messages 表行数推导（不从内存计数器恢复），因此进程重启后
    从正确位置自愈：不重复整合已处理的消息、也不遗漏新增的消息。

    Attributes:
        agent_state: AgentState，提供 agent_id 供查询消息与推导游标
        block_manager: BlockManager，编辑指令最终落到它执行
        message_manager: MessageManager，读对话消息与推导游标
        structured: StructuredOutput，LLM 抽取编辑指令的通道
        enable: bool，总开关
        frequency: int，新增消息数达到该值才触发整合
        min_interval_s: float，两次整合的最小间隔
        max_entries: int，预留的单批上限（当前未消费）
        _running: bool，本次整合是否在执行中（防并发重叠）
        _last_run: float，上次整合的单调时钟时刻
        _failures: int，连续失败计数（达阈值强制推进游标，防死循环）
        _cursor: int，已处理到的消息数游标（由 messages 表行数推导，进程重启可自愈）
    """

    def __init__(self, agent_state, block_manager, message_manager,
                 structured, enable: bool = False, frequency: int = 50,
                 min_interval_s: float = 60.0, max_entries: int = 20):
        """装配整合器依赖与触发参数。

        Args:
            agent_state: AgentState 实例（supervisor 的 agent 状态），提供 agent_id
                供按会话查询消息、推导游标。
            block_manager: BlockManager，编辑指令最终映射到它执行（block CRUD +
                MemFS markdown 投影与 git commit）。
            message_manager: MessageManager，读对话消息、推导游标（size = 该会话
                消息总数）；None 时游标恒 0、整合跳过。
            structured: StructuredOutput，LLM 抽取编辑指令的通道；LLM 输出不可信，
                指令须经校验才应用。
            enable: 总开关；False 时 run_once_if_due 恒直接返回。
            frequency: 新增消息数达到该值才触发整合（「攒够再整合」避免逐条写块把
                噪音也沉淀进核心记忆）。
            min_interval_s: 两次整合的最小间隔，防高频触发打爆 LLM 调用。
            max_entries: 预留的单批编辑上限；实际上限由 MemoryEditBatch 的
                max_length=20 约束，此参数当前未消费。
        """
        self.agent_state = agent_state
        self.block_manager = block_manager
        self.message_manager = message_manager
        self.structured = structured
        self.enable = enable
        self.frequency = frequency
        self.min_interval_s = min_interval_s
        self.max_entries = max_entries
        self._running = False
        self._last_run = time.monotonic()
        self._failures = 0
        #: 已处理到的消息数量游标（基于 messages 表行数，从 DB 推导）
        #: 语义是“下次从游标处开始整合”
        self._cursor = self.message_manager.size(agent_state.agent_id) if message_manager else 0

    async def run_once_if_due(self) -> None:
        """快速判定是否该运行整合；全部廉价检查，大多立即返回。

        任一条件不满足（未启用 / 已在跑 / 新增消息不足 frequency / 距上次
        不足 min_interval_s）都直接返回——每轮 REPL 的开销极小。

        注意：此方法为异步，但内部实际执行 _run_once 也是异步；此处只做入口。
        """
        # 未启用或已在运行（防止并发重叠）
        if not self.enable or self._running:
            return

        # 新增消息不足 frequency
        size = self.message_manager.size(self.agent_state.agent_id)
        if size - self._cursor < self.frequency:
            return

        # 距上次整合不足 min_interval_s（防止频繁调用 LLM）
        if time.monotonic() - self._last_run < self.min_interval_s:
            return

        # 通过所有检查，开始执行
        self._running = True
        try:
            await self._run_once()
        finally:
            self._running = False
            self._last_run = time.monotonic()

    async def _run_once(self) -> None:
        """读取新消息 → LLM 编辑指令 → 全量预验证 → 逐条应用 → commit → 推进。

        整个流程分为两个阶段：
            1. 全量预验证（_validate_edit）：校验文件白名单。
               若任一编辑非法，则整体抛出 MemoryEditValidationError，一条都不写。
            2. 若全部合法，则逐条应用编辑（_apply_edit）。
               应用期间若有任何异常（如块超限/read_only），计入连败计数；
               连败达 3 次则强制推进游标，避免死循环。

        游标推进时机：
            - 成功完成整合后，游标更新为当前 messages 表总行数。
            - 阶段 2 应用异常且连败累计达 3 次，则同样强制推进游标（跳过这批问题消息），
              防止同一批编辑被反复重放导致无限循环。
            - 若新消息为空（游标可能超前），则校准游标到当前总行数并返回。
        """
        # 获取该 agent 当前所有消息
        new_msgs = self.message_manager.get_messages_by_agent_id(
            self.agent_state.agent_id)

        # 从游标处切出尚未整合的新消息。
        # 游标可能超前于当前总行数（如消息被清理或上次连败强制推进），此时切片为空，
        # 需校准游标，避免下次重复计算空区间。
        new_msgs = new_msgs[self._cursor:]
        if not new_msgs:
            self._cursor = self.message_manager.size(self.agent_state.agent_id)
            return

        # 构建提示词
        prompt = self._build_prompt(new_msgs)
        try:
            # 调用 LLM 获取编辑指令批次
            batch = await self.structured.extract(
                prompt=prompt, schema=MemoryEditBatch,
                fallback=lambda: MemoryEditBatch(edits=[]))

            # 阶段 1：全量预验证——任一非法则整体失败，一条都不写。
            # 校验失败（MemoryEditValidationError）上抛给调用方，不计入连败
            for edit in batch.edits:
                self._validate_edit(edit)

            # 阶段 2：全量通过后逐条应用（映射到 block 编辑）
            for edit in batch.edits:
                self._apply_edit(edit)

            # 若 block_manager 支持 git，则提交变更
            if hasattr(self.block_manager, '_commit') and callable(self.block_manager._commit):
                self.block_manager._commit(f"sleeptime: {len(new_msgs)} 条历史")

            # 成功后推进游标到当前总行数，并重置失败计数
            self._cursor = self.message_manager.size(self.agent_state.agent_id)
            self._failures = 0

        except MemoryEditValidationError:
            # 阶段 1 校验失败：原子性失败不吞掉，直接向上抛出，由调用方处理。
            raise
        except Exception:
            # 阶段 2 应用期错误（如块超限/read_only 的 ValueError）：
            # 计入连败，3 次强制前进游标，避免同一批编辑被无限重放
            self._failures += 1
            if self._failures >= 3:
                # 防卡死：连败 3 次，强制推进游标（跳过这批消息）
                self._cursor = self.message_manager.size(self.agent_state.agent_id)

    def _build_prompt(self, new_msgs: list) -> str:
        """构造整合指令生成提示：声明可写文件、规则与定向建议。

        定向表把知识类型映射到目标文件——用户画像→system/profile.md、
        助手自我→system/assistant.md、反馈/项目/文献→三类前缀块——
        与白名单 _EDIT_FILE_PATTERN 一一对应。

        Args:
            new_msgs: 尚未整合的消息列表（已按时间升序）。

        Returns:
            构造好的提示文本（字符串）。
        """
        parts = [
            "你是 paperFlow 的记忆整合器（sleeptime）。分析以下新对话，输出记忆编辑指令。",
            "可写文件与定向规则（只能写下列文件，不得发明新文件）：",
            "- system/profile.md — 学到用户身份/研究方向/偏好/背景 → "
            "append（新增条目）或 replace（整理重写）",
            "- system/assistant.md — 助手角色/工作方式认知变化 → replace（整块重写）",
            "- feedback_<主题>.md（主题名仅限字母数字下划线，如 feedback_note_style）— 用户对做法的反馈与纠正 → "
            "append（每条一行）",
            "- project_<主题>.md — 研究项目/论文进展的关键事实 → append",
            "- reference_<主题>.md — 文献/资料可长期复用的要点 → append",
            "规则：值得长期记住才写；同主题合并重复；旧结论被推翻时 replace 而非追加矛盾条目；"
            "单批最多 20 条；每条内容一行、自带主语。",
            "", "新对话：",
        ]
        # 将每条消息的 role 和 content 以文本形式拼入
        for m in new_msgs:
            parts.append(f"[{m.role.value}] {m.content}")
        return "\n".join(parts)

    def _validate_edit(self, edit: MemoryEdit) -> None:
        """阶段 1 校验：目标必须命中类型枚举白名单。

        白名单之外的写入路径一律拒绝——LLM 输出不可信，防止它把编辑指令
        指向任意记忆文件或发明新文件；system/ 两块与顶层三类前缀是全部
        可写面。

        Args:
            edit: MemoryEdit，待校验的编辑指令。

        Raises:
            MemoryEditValidationError: 若文件不在白名单内。
        """
        if not _EDIT_FILE_PATTERN.match(edit.file):
            raise MemoryEditValidationError(f"非法编辑目标: {edit.file}")

    def _apply_edit(self, edit: MemoryEdit) -> None:
        """把编辑指令映射到 BlockManager（file → block label）。

        追加/替换的目标块不存在时创建（append/replace 都允许「写新块」的意图自动
        建块）。已有块走 mutate_block：append 在 mutator 里做「旧值 + 新内容」、
        replace 做整块替换，整段读-算-写一次持锁。

        Args:
            edit: MemoryEdit，已通过校验的编辑指令。
        """
        # label 由 file 移除 .md 后缀并去除 "system/" 前缀得到（system/ 下的块 label 即文件名）。
        label = edit.file.removesuffix(".md").replace("system/", "")

        def _mutate(v: str) -> str:
            """append 在旧值后追加一行，replace 整块替换。

            Args:
                v: str，块的当前值（旧值）

            Returns:
                应用本条编辑后的新块值（append 追加一行，replace 整块替换）。
            """
            if edit.action == "append":
                return v + "\n" + edit.content
            return edit.content

        try:
            self.block_manager.mutate_block(label, _mutate)
        except KeyError:
            # 块不存在：建新块（append/replace 对缺失块的首写内容都等于 edit.content）
            self.block_manager.create_block(label, edit.content)