"""archival memory 工具（命名对齐 Letta function_sets/archival.py）。"""
from __future__ import annotations

__all__ = ["archival_memory_insert", "archival_memory_search"]


def archival_memory_insert(ctx, content: str, tags: list[str] | None = None) -> str:
    p = ctx.passage_manager.insert_passage(ctx.agent_id, content, tags=tags or [])
    return f"Stored to archival memory: {p.id}"


def archival_memory_search(ctx, query: str, tags: list[str] | None = None,
                           tag_match_mode: str = "all", top_k: int = 10,
                           start_datetime: str | None = None,
                           end_datetime: str | None = None) -> str:
    hits = ctx.passage_manager.search_passages(
        ctx.agent_id, query, tags=tags, top_k=top_k,
        start_datetime=start_datetime, end_datetime=end_datetime)
    if not hits:
        return "No archival memory found."
    return "\n".join(f"- [{p.id}] {p.text}" for p in hits)
