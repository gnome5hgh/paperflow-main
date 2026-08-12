"""REPL 输入适配器。

契约：read(prompt) 读主输入；confirm(text) 做 yes/no 确认（Ctrl-D/EOF → False）；
ask(question) 读开放问题答案（Ctrl-D/EOF → 空串）。TTY 下用 prompt_toolkit
（Task 3），非 TTY 退化为内置 input()——管道/CI/测试走后者，行为与改造前一致。

并发确认（并行子 agent 同时触发 confirm/ask）由 _confirm_lock 串行化：提示不
交错，且 prompt_toolkit 的 session 多线程并发读不安全——锁是硬性要求。
"""
import threading

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
            print(text, end="", flush=True)
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
