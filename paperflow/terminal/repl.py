# paperflow/terminal/repl.py
"""REPL 交互半区：主循环 + 确认/提问回调 + 横幅渲染。

从 cli.py 迁入（行为保持重构）：cli.py 保留装配组合根（main），本模块承载
「读输入 → 驱动 supervisor → 渲染输出」的每轮交互。与 io.py/render.py 同层
——依赖 core 的类型（Agent/ConversationState）但不组装对象图，对象图仍由
cli.main 装配后注入；不进包级 __init__ 导出（避免包级导入拖入 core 依赖链）。

每轮:读 stdin → 合并挂起的澄清(若有)→ supervisor.run(query, force_dispatch) →
若产生澄清问题且未超轮 → 挂起打印问题;否则打印结果。
"""
import asyncio
import logging
import signal
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent, MaxTurnsExceeded
from paperflow.core.intent.conversation_state import (
    ConversationState, PendingClarification)
from paperflow.terminal.diff import compute_diff, truncate_diff
from paperflow.terminal.io import InputIO
from paperflow.terminal.render import StreamRenderer

logger = logging.getLogger(__name__)


def _make_print_fn(console):
    """
    根据终端类型构造打印函数。

    Args:
        console: rich.Console 实例，若为 None（非 TTY）则使用内置 print。

    Returns:
        打印函数，接受 *args, style=None, end="\n", flush=False 等参数。
            若 console 非 None，使用 console.print 并支持 style（如 dim 样式）；
            若 console 为 None，使用内置 print 并忽略 style（不产生 ANSI 转义）。
    """
    if console is None:
        def _plain(*args, style=None, **kwargs):
            print(*args, **kwargs)
        return _plain

    def _rich(*args, style=None, end="\n", flush=False):
        # overflow="fold"：长行自动换行而非截断
        console.print(*args, style=style, end=end, overflow="fold")
    return _rich


def _confirm_diff_preview(tool_name: str, params: dict) -> str | None:
    """
    为写/编辑工具生成确认前的 diff 预览文本。

    Args:
        tool_name: 工具名称（"write_file" 或 "edit_file"）。
        params: 工具参数字典，需包含 "path"，write_file 还需 "content"，
                edit_file 还需 "old_text" 和 "new_text"。

    Returns:
        str | None: 若预览可用则返回截断后的 unified diff 字符串；
                    若工具不是写/编辑、参数缺失、文件读取失败或编辑替换条件不满足，
                    则返回 None（表示走纯确认，无预览）。

    关键边界条件（edit_file）：
        - edit_file 工具仅在 old_text 在文件中恰好出现一次时才执行替换。
          若 count != 1，则实际不会写入，此时预览与当前内容无异，反而干扰用户，
          因此直接返回 None，只走纯确认。
        - 若文件不存在，old 为空字符串，count=0，亦返回 None。
    """
    if tool_name not in {"write_file", "edit_file"}:
        return None
    path = params.get("path") if isinstance(params, dict) else None
    if not path:
        return None
    p = Path(path)
    try:
        old = p.read_text(encoding="utf-8") if p.exists() else ""
    except (OSError, UnicodeDecodeError):
        return None
    if tool_name == "write_file":
        new = params.get("content", "")
    else:  # edit_file
        old_text, new_text = params.get("old_text"), params.get("new_text")
        if old_text is None or new_text is None:
            return None
        # 仅在替换确实会应用时预览：要求 old_text 在文件中恰好出现一次
        if old.count(old_text) != 1:
            return None
        new = old.replace(old_text, new_text)
    return truncate_diff(compute_diff(old, new, fromfile=str(p), tofile=str(p)))


def _make_confirm_callback(io: InputIO, renderer: StreamRenderer):
    """
    构造异步确认回调函数，供 Agent 执行器在工具执行前调用。

    Args:
        io: 输入适配器（用于读取用户确认）。
        renderer: 渲染器（用于显示 diff 预览和暂停 live）。

    Returns:
        async callable: 接收一个 ConfirmRequest 对象，返回 bool（True 表示确认继续）。

    行为：
        1. 若工具是写/编辑，先计算 diff 预览并通过 renderer.print_diff 显示。
        2. 调用 renderer.suspend() 停止实时渲染（避免与 prompt_toolkit 提示框冲突）。
        3. 通过 asyncio.to_thread 在单独线程中执行 io.confirm（避免阻塞事件循环）。
        4. 捕获 EOFError/KeyboardInterrupt 返回 False（保守拒绝）。
    """
    async def _confirm(cr) -> bool:
        preview = _confirm_diff_preview(cr.tool_name, getattr(cr, "params", None))
        if preview:
            renderer.print_diff(preview)
        # 确认框前无条件停 live（spinner/残留内容块）：rich Live 与 prompt_toolkit
        # 提示框并发会互相干扰（方向键不响应）。print_diff 已停一次，这里兜底。
        renderer.suspend()
        try:
            return await asyncio.to_thread(
                io.confirm, f"[Confirm] {cr.tool_name}?")
        except (EOFError, KeyboardInterrupt):
            return False
    return _confirm


