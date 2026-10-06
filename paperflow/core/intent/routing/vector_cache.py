# paperflow/core/intent/routing/vector_cache.py
"""路由语料稠密向量落盘缓存——消除每次启动对静态语料的重编码。

routes.yaml 的 1684 条 utterance 是静态知识资产，向量内容只取决于
（模型, 维度, 语料），每次启动重算纯属浪费。缓存键 = sha256(模型 + 维度 +
按序语料)，语料/模型/维度任一变更自动失效。命中即零网络启动；未命中由
HybridRouter 现算后回写。
"""
import hashlib
import os
import tempfile
from pathlib import Path

import numpy as np

#: npz 内字段名（防外部误读；save/load 成对约定）
_KEY_FIELD = "cache_key"
_MODEL_FIELD = "model"
_DENSE_FIELD = "dense"
_DIM_FIELD = "dim"


def cache_key(model_name: str, dim: int, utterances: list[str]) -> str:
    """内容寻址键：任一要素变更即失效。顺序敏感（索引按序对位）。

    Args:
        model_name: 编码模型名。
        dim: 向量维度。
        utterances: 参与编码的语料列表（按序）。

    Returns:
        sha256 十六进制缓存键。
    """
    h = hashlib.sha256()
    h.update(model_name.encode())
    h.update(str(dim).encode())
    for u in utterances:
        h.update(b"\x00")       # 分隔符防拼接歧义（"ab","c" vs "a","bc"）
        h.update(u.encode(errors="replace"))
    return h.hexdigest()


def load_cached_dense(path: Path, key: str) -> np.ndarray | None:
    """读缓存；文件缺失/损坏/键不符/dim 与矩阵不一致一律 None（调用方走现算，绝不抛）。

    Args:
        path: 缓存文件路径（npz）。
        key: 期望的缓存键（cache_key 的产出）。

    Returns:
        稠密向量矩阵 (n, dim)；缓存不可用时 None。
    """
    if not Path(path).is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            if str(data[_KEY_FIELD]) != key:
                return None
            dense = np.asarray(data[_DENSE_FIELD], dtype=np.float32)
            # 显式 dim 字段必须与矩阵第二维一致——不一致说明文件被
            # 篡改/字段错位，宁可失效重算也不把形状可疑的向量灌进索引。
            if int(data[_DIM_FIELD]) != dense.shape[1]:
                return None
            return dense
    except Exception:
        return None


def save_cached_dense(path: Path, key: str, model_name: str, dense: np.ndarray) -> None:
    """原子写缓存：tmp + rename，进程中断不留半文件。写失败只告警不抛——
    缓存是纯加速层，写不进大不了下次重算。

    Args:
        path: 缓存文件路径（npz）。
        key: 缓存键（cache_key 的产出）。
        model_name: 编码模型名（诊断字段，随文件存一份）。
        dense: 稠密向量矩阵 (n, dim)。
    """
    import logging
    path = Path(path)
    dense = np.asarray(dense, dtype=np.float32)
    try:
        # mkdir 一并纳入 try：安装根只读时创建目录会抛，缓存写失败只应告警，
        # 不得让保存动作把启动打断（与本函数 docstring 的语义一致）。
        os.makedirs(path.parent, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp",
                                         delete=False) as tmp:
            np.savez(tmp, **{_KEY_FIELD: key, _MODEL_FIELD: model_name,
                             _DIM_FIELD: int(dense.shape[1]),
                             _DENSE_FIELD: dense})
            tmp_name = tmp.name
        Path(tmp_name).replace(path)
    except Exception as e:
        logging.getLogger(__name__).warning("路由向量缓存写入失败（忽略，下次重算）：%s", e)
