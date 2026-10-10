"""启动预检：确保 Docker 依赖服务（Milvus）就绪，未起则自动拉起。

REPL 装配前调用 `_ensure_services`：对依赖服务端点做端口连通探测（socket，秒级）
→ 不可达则定位 docker-compose.yml 执行 ``docker compose up -d`` 并轮询至健康。
软依赖语义：任何失败（无 docker / compose 文件缺失 / compose 失败 / 等待超时）
都只产出警告文本，不抛异常、不阻塞启动——服务缺席时 RAG 检索与 PDF 解析自动
降级（降级逻辑在 rag/vision 层）。警告与进度文本经返回值/notify 回调交由
`main()` 呈现，本模块自身不打印。跳过条件：非 TTY（管道/CI/测试）或环境变量
``PAPERFLOW_SKIP_BOOTSTRAP=1``——静默返回，连端口探测都不执行。
"""
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from paperflow.config import PaperFlowConfig


# REPL 装配前调用 _ensure_services：对依赖服务端点做端口连通探测（socket，秒级）
# → 不可达则定位 docker-compose.yml 执行 `docker compose up -d` 并轮询至健康。
# 软依赖语义：任何失败（无 docker / compose 文件缺失 / compose 失败 / 等待超时）
# 都只产出警告文本，不抛异常、不阻塞启动——服务缺席时 RAG 检索与 PDF 解析自动
# 降级（降级逻辑在 rag/vision 层）。警告与进度文本经返回值/notify 回调交由
# main() 呈现，本段自身不打印。跳过条件：非 TTY（管道/CI/测试）或环境变量
# PAPERFLOW_SKIP_BOOTSTRAP=1——静默返回，连端口探测都不执行。

#: 等待服务健康的总时长：覆盖 Milvus healthcheck 的 start_period 90s + 余量
_WAIT_TIMEOUT_S = 150.0
_POLL_INTERVAL_S = 2.0       # 端口轮询间隔
_PROBE_TIMEOUT_S = 1.0       # 单次端口连通探测超时
_COMPOSE_TIMEOUT_S = 600.0   # docker compose up -d 上限（首启可能拉镜像）

_DEGRADE_NOTE = "RAG 检索功能降级，REPL 仍可正常使用"
_NO_DOCKER_WARN = f"未检测到 docker，无法自动拉起依赖服务（Milvus）；{_DEGRADE_NOTE}"
_NO_COMPOSE_WARN = f"未找到 docker-compose.yml（当前目录与安装目录均无），无法自动拉起依赖服务；{_DEGRADE_NOTE}"


