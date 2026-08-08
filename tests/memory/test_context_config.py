# tests/test_context_config.py
from paperflow.core.memory.context_config import ContextConfig, SummarySchema


def test_resolve_explicit_size():
    cfg = ContextConfig(context_size=8000)
    assert cfg.resolve_context_size(65536) == 8000


def test_resolve_auto_half_window():
    cfg = ContextConfig()
    assert cfg.resolve_context_size(65536) == 32768


def test_defaults():
    cfg = ContextConfig()
    assert cfg.trigger_ratio == 0.8
    assert cfg.reserve_ratio == 0.1
    assert cfg.context_size == 0


def test_summary_schema_fields():
    s = SummarySchema(
        task_overview="t", current_state="c", important_discoveries="d",
        next_steps="n", context_to_preserve="p",
    )
    assert s.task_overview == "t"
    assert s.context_to_preserve == "p"
