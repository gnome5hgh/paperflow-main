# paperflow/terminal/io/interactive.py
"""TTY 输入适配：prompt_toolkit 的主输入框、历史落盘与两套键绑定。

主输入框支持多行编辑（Alt+Enter 换行、Enter 提交）、历史落盘（跨会话保留）与
基于历史的自动建议；确认框是独立的一次性 prompt，只识别字母键（y/a/n/Enter），
不依赖方向键（部分终端不可靠）。键绑定与 fail-safe 语义与 fallback.py 的非 TTY
实现刻意对齐（空输入即拒绝），两个实现可互换。
"""
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from .base import InputIO, _confirm_lock


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
        """Enter：提交当前输入（多行模式下必须显式绑定）。

        Args:
            event: KeyPressEvent，键绑定回调注入的按键事件
        """
        event.current_buffer.validate_and_handle()

    # prompt_toolkit 没有 Shift+Enter 键（Keys 枚举缺该键），换行用 Alt+Enter 等价代替
    @kb.add("escape", "enter")
    def _newline(event):
        """Alt+Enter：插入换行（prompt_toolkit 无 Shift+Enter 键）。

        Args:
            event: KeyPressEvent，键绑定回调注入的按键事件
        """
        event.current_buffer.insert_text("\n")

    # Ctrl+C：有内容先清空输入框（与常见 REPL 一致），空框才抛 KeyboardInterrupt 退出
    @kb.add("c-c")
    def _cancel(event):
        """Ctrl+C：有内容先清空输入框，空框才抛 KeyboardInterrupt 退出。

        Args:
            event: KeyPressEvent，键绑定回调注入的按键事件
        """
        if event.current_buffer.text:
            event.current_buffer.reset()
        else:
            raise KeyboardInterrupt

    # Ctrl+D：空框抛 EOFError 退出；有内容只删光标前一个字符（标准行编辑语义）
    @kb.add("c-d")
    def _eof(event):
        """Ctrl+D：空框抛 EOFError 退出，有内容只删光标前一个字符。

        Args:
            event: KeyPressEvent，键绑定回调注入的按键事件
        """
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
        """y/Y：立即接受本次放行。

        Args:
            event: KeyPressEvent，键绑定回调注入的按键事件
        """
        event.app.exit(result="y")

    # 键入 a/A：本会话同 (工具,路径) 放行
    @kb.add("a")
    @kb.add("A")
    def _all(event):
        """a/A：本会话同 (工具, 路径) 放行。

        Args:
            event: KeyPressEvent，键绑定回调注入的按键事件
        """
        event.app.exit(result="a")

    # 键入 n/N 立即拒绝
    @kb.add("n")
    @kb.add("N")
    def _no(event):
        """n/N：立即拒绝。

        Args:
            event: KeyPressEvent，键绑定回调注入的按键事件
        """
        event.app.exit(result="n")

    # Enter 默认拒绝：误按回车不误放行（写盘等高危操作保守处理）
    @kb.add("enter")
    def _accept(event):
        """Enter：默认拒绝（防误按回车即放行高危写操作）。

        Args:
            event: KeyPressEvent，键绑定回调注入的按键事件
        """
        event.app.exit(result="n")
    return kb


class PromptToolkitIO(InputIO):
    """TTY 环境下的输入适配器，基于 prompt_toolkit 实现高级交互功能。

    功能特点：
        - 多行编辑（Alt+Enter 换行，Enter 提交）。
        - 历史记录落盘（跨会话保留）。
        - 自动建议（根据历史记录灰色提示）。
        - 光标移动、搜索历史等。

    并发安全：confirm 和 confirm_choice 方法使用 _confirm_lock 串行化，避免多个线程同时弹出提示。
    此外，每个 confirm/ask 使用独立的 prompt 会话，不与主输入 session 共享，避免多线程下
    的 session 状态污染。

    Attributes:
        _session: PromptSession，主输入会话（confirm/ask 另起临时会话，避免与它共享状态）
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
            # 提交后擦掉输入框 UI（含已打文本）——否则提交内容残留在滚动区，
            # 与 repl 的 `❯ ` 回显叠成两份。参数在构造处而非 prompt() 调用处：
            # app 在 __init__ 一次性创建（erase_when_done 是构造参数，
            # PromptSession.prompt() 不接受它——该参数只在构造处生效），
            # prompt() 复用该 app。
            erase_when_done=True,
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
            输入框的提交后擦除（erase_when_done）在会话构造处配置——回显是
            屏上唯一的输入记录（ZCode 式翻历史锚点）。
        """
        return self._session.prompt(prompt)

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