def _make_ask_callback(io: InputIO, renderer: StreamRenderer):
    """
    构造 ask_user 回调：读取开放问题的答案。

    Args:
        io: 输入适配器。
        renderer: 渲染器（用于暂停 live）。

    Returns:
        callable: 接受 question 字符串，返回答案字符串。
                  遇到 EOF/Ctrl+C 返回空串（fail-safe）。

    行为：
        - 先 suspend 停止 live 渲染，避免与输入框冲突。
        - 调用 io.ask，捕获异常返回空串。
    """
    def _ask(question: str) -> str:
        renderer.suspend()
        try:
            return io.ask(question)
        except (EOFError, KeyboardInterrupt):
            return ""
    return _ask


def _merge_pending(conversation: ConversationState, raw: str) -> tuple[str, bool]:
    """
    合并跨轮澄清输入，返回 (query, force_dispatch)。

    当上一轮产生了澄清问题且未超轮（round < 2），本轮输入视为对澄清的回答，
    将其与原始查询拼接作为新查询，并设置 force_dispatch=False 以便重新运行 intent 管线。
    若澄清轮数已达上限（round >= 2），则强制调度（force_dispatch=True），
    使用原始查询（不含用户澄清）直接进入 ReAct 循环，避免无限澄清循环。

    Args:
        conversation: 会话状态（包含 pending_intent）。
        raw: 当前轮的用户输入。

    Returns:
        (query: str, force_dispatch: bool)
            - query: 实际用于 supervisor.run 的查询文本。
            - force_dispatch: 若为 True，则跳过意图识别，直接执行 ReAct 循环。
    """
    p = conversation.pending_intent
    if p is None:
        return raw, False
    if p.round >= 2:
        # 超轮：强制调度，清除 pending 状态
        conversation.pending_intent = None
        return p.original_input, True
    # 未超轮：合并澄清内容，清除 pending
    conversation.pending_intent = None
    return f"{p.original_input}（用户澄清：{raw}）", False


def _shorten_path(p: str) -> str:
    """将路径中的 home 目录缩写为 '~'，用于 banner 显示。"""
    home = str(Path.home())
    return "~" + p[len(home):] if str(p).startswith(home) else str(p)


def _render_banner(model: str, workspace: str) -> str:
    """
    生成启动横幅，使用 box-drawing 字符绘制方框。

    Args:
        model: 模型名称（如 "gpt-4"）。
        workspace: 工作区路径（已缩写）。

    Returns:
        str: 多行横幅字符串，包含标题、模型和工作区信息。
    """
    lines = [
        ">_ paperFlow Academic Assistant",
        "",
        f"model:     {model}",
        f"workspace: {workspace}",
    ]
    inner = max(len(l) for l in lines)
    top = "╭" + "─" * (inner + 2) + "╮"
    body = "\n".join(f"│ {l:<{inner}} │" for l in lines)
    bottom = "╰" + "─" * (inner + 2) + "╯"
    return f"{top}\n{body}\n{bottom}"


