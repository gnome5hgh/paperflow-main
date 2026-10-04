# paperflow/terminal/io.py
"""REPL 输入适配器——把「读输入」与终端类型解耦。

契约：read(prompt) 读主输入；confirm(text) 做 yes/no 确认（Ctrl-D/EOF → False，
fail-safe 拒绝）；ask(question) 读开放问题答案（Ctrl-D/EOF → 空串）。
TTY 下用 prompt_toolkit（多行编辑、历史落盘、自动建议），非 TTY 退化为内置
input()——管道/CI/测试走后者，两套实现行为可替换。

并发确认（并行子 agent 同时触发 confirm/ask）由 _confirm_lock 串行化：提示不
交错，且 prompt_toolkit 的 session 多线程并发读不安全——锁是硬性要求。
"""
import sys
import threading
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

#: 并发确认/提问串行化锁：并行子 agent 同时弹确认框时保证提示不交错
_confirm_lock = threading.Lock()


class InputIO:
    """输入适配器抽象基类，定义输入操作的统一契约。

    所有输入操作都应实现 read/confirm/ask，以便在 TTY 和非 TTY 环境下提供一致的行为。
    测试时可通过注入假实现（鸭子类型）进行单元测试。
    """

    def read(self, prompt: str) -> str:
        """
        读取一行用户输入（用于主 REPL 循环）。

        Args:
            prompt (str): 显示给用户的提示符。

        Returns:
            str: 用户输入的字符串（不包含换行符）。

        Raises:
            EOFError: 用户按下 Ctrl-D（或输入流结束）且输入框为空。
            KeyboardInterrupt: 用户按下 Ctrl+C 且输入框为空（或有内容时由键绑定处理）。
        """
        raise NotImplementedError

    def confirm(self, text: str) -> bool:
        """
        执行 yes/no 确认操作。

        Args:
            text (str): 确认提示文本。

        Returns:
            bool: 用户确认返回 True，否则返回 False。
                  当遇到 EOF（Ctrl-D）或中断时，返回 False（fail-safe 拒绝）。
        """
        raise NotImplementedError

    def confirm_choice(self, text: str) -> str:
        """
        三态确认：返回 "y"（本次放行）/ "a"（本会话同路径放行）/ "n"（拒绝）。

        默认实现基于 confirm() 折叠为二态（"y"/"n"）；TTY 实现识别 a 键。
        EOF/中断返回 "n"（fail-safe 拒绝）。
        """
        return "y" if self.confirm(text) else "n"

    def ask(self, question: str) -> str:
        """
        读取一个开放问题的答案（自由文本输入）。

        Args:
            question (str): 问题文本。

        Returns:
            str: 用户输入的回答（去除首尾空白）。若遇到 EOF（Ctrl-D）或中断，返回空字符串。
        """
        raise NotImplementedError


class FallbackIO(InputIO):
    """非 TTY 环境下的输入适配器，基于内置 input() 实现。

    适用于管道、CI 环境或测试场景，不依赖 prompt_toolkit。
    所有输入操作均串行化（通过 _confirm_lock）以保持一致性和安全性。
    """

    def read(self, prompt: str) -> str:
        """
        直接使用内置 input() 读取一行输入。

        Args:
            prompt (str): 提示符。

        Returns:
            str: 用户输入。

        Raises:
            EOFError: 输入流结束。
            KeyboardInterrupt: 用户中断。
        """
        return input(prompt)

    def confirm(self, text: str) -> bool:
        """
        使用内置 input() 进行确认，接受 y/yes/是/确定（不区分大小写），其余（含空回车）拒绝。

        Args:
            text (str): 确认提示。

        Returns:
            bool: 用户确认返回 True，否则 False。EOF 或中断时返回 False。

        Notes:
            - 使用锁 _confirm_lock 保证并发调用时提示不交错。
            - 默认选项为 N（提示中显示 (y/N)），与 TTY 实现行为一致。
            - EOF 按拒绝处理（fail-safe），防止误操作。
        """
        with _confirm_lock:
            # 显示提示，并标记默认值为 N（回车即拒绝）
            print(f"{text} (y/N) ", end="", flush=True)
            try:
                return input().strip().lower() in {"y", "yes", "是", "确定"}
            except EOFError:
                # EOF/Ctrl-D：无法获得输入时保守拒绝
                return False

    def confirm_choice(self, text: str) -> str:
        """
        三态确认：y=yes / a=all（本会话同路径放行）/ 其余拒绝。

        Args:
            text (str): 确认提示。

        Returns:
            str: "y" / "a" / "n"。EOF 返回 "n"（fail-safe）。
        """
        with _confirm_lock:
            print(f"{text} (y/a/N) ", end="", flush=True)
            try:
                raw = input().strip().lower()
            except EOFError:
                return "n"
            if raw in {"y", "yes", "是", "确定"}:
                return "y"
            if raw in {"a", "all", "全部"}:
                return "a"
            return "n"

    def ask(self, question: str) -> str:
        """
        使用内置 input() 读取开放问题的答案。

        Args:
            question (str): 问题文本。

        Returns:
            str: 用户输入（去除首尾空白），若 EOF 则返回空字符串。

        Notes:
            - 持锁串行化，避免并发提示交错。
            - EOF 返回空字符串，由上层回调决定如何处理（如重试或忽略）。
        """
        with _confirm_lock:
            print(question)
            try:
                return input("[回答模式] > ").strip()
            except EOFError:
                # EOF/Ctrl-D：返回空串而非抛错，上层（ask_user 回调）自行处理
                return ""


