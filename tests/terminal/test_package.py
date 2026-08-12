# tests/terminal/test_package.py
from paperflow.terminal import (
    InputIO, FallbackIO, PromptToolkitIO, make_input_io,
    StreamRenderer, PlainBlock, RichBlock, make_renderer,
)


def test_terminal_package_imports():
    import paperflow.terminal
    assert paperflow.terminal.__doc__


def test_package_unified_exports():
    # 包级统一导出公开接口：调用方（cli.py / 测试）从 paperflow.terminal 直接导入。
    import paperflow.terminal as terminal

    for name in ("InputIO", "FallbackIO", "PromptToolkitIO", "make_input_io",
                 "StreamRenderer", "PlainBlock", "RichBlock", "make_renderer"):
        assert name in terminal.__all__, name
    # 导出的类/工厂均可调用（类是 callable，工厂是函数）
    for obj in (make_input_io, make_renderer, StreamRenderer, InputIO,
                FallbackIO, PromptToolkitIO, PlainBlock, RichBlock):
        assert callable(obj)
