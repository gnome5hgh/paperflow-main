"""MemoryConsolidator 记忆整合：把每轮对话沉淀进记忆块。

REPL 每轮对话结束后调 consolidate()：读取游标之后的新消息 + 全部可写块的当前
内容 → LLM 输出记忆编辑指令 → 全量预验证 → 经 BlockManager 应用进记忆块。喂入
旧值是「不产生矛盾条目」的前提——模型看不到旧内容就无从判断新信息推翻了哪一条。

产出是**行级**指令：新增一行 / 按行首前缀取代一行 / 删掉一行，核心块另可整块重写；
没有值得沉淀的内容就返回空批次（NOOP，什么都不写）。目标块由代码按「类型 + 当天
日期」拼装（模型只给类型，不写日期）——同一类型同一天进同一分册，写满了续号。
LLM 输出不可信，指令先过全量预验证（一条不过则整批不写）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from paperflow.core.memory.common.errors import BlockLimitExceeded

logger = logging.getLogger(__name__)

__all__ = ["MemoryConsolidator", "MemoryEditBatch", "MemoryEdit",
           "MemoryEditValidationError"]

#: 可写块的 label 形态：两个核心块精确枚举，三类前缀家族（前缀之后是日期分册，
#: 也可能是早期按主题命名的块）。用来挑出「旧值要喂进 prompt」的块。
_WRITABLE_LABELS = frozenset({"profile", "assistant"})
_WRITABLE_PREFIXES = ("feedback_", "project_", "reference_")

#: 日期分册的块类型（label = <类型>_<日期>，写满续号 <类型>_<日期>_2）。
#: 核心块不参与分册——它们靠整块重写收敛。
_PREFIX_TARGETS = ("feedback", "project", "reference")


def _today() -> str:
    """当天日期（YYYY-MM-DD）——分册键。

    由代码拼装而非让模型给：模型写错日期会让内容散进错误的分册。

    Returns:
        str，形如 "2026-10-10"。
    """
    return datetime.now().strftime("%Y-%m-%d")


def _is_writable_label(label: str | None) -> bool:
    """label 是否落在可写面上（决定哪些块的当前值要喂给整合器）。

    Args:
        label: str | None，块标签。

    Returns:
        bool，是核心块或三类前缀家族之一为 True。
    """
    if not label:
        return False
    return label in _WRITABLE_LABELS or label.startswith(_WRITABLE_PREFIXES)


class MemoryEdit(BaseModel):
    """单条记忆编辑指令：目标类型 + 动作 + 新行 / 匹配前缀。

    target 是类型枚举而非文件名——真实的块 label 由代码按「类型 + 当天日期」拼装，
    模型不碰日期。content 与 match 都带长度上限（防 LLM 输出爆炸）；语义约束
    （rewrite 只给核心块、supersede/drop 必须给 match）在 _validate_edit 里校验。

    Attributes:
        target: Literal["profile","assistant","feedback","project","reference"]，目标类型
        action: Literal["add","supersede","drop","rewrite"]，动作（rewrite 仅限核心块）
        content: str，新行内容（add/supersede）或整块新内容（rewrite），上限 8000 字符
        match: str，行首前缀；supersede/drop 据此命中旧行，上限 500 字符
    """

    target: Literal["profile", "assistant", "feedback", "project", "reference"]
    action: Literal["add", "supersede", "drop", "rewrite"]
    content: str = Field(default="", max_length=8000)
    match: str = Field(default="", max_length=500)


class MemoryEditBatch(BaseModel):
    """一次整合会话的编辑指令集合（上限 20 条，防单轮过量写入）。

    Attributes:
        edits: list[MemoryEdit]，本轮整合的编辑指令（上限 20 条）
    """

    edits: list[MemoryEdit] = Field(default_factory=list, max_length=20)


class MemoryEditValidationError(ValueError):
    """编辑指令未通过阶段 1 校验（动作与目标不匹配、缺必要字段）。

    与阶段 2 的应用期错误（match 命中不到旧行、块写入超限/read_only）区分：只有
    校验错误被上抛（原子性——一条不写）；应用期错误计入连败计数，连败 3 次强制
    前进游标，避免同一批编辑被无限重放。
    """


class MemoryConsolidator:
    """记忆整合器：把每轮对话沉淀进记忆块。

    触发是每轮一次（REPL 在本轮收尾调用）；进度由游标跟踪——游标值基于 messages
    表行数推导（不从内存计数器恢复），因此进程重启后从正确位置自愈：不重复整合
    已处理的消息、也不遗漏新增的消息。

    Attributes:
        agent_state: AgentState，提供 agent_id 供查询消息与推导游标
        block_manager: BlockManager，编辑指令最终落到它执行，也是旧记忆的来源
        message_manager: MessageManager，读对话消息与推导游标
        structured: StructuredOutput，LLM 抽取编辑指令的通道
        enable: bool，总开关
        _running: bool，本次整合是否在执行中（防并发重叠）
        _failures: int，连续失败计数（达阈值强制推进游标，防死循环）
        _cursor: int，已处理到的消息数游标（由 messages 表行数推导，进程重启可自愈）
    """

    def __init__(self, agent_state, block_manager, message_manager,
                 structured, enable: bool = False):
        """装配整合器依赖。

        Args:
            agent_state: AgentState 实例（supervisor 的 agent 状态），提供 agent_id
                供按会话查询消息、推导游标。
            block_manager: BlockManager，编辑指令最终映射到它执行（block CRUD +
                MemFS markdown 投影与 git commit），并读出全部可写块的旧值。
            message_manager: MessageManager，读对话消息、推导游标（size = 该会话
                消息总数）。
            structured: StructuredOutput，LLM 抽取编辑指令的通道；LLM 输出不可信，
                指令须经校验才应用。
            enable: 总开关；False 时 consolidate 直接返回（不调 LLM）。
        """
        self.agent_state = agent_state
        self.block_manager = block_manager
        self.message_manager = message_manager
        self.structured = structured
        self.enable = enable
        self._running = False
        self._failures = 0
        #: 已处理到的消息数量游标（基于 messages 表行数，从 DB 推导）
        #: 语义是“下次从游标处开始整合”
        self._cursor = self.message_manager.size(agent_state.agent_id) if message_manager else 0

    async def consolidate(self) -> None:
        """本轮收尾调用一次：有新消息就整合，没有就直接返回。

        三道廉价检查（未启用 / 已在跑 / 本轮无新增消息）任一成立即返回——
        空轮不调用 LLM。检查通过后执行一轮完整整合。
        """
        if not self.enable or self._running:
            return
        size = self.message_manager.size(self.agent_state.agent_id)
        if size <= self._cursor:
            return                      # 本轮没有新增消息：不调 LLM
        self._running = True
        try:
            await self._run_once()
        finally:
            self._running = False

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

            # 阶段 3：结算——条目被取代/删除后空掉的分册整块删掉，让「全量喂旧
            # 记忆」的总量有界（记忆的语义是当前有效知识，历史交给 Recall）
            self._cleanup_empty_shards()

            # 若 block_manager 支持 git，则提交变更
            if hasattr(self.block_manager, '_commit') and callable(self.block_manager._commit):
                self.block_manager._commit(f"consolidation: {len(new_msgs)} 条消息")

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

    def _existing_memory(self) -> list:
        """收集当前可写块的快照，供 prompt 呈现旧记忆。

        Returns:
            list[Block]，可写块（两个核心块 + 三类前缀家族的日期分册），按 label 排序。
        """
        blocks = [b for b in self.block_manager.list_blocks()
                  if _is_writable_label(b.label)]
        return sorted(blocks, key=lambda b: b.label or "")

    def _build_prompt(self, new_msgs: list) -> str:
        """构造整合指令生成提示：目标与动作声明 + 旧记忆 + 本轮对话。

        **旧记忆是消解矛盾的前提**：把全部可写块的当前内容摊开，模型才能看出
        新信息推翻了哪一条旧条目、哪几条是重复的。提示里的目标类型与动作说明
        与 _validate_edit 的校验面一一对应。

        Args:
            new_msgs: 本轮新增的消息列表（已按时间升序）。

        Returns:
            构造好的提示文本（字符串）。
        """
        parts = [
            "你是 paperFlow 的记忆整合器（consolidation）。分析本轮对话，输出记忆编辑指令。",
            "目标 target（只能写这些类型；块的日期分册由系统拼装，你不要写日期）：",
            "- profile — 用户身份/研究方向/偏好/背景",
            "- assistant — 助手角色/工作方式认知变化",
            "- feedback — 用户对做法的反馈与纠正",
            "- project — 研究项目/论文进展的关键事实",
            "- reference — 文献/资料可长期复用的要点",
            "动作 action：",
            "- add — 新增一行，content 给这一行的文字",
            "- supersede — match 给旧行开头的一段文字，把命中的旧行换成 content（新的一行）",
            "- drop — match 给旧行开头的一段文字，删掉命中的行（结论作废）",
            "- rewrite — 整块重写，content 给完整新内容；只允许 target=profile / assistant",
            "规则：值得长期记住才写；**没有值得沉淀的内容就返回空 edits（不写任何块，这是正常的）**；"
            "旧结论被推翻时用 supersede 换掉旧行，不要写出一条互相矛盾的新条目；"
            "supersede/drop 的 match 必须能在「已有记忆」里逐字找到行首；"
            "单批最多 20 条；每条内容一行、自带主语，不写日期（日期由系统加）。",
            "", "已有记忆（这些块的当前内容——判断重复与矛盾必须对照它们）：",
        ]
        existing = self._existing_memory()
        if not existing:
            parts.append("（暂无）")
        for b in existing:
            desc = f" — {b.description}" if b.description else ""
            parts.append(f"## {b.label}{desc}（{len(b.value)} 字）")
            parts.append(b.value.strip() or "（空）")
        parts.append("")
        parts.append("本轮对话：")
        # 将每条消息的 role 和 content 以文本形式拼入
        for m in new_msgs:
            parts.append(f"[{m.role.value}] {m.content}")
        return "\n".join(parts)

    def _validate_edit(self, edit: MemoryEdit) -> None:
        """阶段 1 校验：动作与目标是否匹配、必要字段是否齐全。

        LLM 输出不可信，写入前必须把语义约束查一遍：整块重写只允许两个核心块
        （前缀分册是条目流，整块重写会抹掉分册里的其他条目）；supersede/drop
        必须给 match，否则成了没有对象的写入；add/supersede/rewrite 必须给
        content。target 与 action 的取值面由 pydantic 的 Literal 在构造期锁定。

        Args:
            edit: MemoryEdit，待校验的编辑指令。

        Raises:
            MemoryEditValidationError: 动作与目标不匹配、或必要字段为空。
        """
        if edit.action == "rewrite" and edit.target in _PREFIX_TARGETS:
            raise MemoryEditValidationError(
                f"整块重写只允许 profile/assistant，不能用于 {edit.target}")
        if edit.action in ("supersede", "drop") and not edit.match.strip():
            raise MemoryEditValidationError(f"{edit.action} 需要 match（行首前缀）")
        if edit.action != "drop" and not edit.content.strip():
            raise MemoryEditValidationError(f"{edit.action} 需要 content")

    def _label_for(self, target: str) -> str:
        """目标类型 → 块 label（前缀家族按当天日期分册，核心块用原 label）。

        Args:
            target: str，目标类型（profile/assistant/feedback/project/reference）。

        Returns:
            str，块 label，形如 "feedback_2026-10-10" 或 "profile"。
        """
        if target in _PREFIX_TARGETS:
            return f"{target}_{_today()}"
        return target

    def _apply_edit(self, edit: MemoryEdit) -> None:
        """把行级编辑落到目标块（前缀家族按当天日期分册，写满自动续号）。

        add 追加一行；supersede 删掉 match 命中的旧行再追加新行；drop 只删；
        rewrite 整块替换（仅核心块）。行级操作都在 mutate_block 里完成——整段
        读-算-写在一次持锁内，并发写不会互相抹掉。

        Args:
            edit: MemoryEdit，已通过校验的编辑指令。

        Raises:
            ValueError: supersede/drop 的 match 在任何分册里都找不到对应行。
        """
        if edit.action == "rewrite":
            try:
                self.block_manager.mutate_block(edit.target, lambda v: edit.content)
            except KeyError:
                self.block_manager.create_block(edit.target, edit.content)
            return
        if edit.action == "add":
            self._add_line(edit.target, edit.content.strip())
            return
        self._replace_or_drop(edit)

    def _add_line(self, target: str, line: str) -> None:
        """把一行加进该类型当天的分册；写满则续号到下一册（确定性，不丢数据）。

        块不存在时直接建块写入，已存在则原子追加；撞上字符上限就换下一册再试。
        核心块不参与分册——写满如实上抛，由整块重写收敛。

        Args:
            target: str，目标类型。
            line: str，已去空白的条目行。

        Raises:
            BlockLimitExceeded: 非分册目标（profile/assistant）写满。
        """
        base = self._label_for(target)
        shardable = target in _PREFIX_TARGETS
        n = 1
        while True:
            label = base if n == 1 else f"{base}_{n}"
            if self.block_manager.get_block_by_label(label) is None:
                self.block_manager.create_block(label, line)
                return
            try:
                self.block_manager.mutate_block(
                    label, lambda v: f"{v.strip()}\n{line}" if v.strip() else line)
                return
            except BlockLimitExceeded:
                if not shardable:
                    raise
                n += 1          # 这一册写满：续号换下一册

    def _replace_or_drop(self, edit: MemoryEdit) -> None:
        """supersede/drop：在该类型的分册家族里找到 match 命中的行并处理。

        同一类型的条目可能因分册溢出散在多册里，所以逐册找；一册都没命中就如实
        报错，绝不静默降级成 add（那会堆出互相矛盾的两条）。

        Args:
            edit: MemoryEdit，action 为 supersede / drop。

        Raises:
            ValueError: 任何分册里都没有以 match 开头的行。
        """
        prefix = edit.match.strip()
        for label in self._existing_shard_labels(edit.target):
            # mutate_block 的 mutator 返回 None 表示「判定不改」（这一册没命中）
            if self.block_manager.mutate_block(label, self._line_mutator(edit)) is not None:
                return
        raise ValueError(f"match not found in {edit.target}: {prefix}")

    def _line_mutator(self, edit: MemoryEdit):
        """构造 supersede/drop 的 mutator（删掉 match 命中的行，supersede 再补新行）。

        Args:
            edit: MemoryEdit，action 为 supersede / drop。

        Returns:
            Callable[[str], str | None]，返回 None 表示该块没有命中行（调用方据此换下一册）。
        """
        prefix = edit.match.strip()

        def _mutate(v: str) -> str | None:
            lines = [ln for ln in v.splitlines() if ln.strip()]
            kept = [ln for ln in lines if not ln.startswith(prefix)]
            if len(kept) == len(lines):
                return None
            if edit.action == "supersede":
                kept.append(edit.content.strip())
            return "\n".join(kept)

        return _mutate

    def _existing_shard_labels(self, target: str) -> list[str]:
        """该类型当前**已存在**的分册 label（主册在前，续号按数字升序）。

        Args:
            target: str，目标类型。

        Returns:
            list[str]，label 列表；核心块返回自身（不存在则为空）。
        """
        base = self._label_for(target)
        if target not in _PREFIX_TARGETS:
            return [base] if self.block_manager.get_block_by_label(base) else []
        found = [b.label for b in self.block_manager.list_blocks()
                 if b.label and (b.label == base or b.label.startswith(base + "_"))]

        def _key(label: str) -> int:
            """分册排序键：主册 0，续号取其数字。"""
            suffix = label[len(base):]          # "" 或 "_2"
            return int(suffix[1:]) if suffix else 0

        return sorted(found, key=_key)

    def _cleanup_empty_shards(self) -> list[str]:
        """删掉已经空掉的条目分册（取代/删除留下的空壳）。

        记忆的语义是「当前有效的知识」：条目被取代或删除后，承载它的分册若已空就
        整块删掉——否则分册只增不减，逐轮把全部旧记忆喂进 prompt 会越来越肥。
        核心块不在此列（它们常驻，靠整块重写收敛）。

        Returns:
            list[str]，被删除的块 label（无删除时为空）。
        """
        removed: list[str] = []
        for block in self.block_manager.list_blocks():
            label = block.label or ""
            if label.startswith(_WRITABLE_PREFIXES) and not block.value.strip():
                self.block_manager.delete_block(block.id)
                removed.append(label)
        return removed