def _session_key_bindings() -> KeyBindings:
    """
    构造主输入框的键绑定配置。

    绑定策略：
        - Enter：提交当前输入（触发 validate_and_handle）。
        - Alt+Enter：插入换行符（因为 prompt_toolkit 原生不支持 Shift+Enter）。
        - Ctrl+C：若输入框非空则清空内容，否则抛出 KeyboardInterrupt 退出 REPL。
        - Ctrl+D：若输入框为空则抛出 EOFError 退出 REPL，否则删除光标前一个字符（标准行编辑行为）。

    这些绑定与 REPL 主循环的异常处理配合，实现优雅退出和清空功能。

    Returns:
        KeyBindings: 可应用于 PromptSession 的键绑定对象。
    """
    kb = KeyBindings()

    # Enter：把当前输入交给 validate_and_handle 提交（multiline 下必须显式绑定）
    @kb.add("enter")
    def _accept(event):
        event.current_buffer.validate_and_handle()

    # prompt_toolkit 没有 Shift+Enter 键（Keys 枚举缺该键），换行用 Alt+Enter 等价代替
    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    # Ctrl+C：有内容先清空输入框（与常见 REPL 一致），空框才抛 KeyboardInterrupt 退出
    @kb.add("c-c")
    def _cancel(event):
        if event.current_buffer.text:
            event.current_buffer.reset()
        else:
            raise KeyboardInterrupt

    # Ctrl+D：空框抛 EOFError 退出；有内容只删光标前一个字符（标准行编辑语义）
    @kb.add("c-d")
    def _eof(event):
        if not event.current_buffer.text:
            raise EOFError
        event.current_buffer.delete_before_cursor()

    return kb


def _confirm_key_bindings():
    """
    构造确认框的键绑定配置（用于 PromptToolkitIO.confirm / confirm_choice）。

    绑定策略：
        - y / Y：立即接受（返回 "y"）。
        - a / A：本会话同 (工具,路径) 放行（返回 "a"）。
        - n / N：立即拒绝（返回 "n"）。
        - Enter：默认拒绝（保守策略，防止误操作放行）。

    仅支持字母键输入，不依赖方向键（方向键在某些终端不可靠）。
    默认拒绝行为与非 TTY 的 (y/a/N) 空输入拒绝保持一致，确保两种实现可互换。

    Returns:
        KeyBindings: 可应用于临时 prompt 的键绑定对象。
    """
    kb = KeyBindings()

    # 键入 y/Y 立即接受（result="y" 结束确认框）
    @kb.add("y")
    @kb.add("Y")
    def _yes(event):
        event.app.exit(result="y")

    # 键入 a/A：本会话同 (工具,路径) 放行
    @kb.add("a")
    @kb.add("A")
    def _all(event):
        event.app.exit(result="a")

    # 键入 n/N 立即拒绝
    @kb.add("n")
    @kb.add("N")
    def _no(event):
        event.app.exit(result="n")

    # Enter 默认拒绝：误按回车不误放行（写盘等高危操作保守处理）
    @kb.add("enter")
    def _accept(event):
        event.app.exit(result="n")
    return kb


