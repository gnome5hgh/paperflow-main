# paperflow/core/intent/routing/vector_cache.py
"""路由语料稠密向量落盘缓存——启动 20s 重编码的解药（spec 2026-10-05 §4）。

routes.yaml 的 1684 条 utterance 是静态知识资产，向量内容只取决于
（模型, 维度, 语料），每次启动重算纯属浪费。缓存键 = sha256(模型 + 维度 +
按序语料)，语料/模型/维度任一变更自动失效。命中即零网络启动；未命中由
HybridRouter 现算后回写。
"""
import hashlib
import tempfile
from pathlib import Path

import numpy as np

#: npz 内字段名（防外部误读；save/load 成对约定）
_KEY_FIELD = "cache_key"
_MODEL_FIELD = "model"
_DENSE_FIELD = "dense"


def cache_key(model_name: str, dim: int, utterances: list[str]) -> str:
    """内容寻址键：任一要素变更即失效。顺序敏感（索引按序对位）。"""
    h = hashlib.sha256()
    h.update(model_name.encode())
    h.update(str(dim).encode())
    for u in utterances:
        h.update(b"\x00")       # 分隔符防拼接歧义（"ab","c" vs "a","bc"）
        h.update(u.encode(errors="replace"))
    return h.hexdigest()


def load_cached_dense(path: Path, key: str) -> np.ndarray | None:
    """读缓存；文件缺失/损坏/键不符一律 None（调用方走现算，绝不抛）。"""
    if not Path(path).is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            if str(data[_KEY_FIELD]) != key:
                return None
            return np.asarray(data[_DENSE_FIELD], dtype=np.float32)
    except Exception:
        return None


def save_cached_dense(path: Path, key: str, model_name: str, dense: np.ndarray) -> None:
    """原子写缓存：tmp + rename，进程中断不留半文件。写失败只告警不抛——
    缓存是纯加速层，写不进大不了下次重算。"""
    import logging
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp",
                                         delete=False) as tmp:
            np.savez(tmp, **{_KEY_FIELD: key, _MODEL_FIELD: model_name,
                             _DENSE_FIELD: np.asarray(dense, dtype=np.float32)})
            tmp_name = tmp.name
        Path(tmp_name).replace(path)
    except Exception as e:
        logging.getLogger(__name__).warning("路由向量缓存写入失败（忽略，下次重算）：%s", e)
