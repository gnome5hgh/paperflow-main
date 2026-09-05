# paperflow/bootstrap.py
"""启动预检：探测 Docker 依赖服务（Milvus/GROBID），未就绪时自动拉起。

REPL 装配前调用 ensure_services：先对两个服务端点做端口连通探测（socket，
秒级）→ 不可达则定位 docker-compose.yml 执行 `docker compose up -d` 并轮询
至健康。软依赖语义：任何失败（无 docker / compose 文件缺失 / compose 失败 /
等待超时）都只产出警告文本，不抛异常、不阻塞启动——服务缺席时 RAG 检索与
PDF 解析自动降级（降级逻辑在 rag/vision 层，与本模块无关）。

纯逻辑模块：仅标准库依赖；警告与进度文本经返回值/notify 回调交给调用方
（cli.main）呈现——本模块不 import rich/terminal，保证单测轻量。

跳过条件：非 TTY（管道/CI/测试）或环境变量 PAPERFLOW_SKIP_BOOTSTRAP=1——
静默返回，连端口探测都不执行。
"""
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from paperflow.config import PaperFlowConfig

#: 等待服务健康的总时长：覆盖 Milvus healthcheck 的 start_period 90s + 余量
_WAIT_TIMEOUT_S = 150.0
_POLL_INTERVAL_S = 2.0       # 端口轮询间隔
_PROBE_TIMEOUT_S = 1.0       # 单次端口连通探测超时
_COMPOSE_TIMEOUT_S = 600.0   # docker compose up -d 上限（首启可能拉镜像）

_DEGRADE_NOTE = "RAG/PDF 解析功能降级，REPL 仍可正常使用"
_NO_DOCKER_WARN = f"未检测到 docker，无法自动拉起依赖服务（Milvus/GROBID）；{_DEGRADE_NOTE}"
_NO_COMPOSE_WARN = f"未找到 docker-compose.yml（当前目录与安装目录均无），无法自动拉起依赖服务；{_DEGRADE_NOTE}"
_GROBID_RUNBOOK_HINT = "若为首次启动，需先初始化 grobid-home，见 docs/测试指南/问题排查手册.md"


def _host_port(url: str) -> tuple[str, int]:
    """从服务 URL 提取 (host, port)；未显式写端口时按 http/https 语义补全。"""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def _port_open(host: str, port: int, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """TCP 连通探测：不假设 HTTP 健康路径，端口能建立连接即视为服务在。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_compose_dir() -> Path | None:
    """定位 docker-compose.yml 所在目录：优先当前工作目录，回退安装根
    （editable 安装后在任意目录启动也能找到仓库内的 compose 文件）。"""
    for base in (Path.cwd(), Path(__file__).resolve().parents[1]):
        if any((base / name).is_file()
               for name in ("docker-compose.yml", "docker-compose.yaml")):
            return base
    return None


def _compose_up(compose_dir: Path) -> str | None:
    """执行 docker compose up -d。成功返回 None；失败返回带原因的描述
    （取 stderr 尾部几行——compose 的报错信息都在输出末尾）。"""
    try:
        proc = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=compose_dir, capture_output=True, text=True,
            timeout=_COMPOSE_TIMEOUT_S)
    except FileNotFoundError:
        return "docker 命令无法执行（不存在或不可用）"
    except subprocess.TimeoutExpired:
        return f"docker compose up -d 超时（{_COMPOSE_TIMEOUT_S:.0f}s）"
    if proc.returncode != 0:
        output = (proc.stderr or proc.stdout or "").strip().splitlines()
        return "docker compose up -d 失败：" + "；".join(output[-3:])
    return None


def _wait_healthy(endpoints: list[tuple[str, str, int]],
                  timeout_s: float, poll_interval_s: float
                  ) -> list[tuple[str, str, int]]:
    """轮询直至全部端口可达或超时，返回仍未就绪的 (名称, host, port) 列表。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pending = [(n, h, p) for n, h, p in endpoints if not _port_open(h, p)]
        if not pending:
            return []
        time.sleep(poll_interval_s)
    return [(n, h, p) for n, h, p in endpoints if not _port_open(h, p)]


def ensure_services(config: PaperFlowConfig, *, is_tty: bool, notify=None,
                    wait_timeout_s: float = _WAIT_TIMEOUT_S,
                    poll_interval_s: float = _POLL_INTERVAL_S) -> list[str]:
    """
    启动预检：确保 Docker 依赖服务（Milvus/GROBID）就绪，未起则自动拉起。

    Args:
        config: 全局配置（读 milvus_uri / grobid_endpoint 两个端点）。
        is_tty: 是否交互终端——False（管道/CI/测试）直接跳过。
        notify: 进度回调（str → None），拉起/等待阶段逐条调用；None 静默。
        wait_timeout_s: 等待服务健康的总时长（默认覆盖 Milvus start_period 90s）。
        poll_interval_s: 端口轮询间隔。

    Returns:
        警告文本列表（空 = 服务全部就绪或预检被跳过）。调用方负责呈现；
        本模块绝不抛异常——软依赖缺席不应阻塞 REPL 启动。
    """
    if not is_tty or os.environ.get("PAPERFLOW_SKIP_BOOTSTRAP") == "1":
        return []

    endpoints = [
        ("Milvus", *_host_port(config.milvus_uri)),
        ("GROBID", *_host_port(config.grobid_endpoint)),
    ]
    if all(_port_open(h, p) for _, h, p in endpoints):
        return []

    if notify:
        notify("依赖服务未就绪，正在拉起（docker compose up -d，首次启动较慢）…")
    if shutil.which("docker") is None:
        return [_NO_DOCKER_WARN]
    compose_dir = _find_compose_dir()
    if compose_dir is None:
        return [_NO_COMPOSE_WARN]
    err = _compose_up(compose_dir)
    if err is not None:
        return [f"{err}；{_DEGRADE_NOTE}"]
    if notify:
        notify(f"等待服务健康（最长 {wait_timeout_s:.0f}s）…")

    warnings = []
    for name, host, port in _wait_healthy(endpoints, wait_timeout_s,
                                          poll_interval_s):
        w = f"{name} 服务未在 {wait_timeout_s:.0f}s 内就绪（{host}:{port}）；{_DEGRADE_NOTE}"
        if name == "GROBID":
            w += f"；{_GROBID_RUNBOOK_HINT}"
        warnings.append(w)
    return warnings
