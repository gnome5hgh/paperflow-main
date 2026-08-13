"""REPL 输入适配器。

契约：read(prompt) 读主输入；confirm(text) 做 yes/no 确认（Ctrl-D/EOF → False）；
ask(question) 读开放问题答案（Ctrl-D/EOF → 空串）。TTY 下用 prompt_toolkit
（Task 3），非 TTY 退化为内置 input()——管道/CI/测试走后者，行为与改造前一致。

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

#: 并发确认/提问串行化锁（沿 cli._stdin_lock 语义移至此层）
_confirm_lock = threading.Lock()


class InputIO:
    """输入适配器契约。子类实现 read/confirm/ask；测试注入 fake（鸭子类型）。"""

    def read(self, prompt: str) -> str:
        """读一行用户输入（主 REPL 循环）。Ctrl-D/空框 Ctrl+C → EOFError/KeyboardInterrupt。"""
        raise NotImplementedError

    def confirm(self, text: str) -> bool:
        """yes/no 确认。Ctrl-D/EOF → False（fail-safe 拒绝）。"""
        raise NotImplementedError

    def ask(self, question: str) -> str:
        """读一个开放问题的答案。Ctrl-D/EOF → 空串。"""
        raise NotImplementedError


class FallbackIO(InputIO):
    """非 TTY 实现：内置 input()，等价改造前的 _stdin_* 逻辑。"""

    def read(self, prompt: str) -> str:
        return input(prompt)

    def confirm(self, text: str) -> bool:
        with _confirm_lock:
            print(f"{text} (y/N) ", end="", flush=True)
            try:
                return input().strip().lower() in {"y", "yes", "是", "确定"}
            except EOFError:
                return False

    def ask(self, question: str) -> str:
        with _confirm_lock:
            print(question)
            try:
                return input("> ").strip()
            except EOFError:
                return ""


def _session_key_bindings() -> KeyBindings:
    """Enter=提交、Alt+Enter=换行；Ctrl+C 空框退出/有内容清空；Ctrl+D 空框退出。"""
    kb = KeyBindings()

    @kb.add("enter")
    def _accept(event):
        event.current_buffer.validate_and_handle()

    # prompt_toolkit 没有 Shift+Enter 键（Keys 枚举缺该键），换行用 Alt+Enter 等价代替
    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    @kb.add("c-c")
    def _cancel(event):
        if event.current_buffer.text:
            event.current_buffer.reset()
        else:
            raise KeyboardInterrupt

    @kb.add("c-d")
    def _eof(event):
        if not event.current_buffer.text:
            raise EOFError
        event.current_buffer.delete_before_cursor()

    return kb


def _toggle_confirm(state: list) -> None:
    """翻转确认选择（Yes↔No）。state 是 [bool] 闭包容器。"""
    state[0] = not state[0]


def _confirm_key_bindings(state: list):
    """confirm 选择器键绑定：←/→ 切换、Enter 以所选结果退出。

    注意 `@kb.add("left")` / `@kb.add("right")` 是**两个**绑定——`kb.add("left","right")`
    会被当成「先左后右」的按键序列而非二选一。
    """
    kb = KeyBindings()

    @kb.add("left")
    @kb.add("right")
    def _toggle(event):
        _toggle_confirm(state)

    @kb.add("enter")
    def _accept(event):
        event.app.exit(result=state[0])
    return kb


class PromptToolkitIO(InputIO):
    """TTY 实现：PromptSession 多行编辑、光标自由移动、历史落盘、自动建议。

    Enter=提交（validate_and_handle），Alt+Enter=换行。历史经 FileHistory 落盘
    history_path，跨会话保留；灰色自动建议来自历史匹配。Ctrl+C 空框退出、有内容
    清空（_repl 捕获 KeyboardInterrupt 即退出）；Ctrl+D 空框退出。
    """

    def __init__(self, history_path: str, session=None):
        self._session = session or PromptSession(
            multiline=True,
            history=FileHistory(history_path),
            auto_suggest=AutoSuggestFromHistory(),
            key_bindings=_session_key_bindings(),
            enable_history_search=True,
        )

    def read(self, prompt: str) -> str:
        return self._session.prompt(prompt)

    def confirm(self, text: str) -> bool:
        """方向键 Yes/No 选择器：←/→ 切换高亮项、Enter 确认（默认 Yes）。

        Ctrl+C/EOF 由 _repl 捕获（fail-safe 拒绝）。独立 PromptSession 与主输入
        session 隔离——主 session 是多行 Enter=提交，选择器需 Enter=以结果退出。
        """
        with _confirm_lock:
            state = [True]
            from prompt_toolkit.shortcuts import prompt as _pt_prompt
            from prompt_toolkit.styles import Style

            def _toolbar():
                yes = ("class:sel", " Yes ") if state[0] else ("", " Yes ")
                no = ("class:sel", " No ") if not state[0] else ("", " No ")
                return [("", f"{text}   "), yes, ("", "   "), no]

            result = _pt_prompt(
                "", key_bindings=_confirm_key_bindings(state),
                bottom_toolbar=_toolbar,
                style=Style([("sel", "reverse")]))
            return bool(result)

    def ask(self, question: str) -> str:
        with _confirm_lock:
            print(question)
            return self._session.prompt("> ")


def make_input_io(config) -> InputIO:
    """TTY 检测装配：isatty → PromptToolkitIO（历史落盘 workspace）；否则 FallbackIO。"""
    if sys.stdin.isatty():
        return PromptToolkitIO(str(Path(config.workspace) / "repl_history.txt"))
    return FallbackIO()
