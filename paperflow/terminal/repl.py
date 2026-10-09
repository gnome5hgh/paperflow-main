# paperflow/terminal/repl.py
"""REPL 交互半区：主循环 + 确认/提问回调 + 横幅渲染。

从 cli.py 迁入（行为保持重构）：cli.py 保留装配组合根（main），本模块承载
「读输入 → 驱动 supervisor → 渲染输出」的每轮交互。与 io.py/render.py 同层
——依赖 core 的类型（如 Agent）但不组装对象图，对象图仍由
cli.main 装配后注入；不进包级 __init__ 导出（避免包级导入拖入 core 依赖链）。

每轮:读 stdin → 斜杠命令分发（注册表命中则就地处理）→ supervisor.run(query)
→ 打印结果。澄清由 runtime 在本轮内同步问用户，REPL 不再持有跨轮澄清状态。

嵌套关系：
进程
└── _repl 主循环                      ← 每轮：sleeptime tick → 读输入 → 起 run_task → 渲染
     └── supervisor.run("用户输入")     ← 任务级：ReAct 循环（turn 0..max_turns）
          └── turn: LLM → 工具们
               └── spawn 工具 → child.run("子任务")   ← 嵌套的 run（子 agent）
REPL 只有一个，run 可以嵌套很多层。
子 agent 从不经过 REPL——spawn 直接 await child.run(...)，任务完成即返回，
所以子 agent 没有“输入循环”，也不需要终端。这也印证了 REPL 是唯一的会话入口。
"""
import asyncio
import logging
import signal
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent, MaxTurnsExceeded
from paperflow.terminal.commands import (
    CommandContext, CommandRegistry, build_default_registry)
from paperflow.terminal.diff import compute_diff, truncate_diff
from paperflow.terminal.errors import translate_error
from paperflow.terminal.io import InputIO
from paperflow.terminal.render import StreamRenderer
from paperflow.terminal.resume import ResumeReplay, render_resume_replay

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
            """非 TTY 打印：走内置 print（忽略 style，不产生 ANSI 转义）。

            Args:
                args: 任意位置参数，原样转给 print
                style: str | None，rich 样式名（此处忽略）
                kwargs: dict，其余 print 关键字参数（end/flush 等）
            """
            print(*args, **kwargs)
        return _plain

    def _rich(*args, style=None, end="\n", flush=False):
        """TTY 打印：走 rich console.print，支持样式与长行折行。

        Args:
            args: 任意位置参数，原样转给 console.print
            style: str | None，rich 样式名
            end: str，行尾字符
            flush: bool，是否立即刷新（rich 由 console 管理）
        """
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


def _make_confirm_callback(io: InputIO, renderer: StreamRenderer, center=None):
    """
    构造异步确认回调函数，供 Agent 执行器在工具执行前调用。

    Args:
        io: 输入适配器（保留参数供无 center 时兜底）。
        renderer: 渲染器（用于显示 diff 预览和暂停 live）。
        center: ConfirmCenter（确认中心，单一消费者）。None 时创建独立实例
                （仅测试/无 REPL 装配场景）。

    Returns:
        async callable: 接收一个 ConfirmRequired，返回 bool（True 表示确认继续）。

    行为：
        1. 经确认中心排队（跨线程桥接到主循环唯一消费者），弹框期间渲染抑制，
           其他 agent 的事件不会盖掉确认框。
        2. 三态决策：y=本次放行；a=本会话同 (工具,路径) 放行（pre-confirm 进
           PolicyEngine 已确认集合，agent 后续的 cr.confirm() 重复加键无害）；
           n=拒绝。
        3. 看门狗超时自动拒绝（fail-safe）。
    """
    from paperflow.terminal.confirm_center import ConfirmCenter
    center = center or ConfirmCenter(io, renderer)

    async def _confirm(cr) -> bool:
        """确认回调：把三态选择折叠为放行/拒绝，并把会话级授权记入已确认集合。

        Args:
            cr: ConfirmRequired，待确认的工具调用

        Returns:
            True 表示放行；EOF/Ctrl+C 与拒绝都返回 False。
        """
        try:
            choice = await center.confirm(cr)
        except (EOFError, KeyboardInterrupt):
            # deny 语义：确认框内 EOF/Ctrl+C = 拒绝，与 fail-safe 同效
            return False
        if choice == "a":
            # 会话级授权：提前把 (tool, path) 记入已确认集合——同一文件本会话内
            # 后续写/编辑不再询问（批准仅本会话有效）
            cr.confirm()
            return True
        return choice == "y"
    return _confirm


def _shorten_path(p: str) -> str:
    """将路径中的 home 目录缩写为 '~'，用于 banner 显示。

    Args:
        p: str，绝对路径

    Returns:
        home 目录前缀替换为 "~" 的路径（用于横幅显示）。
    """
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