def _host_port(url: str) -> tuple[str, int]:
    """从服务 URL 提取 (host, port)；未显式写端口时按 http/https 语义补全。

    Args:
        url: str，服务 URL

    Returns:
        (host, port)；未显式写端口时按 http/https 语义补全（80/443）。
    """
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def _port_open(host: str, port: int, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """TCP 连通探测：不假设 HTTP 健康路径，端口能建立连接即视为服务在。

    Args:
        host: str，主机名
        port: int，端口
        timeout: float，连接超时（秒）

    Returns:
        True 表示 TCP 连接可建立（不假设 HTTP 健康路径）。
    """
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
    （取 stderr 尾部几行——compose 的报错信息都在输出末尾）。

    Args:
        compose_dir: Path，docker-compose.yml 所在目录

    Returns:
        成功返回 None；失败返回带原因的描述文本（取 stderr 尾部几行）。
    """
    try:
        proc = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=compose_dir, capture_output=True, text=True,
            timeout=_COMPOSE_TIMEOUT_S)
    except OSError as e:
        return f"docker 命令无法执行（不存在或不可用）：{e}"
    except subprocess.TimeoutExpired:
        return f"docker compose up -d 超时（{_COMPOSE_TIMEOUT_S:.0f}s）"
    if proc.returncode != 0:
        output = (proc.stderr or proc.stdout or "").strip().splitlines()
        if not output:
            output = ["无错误输出"]
        return "docker compose up -d 失败：" + "；".join(output[-3:])
    return None


def _wait_healthy(endpoints: list[tuple[str, str, int]],
                  timeout_s: float, poll_interval_s: float
                  ) -> list[tuple[str, str, int]]:
    """轮询直至全部端口可达或超时，返回仍未就绪的 (名称, host, port) 列表。

    Args:
        endpoints: list[tuple[str, str, int]]，(名称, host, port) 端点表
        timeout_s: float，总等待上限（秒）
        poll_interval_s: float，轮询间隔（秒）

    Returns:
        超时后仍未就绪的 (名称, host, port) 列表（全就绪为空列表）。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pending = [(n, h, p) for n, h, p in endpoints if not _port_open(h, p)]
        if not pending:
            return []
        time.sleep(poll_interval_s)
    return [(n, h, p) for n, h, p in endpoints if not _port_open(h, p)]


def _probe_app_layer(endpoints: list[tuple[str, str, int]]) -> list[str]:
    """端口可达之后的应用层探活（TCP 连通探测的应用侧互补）。

    只做 TCP 连通探测会漏掉两类故障：容器「端口开了随即 Exited(1)」（etcd TSO
    超时崩溃）与「半健康栈」都能通过预检，此后 RAG 静默降级而无人察觉。
    Milvus 用 pymilvus 语义级连接（list_collections）。
    任何异常只产出警告、绝不抛出——软依赖语义不变。

    Returns:
        警告文本列表（空 = 应用层全部健康）。

    Args:
        endpoints: list[tuple[str, str, int]]，(名称, host, port) 端点表
    """
    warnings: list[str] = []
    for name, host, port in endpoints:
        try:
            if name == "Milvus":
                from pymilvus import MilvusClient
                client = MilvusClient(uri=f"http://{host}:{port}")
                client.list_collections()
                client.close()
        except Exception as e:
            warnings.append(
                f"{name} 端口可达但应用层探活失败（{e}）——服务可能已中途崩溃，"
                f"RAG/PDF 解析将降级。建议：docker compose restart 后重启 paperflow；"
                f"{_DEGRADE_NOTE}")
    return warnings


def _ensure_services(config: PaperFlowConfig, *, is_tty: bool, notify=None,
                     skip: bool = False,
                     wait_timeout_s: float = _WAIT_TIMEOUT_S,
                     poll_interval_s: float = _POLL_INTERVAL_S) -> list[str]:
    """
    启动预检：确保 Docker 依赖服务（Milvus）就绪，未起则自动拉起。

    Args:
        config: 全局配置（读 milvus_uri 端点）。
        is_tty: 是否交互终端——False（管道/CI/测试）直接跳过。
        skip: 显式跳过开关（--skip-bootstrap flag，与环境变量等价）。
        notify: 进度回调（str → None），拉起/等待阶段逐条调用；None 静默。
        wait_timeout_s: 等待服务健康的总时长（默认覆盖 Milvus start_period 90s）。
        poll_interval_s: 端口轮询间隔。

    Returns:
        警告文本列表（空 = 服务全部就绪或预检被跳过）。调用方负责呈现；
        本函数绝不抛异常——软依赖缺席不应阻塞 REPL 启动。
    """
    if not is_tty or skip or os.environ.get("PAPERFLOW_SKIP_BOOTSTRAP") == "1":
        return []

    endpoints = [("Milvus", *_host_port(config.rag.storage.uri))]
    if all(_port_open(h, p) for _, h, p in endpoints):
        return _probe_app_layer(endpoints)      # 端口在 → 应用层语义健康再确认

    if shutil.which("docker") is None:
        return [_NO_DOCKER_WARN]
    compose_dir = _find_compose_dir()
    if compose_dir is None:
        return [_NO_COMPOSE_WARN]
    if notify:
        notify("依赖服务未就绪，正在拉起（docker compose up -d，首次启动较慢）…")
    err = _compose_up(compose_dir)
    if err is not None:
        return [f"{err}；{_DEGRADE_NOTE}"]
    if notify:
        notify(f"等待服务健康（最长 {wait_timeout_s:.0f}s）…")

    warnings = []
    not_ready = _wait_healthy(endpoints, wait_timeout_s, poll_interval_s)
    for name, host, port in not_ready:
        warnings.append(
            f"{name} 服务未在 {wait_timeout_s:.0f}s 内就绪（{host}:{port}）；{_DEGRADE_NOTE}")
    # 端口就绪的子集再做应用层探活（未就绪的不重复报）
    ready = [(n, h, p) for n, h, p in endpoints if (n, h, p) not in not_ready]
    warnings.extend(_probe_app_layer(ready))
    return warnings