async def _repl(supervisor: Agent, conversation: ConversationState, *,
                io: InputIO, renderer: StreamRenderer, sleeptime=None,
                config: PaperFlowConfig | None = None) -> None:
    """
    REPL 主循环。

    每轮：
        1. 触发后台记忆整合（Sleeptime）。
        2. 读取用户输入（通过 io.read，工作线程）。
        3. 若有挂起的澄清，合并查询（_merge_pending）。
        4. 重置渲染器（renderer.reset），注册 SIGINT 处理器以取消运行中的任务。
        5. 异步执行 supervisor.run(query, force_dispatch)。
        6. 根据结果：
            - 若任务被取消（Ctrl+C）：打印 "Cancelled"，继续循环。
            - 若超轮：提示并继续。
            - 若产生澄清且未强制：挂起澄清（pending_intent），打印问题，继续下一轮。
            - 否则：结束渲染（finalize），根据 should_print 决定是否打印最终答案。

    Ctrl+C 三态处理：
        - 输入框为空时：io.read 抛出 KeyboardInterrupt，退出 REPL。
        - 输入框有内容时：PromptToolkitIO 键绑定清空输入框，不退出。
        - Agent 运行时：SIGINT 处理器取消当前 run_task，捕获 CancelledError 后继续。

    Args:
        supervisor: 主 Agent 实例。
        conversation: 跨轮状态。
        io: 输入适配器。
        renderer: 渲染器。
        sleeptime: 后台记忆整合调度器（可选）。
        config: 配置（仅用于横幅，若为 None 则从环境加载）。
    """
    cfg = config or PaperFlowConfig.from_env()
    renderer.print(_render_banner(cfg.llm.model, _shorten_path(cfg.workspace)))
    renderer.print("\n  Tip: Type a research task to begin, or /exit to quit")
    supervisor.stream_callback = renderer.on_event
    loop = asyncio.get_running_loop()
    can_sigint = (hasattr(loop, "add_signal_handler")
                  and hasattr(loop, "remove_signal_handler"))
    run_task = None
    read_failures = 0

    def _cancel_run():
        if run_task is not None and not run_task.done():
            run_task.cancel()

    while True:
        # 每轮循环顶部触发后台记忆整合——放在读 stdin 之前，让用户思考期间累积的
        # 对话被整合，整合不阻塞本轮输入。
        if sleeptime is not None:
            try:
                await sleeptime.run_once_if_due()
            except Exception:  # Sleeptime 失败不打断 REPL
                logger.warning("sleeptime tick failed", exc_info=True)
        try:
            # io.read 必须经 to_thread 在 worker 线程执行：PromptToolkitIO.read 内部
            # session.prompt() 会自建事件循环（asyncio.run），而 _repl 跑在主事件循环
            # 线程——直接同步调用会抛 "asyncio.run() cannot be called from a running
            # event loop"。confirm/ask 回调已是 to_thread，read 对齐之。
            raw = await asyncio.to_thread(io.read, "> ")
        except (EOFError, KeyboardInterrupt):
            break                # Ctrl-D / 空框 Ctrl+C：与 /exit 同效，优雅退出
        except Exception as e:
            # 输入适配器故障不杀 REPL：打印后继续；但连续失败说明故障是持久的，
            # 无限刷错误比退出更糟——3 次后放弃。
            read_failures += 1
            renderer.print(f"Input error: {e}")
            if read_failures >= 3:
                renderer.print("Input failing repeatedly. Exiting.")
                break
            continue
        read_failures = 0
        if raw.strip() == "/exit":
            break
        if not raw.strip():
            # 纯空白输入（真实使用测试 P3-2）：直接忽略，不进意图管线——
            # 否则一次完整 LLM 调用后才被兜底拒绝，白烧 token。轻提示一次，
            # 避免用户以为卡死。
            renderer.print("（空输入已忽略）", style="dim")
            continue
        p = conversation.pending_intent
        query, force = _merge_pending(conversation, raw)
        renderer.reset()                    # 每轮清残留：异常/澄清路径不消费 should_print
        # 先注册 SIGINT handler 再 create_task：注册与建任务之间的同步间隙若落一个
        # SIGINT，默认 handler 会在主线程抛 KeyboardInterrupt 崩 REPL。handler 已就位
        # 则 _cancel_run 吞掉它（run_task 尚未赋值 → no-op，不崩）。
        run_task = None
        if can_sigint:
            try:
                loop.add_signal_handler(signal.SIGINT, _cancel_run)
            except (NotImplementedError, RuntimeError):
                # 信号注册失败（如非主线程/平台不支持）→ 降级为默认 Ctrl+C，不崩 REPL
                can_sigint = False
        run_task = asyncio.create_task(supervisor.run(query, force_dispatch=force))
        try:
            result = await run_task
        except asyncio.CancelledError:
            # Ctrl+C 优雅中断：渲染器过滤孤儿事件（to_thread 无法真正取消）、打印
            # 提示、回到输入框。安全阀语义与 MaxTurnsExceeded 一致——不杀 REPL。
            renderer.interrupt()
            renderer.print("Cancelled")
            continue
        except MaxTurnsExceeded:
            renderer.print("Task exceeded max turns. Please rephrase and retry.")
            continue
        except Exception as e:
            renderer.print(f"Error: {e}")
            continue
        finally:
            if can_sigint:
                try:
                    loop.remove_signal_handler(signal.SIGINT)
                except (NotImplementedError, RuntimeError):
                    pass
        intent = supervisor.last_intent
        if intent is not None and intent.clarification and not force:
            # 未超轮：挂起澄清，round 链式累计（REPL 重建时用 p.round，不重置为 0）
            prev_round = p.round if p is not None else 0
            conversation.pending_intent = PendingClarification(
                question=intent.clarification, original_input=query,
                round=prev_round + 1)
            renderer.print(intent.clarification)
            continue
        renderer.finalize()
        renderer.print(renderer.should_print(result))