async def _repl(supervisor: Agent, *,
                io: InputIO, renderer: StreamRenderer, sleeptime=None,
                config: PaperFlowConfig | None = None,
                resume_hint: str | None = None, confirm_center=None,
                resume_replay: ResumeReplay | None = None,
                mcp_manager=None,
                registry: CommandRegistry | None = None) -> None:
    """
    REPL 主循环。

    每轮：
        1. 触发后台记忆整合（Sleeptime）。
        2. 读取用户输入（通过 io.read，工作线程）。
        4. 重置渲染器（renderer.reset），注册 SIGINT 处理器以取消运行中的任务。
        5. 异步执行 supervisor.run(query)。
        6. 根据结果：
            - 若任务被取消（Ctrl+C）：打印 "Cancelled"，继续循环。
            - 若超轮：提示并继续。
            - 否则：结束渲染（finalize），根据 should_print 决定是否打印最终答案。

    Ctrl+C 三态处理：
        - 输入框为空时：io.read 抛出 KeyboardInterrupt，退出 REPL。
        - 输入框有内容时：PromptToolkitIO 键绑定清空输入框，不退出。
        - Agent 运行时：SIGINT 处理器取消当前 run_task，捕获 CancelledError 后继续。

    Args:
        supervisor: 主 Agent 实例。
        io: 输入适配器。
        renderer: 渲染器。
        sleeptime: 后台记忆整合调度器（可选）。
        config: 配置（仅用于横幅，若为 None 则从环境加载）。
        resume_hint: 无参启动检测到历史会话时的 dim 提示（可选）。
        confirm_center: 确认/提问的唯一消费者（可选，未注入则自建）。
        resume_replay: --resume 的历史回放载荷（可选）。由 cli 层从同一 in-context
            窗口构建，此处只在横幅之后、首次读输入之前渲染进滚动区——顺序错了
            历史会跑到横幅上方。回放是只读的（见 terminal/resume.py）。
        registry: 斜杠命令注册表（可选，None 时用 io/renderer/mcp_manager 自建默认表）。
    """
    # 开场输出一律先于读输入：横幅、Tip、以及在 Tip 之下择一出现的 hint / 回放。
    # 三者必须按此序打——回放若在横幅之前渲染，历史会印到横幅上方，用户上翻看到的
    # 顺序即颠倒（故回放数据由 cli 传入、在此处渲染，而不是在装配层直接打印）。
    cfg = config or PaperFlowConfig.from_env()
    renderer.print(_render_banner(cfg.llm.model, _shorten_path(cfg.runtime.workspace)))
    renderer.print("\n  Tip: Type a research task to begin, or /help for commands")
    if resume_hint:
        renderer.print(f"  {resume_hint}", style="dim")
    if resume_replay is not None:
        render_resume_replay(renderer, resume_replay)
    # 挂上回调之后 run() 才走 chat_stream（流式）；不挂则回落 chat() 一次性返回。
    # 这是两条渲染路径的总开关，必须在第一次 supervisor.run 之前完成。
    supervisor.stream_callback = renderer.on_event
    # SIGINT 可编程接管是平台能力，不是必然，且这一层探测并不足以判定：
    # asyncio.BaseEventLoop 恒定义了 add_signal_handler（未实现时抛 NotImplementedError），
    # 所以 hasattr 在 Windows 上同样为 True。这里的 hasattr 只筛掉不继承该方法的
    # 非常规事件循环；真正兜住“不支持”的是下方注册处的 try/except NotImplementedError。
    # 两者都不成立时才把 can_sigint 置 False，降级为默认 Ctrl+C（Ctrl+C 三态仍有效）。
    loop = asyncio.get_running_loop()
    can_sigint = (hasattr(loop, "add_signal_handler")
                  and hasattr(loop, "remove_signal_handler"))
    # run_task 提前绑定为 None：_cancel_run 闭包在任务创建前就可能被 SIGINT 调到，
    # 届时必须有已定义的名字可读（None → no-op，而非 NameError 崩掉）。
    run_task = None
    read_failures = 0            # 连续输入失败计数（满 3 次放弃，见读输入处）

    # 确认中心：主循环上的唯一消费者。cli.main 把同一实例注入 confirm/ask 回调，
    # 这里负责启动与收尾；未注入（测试/裸跑）时自建。
    from paperflow.terminal.confirm_center import ConfirmCenter
    center = confirm_center or ConfirmCenter(io, renderer)
    center.start()
    registry = registry or build_default_registry(
        CommandContext(io=io, renderer=renderer, mcp_manager=mcp_manager))

    def _cancel_run():
        """SIGINT handler：只取消当前 run 任务，REPL 本身继续存活（回到输入框）。"""
        # SIGINT handler：只取消当前 run_task，REPL 本身活着（回到输入框，不是退出）。
        # 只 cancel 主循环任务——子 agent 树的级联取消由 spawn 异步化保证。
        # run_task 是 create_task 建立的句柄，.cancel() 会在其下一个 await 点抛
        # CancelledError，被下方主循环接住。
        if run_task is not None and not run_task.done():
            run_task.cancel()

    try:
        # 无限循环，出口只有三处 break：/exit、EOF 或空框 Ctrl+C、输入连续失败 3 次。
        # 其余一切异常都在循环内消化并 continue——单轮失败不该带走整个会话。
        while True:
            # 每轮循环顶部触发后台记忆整合——放在读 stdin 之前，让用户思考期间累积的对话被整合，整合不阻塞本轮输入。
            if sleeptime is not None:
                try:
                    await sleeptime.run_once_if_due()
                except Exception:  # Sleeptime 失败不打断 REPL
                    logger.warning("sleeptime tick failed", exc_info=True)
            try:
                # io.read 必须经 to_thread 在 worker 线程执行：
                # PromptToolkitIO.read 内部session.prompt() 会自建事件循环（asyncio.run），
                # 而 _repl 跑在主事件循环线程——
                # 直接同步调用会抛 "asyncio.run() cannot be called from a running event loop"。
                # confirm/ask 回调已是 to_thread，read 对齐之。
                raw = await asyncio.to_thread(io.read, "❯ ")
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
            if not raw.strip():
                # 纯空白输入：直接忽略，不进意图管线——
                # 否则一次完整 LLM 调用后才被兜底拒绝，白烧 token。轻提示一次，
                # 避免用户以为卡死。
                renderer.print("（空输入已忽略）", style="dim")
                continue
            # 斜杠命令分发。dispatch 进 worker 线程：skill 的交互确认走
            # prompt_toolkit，绝不能跑在事件循环线程上（与 io.read 的
            # to_thread 同因）；renderer.print 内部持锁，跨线程安全。
            outcome = await asyncio.to_thread(registry.dispatch, raw)
            if outcome.exit:
                break
            if outcome.consumed:
                continue
            # 用户回显：每轮的翻历史锚点
            renderer.print_raw(f"❯ {raw}")
            query = raw
            # 每轮清残留：异常路径不消费 should_print，
            # 不重置则上一轮的流式缓冲会带进本轮的三段比对，导致最终答案漏打或重打。
            renderer.reset()
            # 先注册 SIGINT handler 再 create_task：注册与建任务之间的同步间隙若落一个 SIGINT，
            # 默认 handler 会在主线程抛 KeyboardInterrupt 崩 REPL。
            # handler 已就位则 _cancel_run 吞掉它（run_task 尚未赋值 → no-op，不崩）。
            run_task = None
            if can_sigint:
                try:
                    loop.add_signal_handler(signal.SIGINT, _cancel_run)
                except (NotImplementedError, RuntimeError):
                    # 信号注册失败（如非主线程/平台不支持）→ 降级为默认 Ctrl+C，不崩 REPL
                    can_sigint = False
            # 用 create_task 而非直接 await：需要一个可取消句柄交给 SIGINT handler（await 表达式本身无法被外部 cancel）。
            # 本行只把协程入队、不阻塞，真正的等待在下一行的 await。
            run_task = asyncio.create_task(supervisor.run(query))
            # 本轮 run 的三种失败都在紧跟的 except 里就地消化，都 continue、不 re-raise：
            # 一次 API 抖动 / 超轮 / 用户中断只终结本轮，不该把整个会话带走。
            try:
                result = await run_task
            except asyncio.CancelledError:
                # Ctrl+C 优雅中断：渲染器过滤孤儿事件（to_thread 无法真正取消）、打印提示、回到输入框。
                # 安全阀语义与 MaxTurnsExceeded 一致——不杀 REPL。
                renderer.interrupt()
                renderer.print("Cancelled")
                continue
            except MaxTurnsExceeded:
                renderer.print("Task exceeded max turns. Please rephrase and retry.")
                continue
            except Exception as e:
                # 错误 → 用户语言翻译：API 原文不直接当唯一呈现
                renderer.print(translate_error(e), style="red")
                continue
            finally:
                # 注销 SIGINT handler：handler 只在 run 期间有意义，
                # 常驻会让输入阶段的 Ctrl+C 被 _cancel_run 吞成 no-op，
                # Ctrl+C 三态里的“空框退出”随之失效。
                if can_sigint:
                    try:
                        loop.remove_signal_handler(signal.SIGINT)
                    except (NotImplementedError, RuntimeError):
                        pass
            # 正常收尾：finalize 终态渲染最后一个 live 块（停 spinner）；
            # should_print 比对流式缓冲与最终答案——一致则只补换行，被中间件
            # on_finish 改写过则补打最终版，既不重复也不漏打。
            renderer.finalize()
            text = renderer.should_print(result)
            if text:
                renderer.print_markdown(text)
            else:
                renderer.print("")

    finally:
        # 确认中心收尾：取消消费者任务，避免退出后任务泄漏告警
        await center.stop()
