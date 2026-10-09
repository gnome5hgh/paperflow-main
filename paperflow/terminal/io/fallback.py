"""非 TTY 环境下的输入适配器，基于内置 input() 实现。

适用于管道、CI 环境或测试场景，不依赖 prompt_toolkit。
所有确认操作均经契约层的 `_confirm_lock` 串行化，与 TTY 实现行为一致：
空回车默认拒绝（提示中显示 `(y/N)` / `(y/a/N)`），EOF 按拒绝处理（fail-safe）。
"""
from .base import InputIO, _confirm_lock


class FallbackIO(InputIO):
    """非 TTY 输入适配：读输入直接用内置 input()，确认/三态确认自解析字母。"""

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
