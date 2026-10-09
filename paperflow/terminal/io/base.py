"""REPL 输入适配的契约——把「读输入」与终端类型解耦。

契约：read(prompt) 读主输入；confirm(text) 做 yes/no 确认；confirm_choice(text)
做三态确认。EOF / Ctrl-D 一律按「拒绝」处理（fail-safe），绝不挂起。TTY 实现见
`interactive.py`（prompt_toolkit），非 TTY 见 `fallback.py`（内置 input）；
装配入口 `make_input_io` 在包 `__init__`。测试可注入鸭子类型的假实现。

并发确认（并行子 agent 同时触发 confirm）由 `_confirm_lock` 串行化：提示不交错，
且 prompt_toolkit 的 session 多线程并发读不安全——锁是硬性要求，故放在契约层
供两种实现共用。
"""
import threading

#: 并发确认/提问串行化锁：并行子 agent 同时弹确认框时保证提示不交错
_confirm_lock = threading.Lock()


class InputIO:
    """输入适配器抽象基类，定义输入操作的统一契约。

    所有输入操作都应实现 read/confirm，以便在 TTY 和非 TTY 环境下提供一致的行为。
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
        """三态确认：返回 "y"（本次放行）/ "a"（本会话同路径放行）/ "n"（拒绝）。

        默认实现基于 confirm() 折叠为二态（"y"/"n"）；TTY 实现识别 a 键。
        EOF/中断返回 "n"（fail-safe 拒绝）。

        Args:
            text: str，确认提示文本

        Returns:
            "y"（本次放行）/ "a"（本会话同路径放行）/ "n"（拒绝）；基类默认折叠为二态。
        """
        return "y" if self.confirm(text) else "n"
