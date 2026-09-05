"""Sleeptime 记忆整合后台：把对话增量沉淀进核心记忆块。

CLI REPL 每轮循环顶部调 run_once_if_due()：读取未消费历史 → LLM 用记忆编辑
工具语义输出编辑指令（append/replace/delete）→ 全量预验证 → 经 BlockManager
应用进核心块。写入前必须过白名单并禁止删除 system/ 块，因为 LLM 输出不可信；
应用期连败 3 次强制推进游标，防止同一批坏编辑被无限重放。
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
# 常量：可写记忆文件白名单（正则表达式）
# ============================================================================
# LLM 输出不可信，写入前必须校验目标文件是否在允许列表中。
# 允许的模式：
#   - system/<name>.md，其中 name 仅含字母数字下划线（即 system/persona.md, system/human.md）
#   - <name>.md（顶层块，如 feedback_*, project_*, reference_*, 或通用块）
# 使用正则确保路径安全，防止目录遍历或非法后缀。
_EDIT_FILE_PATTERN = re.compile(
    r"^(system/[A-Za-z0-9_]+|[A-Za-z0-9_]+|feedback_[A-Za-z0-9_]+|"
    r"project_[A-Za-z0-9_]+|reference_[A-Za-z0-9_]+)\.md\Z")


class MemoryEdit(BaseModel):
    """单条记忆编辑指令：目标文件 + 动作 + 内容/钩子。

    content 与 hook 都带长度上限（防 LLM 输出爆炸）；file 须命中白名单。
    """

    file: str
    action: Literal["append", "replace", "delete"]
    content: str = Field(default="", max_length=8000)
    hook: str = Field(default="", max_length=500)


class MemoryEditBatch(BaseModel):
    """一次整合会话的编辑指令集合（上限 20 条，防单轮过量写入）。"""

    edits: list[MemoryEdit] = Field(default_factory=list, max_length=20)


class MemoryEditValidationError(ValueError):
    """编辑指令未通过阶段 1 校验（目标不在白名单 / 删除受保护 system/ 块）。

    与阶段 2 的应用期错误（如 update_block_value 因块超限/read_only 抛的
    ValueError）区分：只有校验错误被上抛（原子性——一条不写）；应用期错误
    计入连败计数，连败 3 次强制前进游标，避免同一批编辑被无限重放。
    """


class Sleeptime:
    """后台记忆整合器：把对话增量沉淀进核心记忆块。

    整合节奏由触发参数（frequency/min_interval_s）控制；进度由游标跟踪——
    游标值基于 messages 表行数推导（不从内存计数器恢复），因此进程重启后
    从正确位置自愈：不重复整合已处理的消息、也不遗漏新增的消息。
    """

    def __init__(self, agent_state, block_manager, passage_manager, message_manager,
                 structured, enable: bool = False, frequency: int = 50,
                 min_interval_s: float = 60.0, max_entries: int = 20):
        """装配整合器依赖与触发参数。

        :param agent_state: AgentState 实例（supervisor 的 agent 状态），
            提供 agent_id 供按会话查询消息、推导游标
        :param block_manager: BlockManager，编辑指令最终映射到它执行
            （block CRUD + MemFS markdown 投影与 git commit）
        :param passage_manager: PassageManager，预留的长期记忆句柄，
            当前整合器不直接消费（保持装配接口兼容）
        :param message_manager: MessageManager，读对话消息、推导游标
            （size = 该会话消息总数）；None 时游标恒 0、整合跳过
        :param structured: StructuredOutput，LLM 抽取编辑指令的通道；
            LLM 输出不可信，指令须经校验才应用
        :param enable: 总开关；False 时 run_once_if_due 恒直接返回
        :param frequency: 新增消息数达到该值才触发整合（默认 50）——
            「攒够再整合」避免逐条写块把噪音也沉淀进核心记忆
        :param min_interval_s: 两次整合的最小间隔，防高频触发打爆 LLM 调用
        :param max_entries: 预留的单批编辑上限；实际上限由
            MemoryEditBatch 的 max_length=20 约束，此参数当前未消费
        """
        self.agent_state = agent_state
        self.block_manager = block_manager
        self.passage_manager = passage_manager
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
            1. 全量预验证（_validate_edit）：校验文件白名单和删除保护。
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
        """构造整合指令生成提示：声明可编辑文件、规则与定向建议。

        定向建议是让 LLM 把「用户身份/偏好」写到 system/human.md、把「助手
        角色认知变化」写到 system/persona.md——与两个默认块的定位一致。

        Args:
            new_msgs: 尚未整合的消息列表（已按时间升序）。

        Returns:
            构造好的提示文本（字符串）。
        """
        parts = [
            "你是 paperFlow 的记忆整合器（sleeptime）。分析以下新对话，输出记忆编辑指令。",
            "可编辑：feedback_*.md / project_*.md / reference_*.md / system/*.md",
            "规则：值得长期记住才写；合并重复；旧条目被推翻时 replace 为新结论。",
            "定向：从对话学到用户身份/偏好/背景 → append/replace system/human.md；",
            "助手角色或工作方式认知变化 → replace system/persona.md",
            "", "新对话：",
        ]
        # 将每条消息的 role 和 content 以文本形式拼入
        for m in new_msgs:
            parts.append(f"[{m.role.value}] {m.content}")
        return "\n".join(parts)

    def _validate_edit(self, edit: MemoryEdit) -> None:
        """阶段 1 校验：目标必须命中白名单，且禁止删除 system/ 块。

        白名单之外的写入路径一律拒绝——LLM 输出不可信，防止它把编辑指令
        指向任意记忆文件。system/ 块（persona/human）是身份与画像，删了
        记忆系统呈空壳，任何情况下都不可删除。

        Args:
            edit: MemoryEdit，待校验的编辑指令。

        Raises:
            MemoryEditValidationError: 若文件不在白名单内或试图删除 system/ 块。
        """
        if not _EDIT_FILE_PATTERN.match(edit.file):
            raise MemoryEditValidationError(f"非法编辑目标: {edit.file}")
        if edit.action == "delete" and edit.file.startswith("system/"):
            raise MemoryEditValidationError("不允许删除 system/ 块")

    def _apply_edit(self, edit: MemoryEdit) -> None:
        """把编辑指令映射到 BlockManager（file → block label）。

        追加/替换的目标块不存在时创建（append/replace 都允许「写新块」的
        意图自动建块）；删除只作用于已存在块，缺失时静默跳过。

        Args:
            edit: MemoryEdit，已通过校验的编辑指令。
        """
        # label 由 file 移除 .md 后缀并去除 "system/" 前缀得到（system/ 下的块 label 即文件名）。
        label = edit.file.removesuffix(".md").replace("system/", "")

        # 处理 delete 动作
        if edit.action == "delete":
            b = self.block_manager.get_block_by_label(label)
            if b is not None:
                self.block_manager.delete_block(b.id)
            return

        # 处理 append / replace 动作
        b = self.block_manager.get_block_by_label(label)

        if edit.action == "append":
            if b is None:
                # 若块不存在，则创建新块
                self.block_manager.create_block(label, edit.content)
            else:
                # 否则追加内容（换行分隔）
                self.block_manager.update_block_value(
                    label, b.value + "\n" + edit.content)

        elif edit.action == "replace":
            if b is None:
                self.block_manager.create_block(label, edit.content)
            else:
                self.block_manager.update_block_value(label, edit.content)