class PromptToolkitIO(InputIO):
    """TTY 环境下的输入适配器，基于 prompt_toolkit 实现高级交互功能。

    功能特点：
        - 多行编辑（Alt+Enter 换行，Enter 提交）。
        - 历史记录落盘（跨会话保留）。
        - 自动建议（根据历史记录灰色提示）。
        - 光标移动、搜索历史等。

    并发安全：confirm 和 ask 方法使用 _confirm_lock 串行化，避免多个线程同时弹出提示。
    此外，每个 confirm/ask 使用独立的 prompt 会话，不与主输入 session 共享，避免多线程下
    的 session 状态污染。
    """

    def __init__(self, history_path: str, session=None):
        """
        初始化主输入会话。

        Args:
            history_path (str): 历史记录文件的路径，用于跨会话持久化。
            session (PromptSession, optional): 可注入的 PromptSession 实例，用于测试。
                                                默认自动创建带有标准配置的会话。
        """
        self._session = session or PromptSession(
            multiline=True,                      # 多行编辑：Alt+Enter 换行、Enter 提交
            history=FileHistory(history_path),   # 历史落盘，跨会话可回看/搜索
            auto_suggest=AutoSuggestFromHistory(),   # 灰色自动建议来自历史匹配
            key_bindings=_session_key_bindings(),
            enable_history_search=True,
        )

    def read(self, prompt: str) -> str:
        """
        读取主 REPL 输入。

        Args:
            prompt (str): 提示符。

        Returns:
            str: 用户输入。

        Note:
            此方法内部会启动新的事件循环（asyncio.run），因此调用方应使用
            asyncio.to_thread 将其移至工作线程，避免与主事件循环冲突。
            erase_when_done：提交后擦掉输入框 UI（含已打的文本）——否则提交的
            内容会残留在滚动区，与 repl 的 `❯ ` 回显叠成两份。回显是唯一的
            输入记录（ZCode 式翻历史锚点）。
        """
        return self._session.prompt(prompt, erase_when_done=True)

    def confirm(self, text: str) -> bool:
        """
        使用 prompt_toolkit 临时提示进行 yes/no 确认。

        Args:
            text (str): 确认提示文本。

        Returns:
            bool: 用户确认返回 True，否则返回 False。

        Notes:
            - 使用独立的一次性 prompt，不与主 session 共享历史，避免污染。
            - 持锁 _confirm_lock 串行化，防止并发确认时提示交错或 session 冲突。
            - 键绑定只识别 y/Y/n/N/Enter（confirm_choice 另识别 a/A），
              Enter 默认拒绝，与非 TTY 实现一致。
            - 异常（如 Ctrl+C）由调用方捕获并返回 False。
        """
        return self.confirm_choice(text) == "y"

    def confirm_choice(self, text: str) -> str:
        """
        使用 prompt_toolkit 临时提示进行三态确认。

        Args:
            text (str): 确认提示文本。

        Returns:
            str: "y"（本次放行）/ "a"（本会话同路径放行）/ "n"（拒绝）。
                 EOF/异常返回 "n"（fail-safe 拒绝）。

        Notes:
            - 持锁 _confirm_lock 串行化，防止并发确认时提示交错或 session 冲突。
            - 键绑定识别 y/Y/a/A/n/N/Enter，Enter 默认拒绝。
        """
        with _confirm_lock:
            from prompt_toolkit.shortcuts import prompt as _pt_prompt
            try:
                result = _pt_prompt(f"{text} (y/a/N) ",
                                    key_bindings=_confirm_key_bindings())
            except (EOFError, KeyboardInterrupt):
                return "n"
            return result if result in ("y", "a") else "n"

    def ask(self, question: str) -> str:
        """
        使用 prompt_toolkit 临时提示读取开放问题的答案。

        Args:
            question (str): 问题文本。

        Returns:
            str: 用户输入（去除首尾空白）。

        Notes:
            - 使用独立的 prompt，不混入主输入历史。
            - 持锁 _confirm_lock 串行化，避免并发问题。
            - 此方法不捕获 EOF，由上层调用者（ask_user 回调）处理异常并返回空串。
        """
        with _confirm_lock:
            print(question)
            from prompt_toolkit.shortcuts import prompt as _pt_prompt
            # 前缀明示输入归属（配合回答模式横幅）：此刻输入是回答，不是新任务
            return _pt_prompt("[回答模式] > ")


def make_input_io(config) -> InputIO:
    """
    工厂函数，根据终端类型装配合适的输入适配器。

    Args:
        config: 配置对象，需要包含 workspace 属性（字符串），用于存放历史文件。

    Returns:
        InputIO: 若标准输入为 TTY，则返回 PromptToolkitIO 实例，历史文件保存在 workspace 下；
                 否则返回 FallbackIO 实例（用于管道/CI/测试）。

    Note:
        TTY 环境下历史文件固定命名为 repl_history.txt，存放在 workspace 目录中。
    """
    if sys.stdin.isatty():
        # 主输入历史文件放在 workspace 下，跨会话保留
        return PromptToolkitIO(str(Path(config.workspace) / "repl_history.txt"))
    return FallbackIO()