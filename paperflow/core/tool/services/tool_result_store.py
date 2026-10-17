"""工具结果外置存储：MinIO 主存 + 本地文件回退。

超过阈值的工具结果不再整段塞进 in-context：原文写这里，消息里只留指针。
对象键按会话与 tool_call_id 确定性生成，因此回取不需要额外的元数据表。

MinIO 是软依赖：不可达时回退到工作区本地文件；两者都失败返回 None，由调用方
降级为明确截断的文本。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)

#: 连接失败后的冷却时间（秒）：不可达的存储连一次要好几秒，冷却期内直接回退本地。
_FAILURE_COOLDOWN = 60.0


def _safe_component(value: str) -> str:
    """把会话/调用 ID 归一化成可安全用于路径与对象键的片段。

    tool_call_id 来自模型，不能直接拼进本地路径；非字母数字字符一律替换，
    并去掉首尾的点，避免 ``..`` 逃出目录。

    Args:
        value: 原始标识。

    Returns:
        安全片段；空值回退为 ``unknown``。
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", value or "")
    return safe.strip(".") or "unknown"


def session_key_prefix(session_id: str) -> str:
    """返回某会话工具结果的对象键前缀。

    Args:
        session_id: 会话标识。

    Returns:
        ``sessions/<safe-session>/tool-results/``。
    """
    return f"sessions/{_safe_component(session_id)}/tool-results/"


def object_key(session_id: str, tool_call_id: str) -> str:
    """返回工具结果在对象存储里的键。

    Args:
        session_id: 会话标识。
        tool_call_id: 工具调用 ID。

    Returns:
        确定性对象键（同一会话同一调用始终相同）。
    """
    return f"{session_key_prefix(session_id)}{_safe_component(tool_call_id)}.txt"


class ToolResultStore:
    """工具结果存档：先写 MinIO，失败回退本地文件。

    Attributes:
        _minio_config: MinioConfig，连接配置
        _bucket: str，桶名
        _local_dir: Path，本地回退根目录
        _client: Minio | None，惰性构造的客户端
        _failed_at: float，上次失败时刻（0 = 没失败过），用于冷却
        _lock: threading.Lock，保护惰性构造
    """

    def __init__(self, minio_config, bucket: str, local_dir: str | Path):
        """绑定连接配置与本地回退目录。

        Args:
            minio_config: MinioConfig，MinIO 连接配置。
            bucket: str，桶名（与图表原图分开）。
            local_dir: str | Path，本地回退根目录。
        """
        self._minio_config = minio_config
        self._bucket = bucket
        self._local_dir = Path(local_dir)
        self._client = None
        self._failed_at = 0.0
        self._lock = threading.Lock()

    def _get_client(self):
        """惰性构造 MinIO 客户端并自举桶；不可用时返回 None（带冷却）。

        Returns:
            Minio | None，可用客户端。
        """
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            if self._failed_at and time.monotonic() - self._failed_at < _FAILURE_COOLDOWN:
                return None
            try:
                from minio import Minio

                cfg = self._minio_config
                client = Minio(
                    cfg.endpoint,
                    access_key=cfg.access_key,
                    secret_key=cfg.secret_key,
                    secure=cfg.secure,
                )
                if not client.bucket_exists(self._bucket):
                    client.make_bucket(self._bucket)
                self._client = client
                return client
            except Exception as exc:
                self._failed_at = time.monotonic()
                logger.warning("工具结果对象存储不可用，回退本地: %r", exc)
                return None

    def put(self, session_id: str, tool_call_id: str, text: str
            ) -> tuple[str, str] | None:
        """写入工具结果原文。

        Args:
            session_id: 会话标识。
            tool_call_id: 工具调用 ID。
            text: 原始结果文本。

        Returns:
            (archive_key, local_path)；MinIO 成功时 archive_key 非空、local_path 为空；
            回退本地时相反；两者都失败返回 None。
        """
        key = object_key(session_id, tool_call_id)
        client = self._get_client()
        if client is not None:
            try:
                data = text.encode("utf-8")
                client.put_object(
                    self._bucket, key, BytesIO(data), len(data),
                    content_type="text/plain; charset=utf-8")
                return key, str(self._local_path(session_id, tool_call_id))
            except Exception as exc:
                self._failed_at = time.monotonic()
                logger.warning("工具结果写 MinIO 失败，回退本地: %r", exc)
        local_path = self._local_path(session_id, tool_call_id)
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(text, encoding="utf-8")
            return "", str(local_path)
        except OSError as exc:
            logger.warning("工具结果写本地失败: %r", exc)
            return None

    def get(self, session_id: str, tool_call_id: str) -> str | None:
        """按会话与 tool_call_id 取回原文（先 MinIO，后本地）。

        Args:
            session_id: 会话标识。
            tool_call_id: 工具调用 ID。

        Returns:
            原文；未找到或存储不可用时为 None。
        """
        text = self.get_key(object_key(session_id, tool_call_id))
        if text is not None:
            return text
        try:
            return self._local_path(session_id, tool_call_id).read_text(encoding="utf-8")
        except OSError:
            return None

    def get_key(self, key: str) -> str | None:
        """按对象键取回原文。

        Args:
            key: 对象键。

        Returns:
            原文；未找到或存储不可用时为 None。
        """
        client = self._get_client()
        if client is None:
            return None
        try:
            response = client.get_object(self._bucket, key)
            try:
                return response.read().decode("utf-8", errors="replace")
            finally:
                response.close()
                response.release_conn()
        except Exception as exc:
            logger.debug("工具结果 MinIO 读取未命中: %r", exc)
            return None

    def _local_path(self, session_id: str, tool_call_id: str) -> Path:
        """返回本地回退路径。

        Args:
            session_id: 会话标识。
            tool_call_id: 工具调用 ID。

        Returns:
            本地文件路径。
        """
        return self._local_dir / _safe_component(session_id) / (
            f"{_safe_component(tool_call_id)}.txt")


def make_tool_result_store(config) -> ToolResultStore:
    """按配置构造工具结果存档。

    Args:
        config: PaperFlowConfig，读取 rag.storage.minio、compaction.tool_results
            与 runtime.workspace。

    Returns:
        配置好的 ToolResultStore。
    """
    settings = config.compaction.tool_results
    local_dir = settings.local_dir or str(
        Path(config.runtime.workspace) / "tool-results")
    return ToolResultStore(config.rag.storage.minio, settings.bucket, local_dir)
