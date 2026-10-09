# paperflow/rag/models/reranker.py
"""精排：Reranker 协议与云端实现（硅基流动 /v1/rerank，Jina/Cohere 风格）。

返回值契约：按相关度降序的文档下标列表（长度 ≤ top_k），调用方零适配。
"""
import time
from typing import Protocol

import httpx

from paperflow.rag.models.embedder import RETRY_BACKOFF_BASE
from paperflow.core.security.text import sanitize_surrogates


class Reranker(Protocol):
    """精排协议：query + 候选文档 → 按相关度降序的下标列表。"""

    def __call__(self, query: str, docs: list[str], top_k: int) -> list[int]:
        """按相关度对候选文档排序。

        Args:
            query: str，查询文本
            docs: list[str]，候选文档列表
            top_k: int，最多返回的下标个数

        Returns:
            按相关度降序的文档下标列表（长度 ≤ top_k）。
        """
        ...


class RagReranker:
    """/v1/rerank 云端精排器。失败重试后抛 RuntimeError("云端精排不可用: …")。

    服务端返回 {"results": [{"index": int, "relevance_score": float}]}；
    客户端防御性按分数降序重排（不信任服务端有序承诺），再截断 top_k。

    Attributes:
        model_name: str，精排模型名
        _max_retries: int，可恢复错误的最大重试次数
        _client: httpx.Client，云端 /v1/rerank 客户端
    """

    def __init__(self, base_url: str, api_key: str, model: str, *,
                 max_retries: int, timeout: float,
                 transport: httpx.BaseTransport | None = None):
        """生产值来自 ``rag.rerank.*``（唯一声明点 config.py，RagService 注入）。

        Args:
            max_retries: 连接错误/超时/5xx（及 408/429）的重试次数（次），
                与嵌入侧语义对齐；4xx 立即失败不重试。
            timeout: httpx.Client 读超时（秒）。
        """
        self.model_name = model
        self._max_retries = max_retries
        kwargs = {"base_url": base_url.rstrip("/"), "timeout": timeout,
                  "headers": {"Authorization": f"Bearer {api_key}"}}
        if transport is not None:
            kwargs["transport"] = transport   # 测试注入口（MockTransport）
        self._client = httpx.Client(**kwargs)

    def __call__(self, query: str, docs: list[str], top_k: int) -> list[int]:
        """调用云端 /v1/rerank 并防御性重排截断。

        Args:
            query: str，查询文本（自动清洗 surrogate）
            docs: list[str]，候选文档列表
            top_k: int，最多返回的下标个数

        Returns:
            按相关度降序的文档下标列表（越界下标被丢弃，长度 ≤ top_k）；docs 为空返回 []。
        """
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
            except httpx.HTTPStatusError as e:
                last_err = e
                # 与 embedding.py 对齐：4xx 不可恢复（408/429 例外）立即失败，
                # 不空耗退避；5xx 仍重试。
                if e.response.status_code < 500 and e.response.status_code not in (408, 429):
                    break
                if attempt < self._max_retries:
                    time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
            except httpx.LocalProtocolError as e:
                last_err = e
                # 客户端请求构造错误（非法 header 等）重试无意义
                break
            except (httpx.HTTPError, KeyError, ValueError) as e:
                last_err = e
                if attempt < self._max_retries:
                    time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
        raise RuntimeError(f"云端精排不可用（{self.model_name}）：{last_err}") from last_err
