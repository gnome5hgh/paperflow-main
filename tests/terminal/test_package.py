# tests/terminal/test_package.py
def test_terminal_package_imports():
    import paperflow.terminal
    assert paperflow.terminal.__doc__
