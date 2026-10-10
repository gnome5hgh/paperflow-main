"""图表原图的对象存储：把块的图区域存进 MinIO，按对象键取回。

依赖栈里的 MinIO 本来只服务向量库，这里给它开一个**自己的 bucket** 放图表原图——
块上只留对象键，图字节不进向量库（向量库塞 blob 会拖慢读写），也不落在工作区
（这样换机器仍能取到图）。键用块 id，天然幂等覆盖。

**全部方法是软依赖**：存储不可达时只记一条告警、返回失败，绝不抛——图是加分项，
它挂了不该让文本索引或检索崩掉。要不要存由 `rag.store_images` 开关决定，关掉时
用空实现（调用方不必处处判断）。
"""
from __future__ import annotations

import io
import logging
import threading
import time
from typing import Protocol

logger = logging.getLogger(__name__)

#: 图表原图的 MIME：渲染固定产 PNG。
_CONTENT_TYPE = "image/png"

#: 连接失败后的冷却时间（秒）。连一次不可达的存储要好几秒（SDK 内部会重试），而索引
#: 一篇论文可能有十几张图——不记这笔账就会按图重复等，一篇就白等一分钟。冷却期内直接
#: 放弃，不再触网。与检索侧熔断器同一思路（冷却 60s 后放一次探测）。
_FAILURE_COOLDOWN = 60.0


class ImageStore(Protocol):
    """图表原图的存取接口（生产用对象存储，测试用内存替身）。

    Attributes:
        enabled: bool，这个存取器是否真的会存图。空实现为 False——调用方据此决定
            要不要去做「渲染 + 上传」这串昂贵动作（只问这个，不必再翻配置开关）。
    """

    enabled: bool

    def put(self, key: str, data: bytes) -> bool:
        """存入一张图。

        Args:
            key: 对象键（用块 id）。
            data: PNG 字节。

        Returns:
            bool: 是否存入成功。
        """
        ...

    def get(self, key: str) -> bytes | None:
        """按键取回一张图。

        Args:
            key: 对象键。

        Returns:
            bytes | None: 图片字节；对象不存在或存储不可用时为 None。
        """
        ...

    def remove(self, key: str) -> None:
        """按键删除一张图（对象不存在也算成功）。

        Args:
            key: 对象键。
        """
        ...


class NullImageStore:
    """不存图的空实现（`rag.store_images` 关掉时用）。"""

    #: 明确不存图：调用方看到它就跳过渲染与上传。
    enabled = False

    def put(self, key: str, data: bytes) -> bool:
        """什么都不做（开关关掉 = 明确不存图，不是失败）。"""
        return False

    def get(self, key: str) -> bytes | None:
        """永远取不到图。"""
        return None

    def remove(self, key: str) -> None:
        """没有对象可删。"""
        return


class MinioImageStore:
    """MinIO 实现：客户端与 bucket 都在首次用时惰性建立。

    Attributes:
        _config: MinioConfig，连接与 bucket 名
        _client: Minio | None，惰性构造的客户端
        _bucket_ready: bool，bucket 是否已确认存在（只自举一次）
        _failed_at: float，上次连接失败的时刻（0 = 没失败过），用于冷却
        _lock: threading.Lock，保护惰性构造（并发索引时会被多个线程碰到）
    """

    #: 会真的存图（是否连得上另说——连不上由各方法降级）。
    enabled = True

    def __init__(self, config):
        """绑定连接配置；真正的客户端留到首次使用时构造。

        Args:
            config: MinioConfig 实例。
        """
        self._config = config
        self._client = None
        self._bucket_ready = False
        self._failed_at = 0.0
        self._lock = threading.Lock()

    def _ensure_client(self):
        """惰性构造 MinIO 客户端并自举 bucket；失败返回 None（调用方降级）。

        失败后会进入冷却期：期内的调用直接返回 None、不再触网，避免「按图重复等连接超时」。

        Returns:
            object | None: 可用的客户端；依赖缺失、连接失败或在冷却期内时为 None。
        """
        with self._lock:
            if self._client is not None and self._bucket_ready:
                return self._client
            if self._failed_at and time.monotonic() - self._failed_at < _FAILURE_COOLDOWN:
                return None                      # 冷却期内不再重试
            try:
                from minio import Minio
                if self._client is None:
                    self._client = Minio(
                        self._config.endpoint,
                        access_key=self._config.access_key,
                        secret_key=self._config.secret_key,
                        secure=self._config.secure,
                    )
                if not self._client.bucket_exists(self._config.bucket):
                    self._client.make_bucket(self._config.bucket)
                self._bucket_ready = True
                self._failed_at = 0.0
                return self._client
            except Exception as e:
                # 软依赖：存储没起/凭证不对/依赖缺失都只告警，绝不让索引或检索崩
                self._failed_at = time.monotonic()
                logger.warning("对象存储不可用（%s）：%s", self._config.endpoint, e)
                return None

    def put(self, key: str, data: bytes) -> bool:
        """把一张图存进 bucket。

        Args:
            key: 对象键（用块 id）。
            data: PNG 字节。

        Returns:
            bool: 是否存入成功（失败只告警）。
        """
        if not data:
            return False
        client = self._ensure_client()
        if client is None:
            return False
        try:
            client.put_object(self._config.bucket, key, io.BytesIO(data),
                              length=len(data), content_type=_CONTENT_TYPE)
            return True
        except Exception as e:
            logger.warning("图表原图入库失败（%s）：%s", key, e)
            return False

    def get(self, key: str) -> bytes | None:
        """按键取回一张图。

        Args:
            key: 对象键。

        Returns:
            bytes | None: 图片字节；对象不存在或存储不可用时为 None。
        """
        client = self._ensure_client()
        if client is None:
            return None
        response = None
        try:
            response = client.get_object(self._config.bucket, key)
            return response.read()
        except Exception as e:
            # 不存在与存储故障在这里不作区分：调用方拿到 None 都按「图不可用」处理
            logger.warning("图表原图取回失败（%s）：%s", key, e)
            return None
        finally:
            if response is not None:
                try:
                    response.close()
                    response.release_conn()
                except Exception:
                    pass

    def remove(self, key: str) -> None:
        """按键删除一张图（不存在或失败都只告警）。

        Args:
            key: 对象键。
        """
        client = self._ensure_client()
        if client is None:
            return
        try:
            client.remove_object(self._config.bucket, key)
        except Exception as e:
            logger.warning("图表原图删除失败（%s）：%s", key, e)


def make_image_store(config) -> ImageStore:
    """按配置造一个存取器：关掉开关给空实现，否则给对象存储实现。

    Args:
        config: PaperFlowConfig 实例。

    Returns:
        ImageStore: 存取器（空实现或对象存储实现）。
    """
    if not getattr(config.rag, "store_images", False):
        return NullImageStore()
    return MinioImageStore(config.rag.storage.minio)
