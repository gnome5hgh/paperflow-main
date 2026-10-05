# paperflow/core/llm/rerank.py
"""精排：Reranker 协议与云端实现（硅基流动 /v1/rerank，Jina/Cohere 风格）。

协议原在 rag/encoders/reranker.py，随本地 CrossEncoder 退役上收至此
（spec 2026-10-05-embedding-cloud-startup §3）。返回值契约与退役前的本地实现
一致：按相关度降序的文档下标列表（长度 ≤ top_k），调用方零适配。
"""
import time

import httpx

from paperflow.core.security.text import sanitize_surrogates


class Reranker:
    """精排协议：query + 候选文档 → 按相关度降序的下标列表。"""

    def __call__(self, query: str, docs: list[str], top_k: int) -> list[int]:
        ...


class CloudReranker:
    """/v1/rerank 云端精排器。失败重试后抛 RuntimeError("云端精排不可用: …")。

    服务端返回 {"results": [{"index": int, "relevance_score": float}]}；
    客户端防御性按分数降序重排（不信任服务端有序承诺），再截断 top_k。
    """

    def __init__(self, base_url: str, api_key: str, model: str, *,
                 max_retries: int = 2, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        self.model_name = model
        self._max_retries = max_retries
        kwargs = {"base_url": base_url.rstrip("/"), "timeout": timeout,
                  "headers": {"Authorization": f"Bearer {api_key}"}}
        if transport is not None:
            kwargs["transport"] = transport   # 测试注入口（MockTransport）
        self._client = httpx.Client(**kwargs)

    def __call__(self, query: str, docs: list[str], top_k: int) -> list[int]:
        if not docs:
            return []
        payload = {"model": self.model_name,
                   "query": sanitize_surrogates(query),
                   "documents": [sanitize_surrogates(d) for d in docs]}
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                r = self._client.post("/rerank", json=payload)
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request,
                                                response=r)
                r.raise_for_status()
                results = r.json()["results"]
                ranked = sorted(results, key=lambda x: x["relevance_score"], reverse=True)
                return [x["index"] for x in ranked[:top_k] if 0 <= x["index"] < len(docs)]
            except (httpx.HTTPError, KeyError, ValueError) as e:
                last_err = e
                if attempt < self._max_retries:
                    time.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"云端精排不可用（{self.model_name}）：{last_err}") from last_err
