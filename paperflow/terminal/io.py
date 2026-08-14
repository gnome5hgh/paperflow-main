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
    """非 TTY 实现：直接用内置 input() 读终端，供管道/CI/测试使用。"""

    def read(self, prompt: str) -> str:
        """读一行输入：把提示词原样交给内置 input()。"""
        return input(prompt)

    def confirm(self, text: str) -> bool:
        """yes/no 确认：接受 y/yes/是/确定，其余（含空回车）一律拒绝。

        持锁串行（并发确认不交错）；EOF 按拒绝处理（fail-safe）。
        """
        with _confirm_lock:
            # 提示文案标注默认值 (y/N)，即回车 = N，与 TTY 版 Enter 默认拒绝一致
            print(f"{text} (y/N) ", end="", flush=True)
            try:
                return input().strip().lower() in {"y", "yes", "是", "确定"}
            except EOFError:
                # EOF/Ctrl-D：拿不到答案时保守拒绝
                return False

    def ask(self, question: str) -> str:
        """读一个开放问题的答案；EOF/Ctrl-D 返回空串（由上层自行决策）。"""
        with _confirm_lock:
            print(question)
            try:
                return input("> ").strip()
            except EOFError:
                # EOF/Ctrl-D：返回空串而非抛错，上层（ask_user 回调）自行处理
                return ""


def _session_key_bindings() -> KeyBindings:
    """主输入框的键绑定：Enter=提交、Alt+Enter=换行；Ctrl+C 空框退出/有内容清空；
    Ctrl+D 空框退出（与 REPL 的退出语义对齐）。

    返回的 KeyBindings 交给 PromptSession 使用，测试里可逐键断言行为。
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
    """确认框键绑定：y/Y → Yes、n/N → No、Enter → 默认 No。

    只认键入，不依赖方向键（方向键在部分终端不响应，实测不可靠）。Enter 默认拒绝，
    与非 TTY 的 (y/N) 空输入拒绝一致——TTY/非 TTY 两实现行为可替换。
    """
    kb = KeyBindings()

    # 键入 y/Y 立即接受（result=True 结束确认框）
    @kb.add("y")
    @kb.add("Y")
    def _yes(event):
        event.app.exit(result=True)

    # 键入 n/N 立即拒绝
    @kb.add("n")
    @kb.add("N")
    def _no(event):
        event.app.exit(result=False)

    # Enter 默认拒绝：误按回车不误放行（写盘等高危操作保守处理）
    @kb.add("enter")
    def _accept(event):
        event.app.exit(result=False)
    return kb


class PromptToolkitIO(InputIO):
    """TTY 实现：PromptSession 多行编辑、光标自由移动、历史落盘、自动建议。

    Enter=提交（validate_and_handle），Alt+Enter=换行。历史经 FileHistory 落盘
    history_path，跨会话保留；灰色自动建议来自历史匹配。Ctrl+C 空框退出、有内容
    清空（_repl 捕获 KeyboardInterrupt 即退出）；Ctrl+D 空框退出。
    """

    def __init__(self, history_path: str, session=None):
        """构造主输入 session。

        history_path 是历史落盘路径（跨会话保留）；session 可注入（测试传 fake，
        断言 read 委托给 session.prompt）。
        """
        self._session = session or PromptSession(
            multiline=True,                      # 多行编辑：Alt+Enter 换行、Enter 提交
            history=FileHistory(history_path),   # 历史落盘，跨会话可回看/搜索
            auto_suggest=AutoSuggestFromHistory(),   # 灰色自动建议来自历史匹配
            key_bindings=_session_key_bindings(),
            enable_history_search=True,
        )

    def read(self, prompt: str) -> str:
        """读主输入：委托给 session.prompt（内部自建事件循环，须经 to_thread 调用）。"""
        return self._session.prompt(prompt)

    def confirm(self, text: str) -> bool:
        """y/n 键入确认：y/Y → Yes、n/N → No、Enter → 默认 No。

        只认键入不依赖方向键（方向键在部分终端不响应）。Ctrl+C/EOF 由调用方（确认
        回调）捕获，按拒绝处理（fail-safe）。用独立的一次性 prompt，与主输入 session
        隔离；默认拒绝与非 TTY 的 (y/N) 空输入拒绝一致，两实现可替换。
        """
        with _confirm_lock:
            from prompt_toolkit.shortcuts import prompt as _pt_prompt
            result = _pt_prompt(f"{text} (y/N) ",
                                key_bindings=_confirm_key_bindings())
            return bool(result)

    def ask(self, question: str) -> str:
        """读开放问题答案：用独立的一次性 prompt（与 confirm 一致）。

        复用主 session 在多线程 worker 下不可靠（会 EOF/卡住），故独立建一次性的
        prompt；独立 session 也不把 ask 问答混进主输入历史。
        """
        with _confirm_lock:
            print(question)
            from prompt_toolkit.shortcuts import prompt as _pt_prompt
            return _pt_prompt("> ")


def make_input_io(config) -> InputIO:
    """TTY 检测装配：isatty → PromptToolkitIO（历史落盘到 workspace）；否则 FallbackIO。"""
    if sys.stdin.isatty():
        # 主输入历史文件放在 workspace 下，跨会话保留
        return PromptToolkitIO(str(Path(config.workspace) / "repl_history.txt"))
    return FallbackIO()
