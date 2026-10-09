# paperflow/rag/encoders/embedder.py
"""稠密编码：Embedder 协议与云端实现（OpenAI 兼容 /v1/embeddings）。

云端 only：构造不碰网络、不校验 api_key（软依赖），失败在调用时以 RuntimeError
暴露，由调用方按各自降级语义处理（检索跳稠密路、索引明确报错）。
"""
import logging
import time
from typing import Protocol

import httpx
import numpy as np

from paperflow.core.security.text import sanitize_surrogates

logger = logging.getLogger(__name__)

#: 模型 → 向量维度的静态映射：dim 是建集合/缓存校验的硬前提，能不发网络就不发。
#: 新增模型时在此登记；未登记模型走单条探测（首次 dim 访问发一次真实请求）。
_EMBED_DIMS = {
    "Qwen/Qwen3-Embedding-0.6B": 1024,
}


class Embedder(Protocol):
    """稠密编码协议：文本批次 → L2 归一化向量矩阵。

    与原 rag 协议逐字一致——调用方（索引器/检索器）无需感知实现更换。

    Attributes:
        dim: int，模型输出的向量维度（只读静态表，不发网络探测）
    """

    def __call__(self, texts: list[str]) -> np.ndarray:
        """把一批文本编码成 (len(texts), dim) 的归一化向量矩阵。

        Args:
            texts: list[str]，待编码的文本批次

        Returns:
            (len(texts), dim) 的 L2 归一化向量矩阵；空输入返回零行矩阵。
        """
        ...

    @property
    def dim(self) -> int:
        """模型输出的向量维度。"""
        ...


#: 指数退避基数（秒）：第 attempt 次重试前 ``sleep(RETRY_BACKOFF_BASE * 2 ** attempt)``。
#: 重试算法契约——embedding 与 rerank 共用（rerank 自本模块导入），改它改变
#: 重试等待时长与最坏延迟，不影响结果。
RETRY_BACKOFF_BASE = 0.5


class RagEmbedder:
    """OpenAI 兼容 /v1/embeddings 云端编码器（默认端点：硅基流动）。

    重试语义与主 LLM 客户端对齐：连接错误/5xx 指数退避重试 max_retries 次，
    耗尽抛 RuntimeError("云端嵌入不可用: …")。L2 归一化在客户端做，保证下游
    余弦相似度与 Milvus 既有向量可比。

    Attributes:
        model_name: str，嵌入模型名
        _max_retries: int，可恢复错误的最大重试次数
        _batch_size: int，单批请求的文本条数
        _client: httpx.Client，云端 /v1/embeddings 客户端
    """

    def __init__(self, base_url: str, api_key: str, model: str, *,
                 batch_size: int, max_retries: int, timeout: float,
                 transport: httpx.BaseTransport | None = None):
        """生产值来自 ``rag.embedding.*``（唯一声明点
        config.py，装配侧注入）；batch_size/timeout/max_retries 不改变向量结果。

        Args:
            batch_size: 单批 ``/v1/embeddings`` 请求送入的文本条数（条），大批量按此切片。
            max_retries: 连接错误/超时/5xx（及 408/429）的重试次数（次）；
                4xx 与客户端构造错误立即失败不重试。
            timeout: httpx.Client 读超时（秒）。
        """
        self.model_name = model
        self._batch_size = batch_size
        self._max_retries = max_retries
        kwargs = {"base_url": base_url.rstrip("/"), "timeout": timeout,
                  "headers": {"Authorization": f"Bearer {api_key}"}}
        if transport is not None:
            kwargs["transport"] = transport   # 测试注入口（MockTransport）
        self._client = httpx.Client(**kwargs)
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        """静态映射优先；未登记模型用单条探测测维（结果缓存，仅一次）。"""
        if self._dim is None:
            self._dim = _EMBED_DIMS.get(self.model_name)
            if self._dim is None:
                self._dim = int(self([_PROBE_TEXT]).shape[1])
        return self._dim

    @property
    def dim_static(self) -> int | None:
        """**不发网络**的维度：静态映射命中返回值，未登记模型返回 None。

        调用方（路由器降级路径/缓存键）在断网时不能触发 dim 探测请求——探测
        会抛异常并中断启动。此属性只读静态表，未登记即 None，由调用方决定
        是降级为零向量还是给出清晰错误。
        """
        return _EMBED_DIMS.get(self.model_name)

    def __call__(self, texts: list[str]) -> np.ndarray:
        """把一批文本分批发往云端编码并做 L2 归一化。

        Args:
            texts: list[str]，待编码文本批次（自动清洗 surrogate）

        Returns:
            (len(texts), dim) 的归一化向量矩阵；空输入返回零行矩阵。
        """
        if not texts:
            return np.zeros((0, self.dim))
        # surrogate 字符会炸远端 tokenizer（本地版同款教训）
        texts = [sanitize_surrogates(t) for t in texts]
        out = [self._embed_batch(texts[i:i + self._batch_size])
               for i in range(0, len(texts), self._batch_size)]
        return _l2_normalize(np.vstack(out))

    def _embed_batch(self, batch: list[str]) -> np.ndarray:
        """单批请求 + 重试。响应 data 按 index 排序后取 embedding（服务端不保证有序）。

        只对**可恢复**错误退避重试：连接错误 / 超时 / 5xx（以及 408/429）。
        4xx（认证/参数错误）与客户端请求构造错误（如空 api_key 产生的非法
        Authorization header）重试必然同样失败——立即中止，避免冷启动/索引在
        必败请求上空耗退避（否则把冷启动延迟预算顶出预期）。

        Args:
            batch: list[str]，单批文本（条数 ≤ _batch_size）

        Returns:
            该批的向量矩阵 (len(batch), dim)，float32；重试耗尽抛 RuntimeError。
        """
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                r = self._client.post("/embeddings",
                                      json={"model": self.model_name, "input": batch})
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request,
                                                response=r)
                r.raise_for_status()
                data = sorted(r.json()["data"], key=lambda d: d["index"])
                # 条数校验：服务端静默丢条会让返回矩阵行数与输入错位，
                # 下游 chunk 与向量一一对应即被破坏（召回张冠李戴）——视为
                # 可重试失败。ValueError 落入下方通用重试分支。
                if len(data) != len(batch):
                    raise ValueError(
                        f"云端嵌入返回条数 {len(data)} != 批次大小 {len(batch)}")
                return np.array([d["embedding"] for d in data], dtype=np.float32)
            except httpx.HTTPStatusError as e:
                last_err = e
                # 4xx 不可恢复（408/429 例外）——立即中止，不空耗退避
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
        raise RuntimeError(f"云端嵌入不可用（{self.model_name}）：{last_err}") from last_err


#: dim 探测用的一条无害短文本（避免空列表边界）
_PROBE_TEXT = "dim"


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    """逐行 L2 归一化；零向量（空输入占位）除零保护返回原值。

    Args:
        v: np.ndarray，待归一化的向量矩阵（每行一个向量）

    Returns:
        逐行 L2 归一化后的矩阵；零向量行保持原值（除零保护）。
    """
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return v / norms
