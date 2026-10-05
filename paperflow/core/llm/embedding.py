# paperflow/core/llm/embedding.py
"""稠密编码：Embedder 协议与云端实现（OpenAI 兼容 /v1/embeddings）。

协议原在 rag/encoders/embedder.py，随本地 sentence-transformers 退役上收至此
（spec 2026-10-05-embedding-cloud-startup §3）——core 定义接口，rag/services
与 cli 向下依赖。云端 only：构造不碰网络、不校验 api_key（软依赖），失败在
调用时以 RuntimeError 暴露，由调用方按各自降级语义处理（路由退稀疏、检索跳
稠密路、索引明确报错）。
"""
import logging
import time

import httpx
import numpy as np

from paperflow.core.security.text import sanitize_surrogates

logger = logging.getLogger(__name__)

#: 模型 → 向量维度的静态映射：dim 是建集合/缓存校验的硬前提，能不发网络就不发。
#: 新增模型时在此登记；未登记模型走单条探测（首次 dim 访问发一次真实请求）。
_EMBED_DIMS = {
    "Qwen/Qwen3-Embedding-0.6B": 1024,
}


class Embedder:
    """稠密编码协议：文本批次 → L2 归一化向量矩阵。

    与原 rag 协议逐字一致——调用方（索引器/路由器/检索器）无需感知实现更换。
    """

    def __call__(self, texts: list[str]) -> np.ndarray:
        """把一批文本编码成 (len(texts), dim) 的归一化向量矩阵。"""
        ...

    @property
    def dim(self) -> int:
        """模型输出的向量维度。"""
        ...


class CloudEmbedder:
    """OpenAI 兼容 /v1/embeddings 云端编码器（默认端点：硅基流动）。

    重试语义与主 LLM 客户端对齐：连接错误/5xx 指数退避重试 max_retries 次，
    耗尽抛 RuntimeError("云端嵌入不可用: …")。L2 归一化在客户端做——与
    SbertEmbedder(normalize_embeddings=True) 输出语义一致，下游余弦相似度
    与既有 Milvus 向量可比。
    """

    def __init__(self, base_url: str, api_key: str, model: str, *,
                 batch_size: int = 32, max_retries: int = 2,
                 timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
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

    def __call__(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim))
        # surrogate 字符会炸远端 tokenizer（本地版同款教训，见原 SbertEmbedder）
        texts = [sanitize_surrogates(t) for t in texts]
        out = [self._embed_batch(texts[i:i + self._batch_size])
               for i in range(0, len(texts), self._batch_size)]
        return _l2_normalize(np.vstack(out))

    def _embed_batch(self, batch: list[str]) -> np.ndarray:
        """单批请求 + 重试。响应 data 按 index 排序后取 embedding（服务端不保证有序）。"""
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
                return np.array([d["embedding"] for d in data], dtype=np.float32)
            except (httpx.HTTPError, KeyError, ValueError) as e:
                last_err = e
                if attempt < self._max_retries:
                    time.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"云端嵌入不可用（{self.model_name}）：{last_err}") from last_err


#: dim 探测用的一条无害短文本（避免空列表边界）
_PROBE_TEXT = "dim"


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    """逐行 L2 归一化；零向量（空输入占位）除零保护返回原值。"""
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return v / norms
