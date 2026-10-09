# paperflow/core/intent/services/jev.py
"""判定服务客户端——经网关的 Decision 模态调一次「从候选类别里选一个」。

**这是意图层唯一依赖外部服务的地方**，因此它的每一条失败路径都必须有明确去向：
探测失败 → 意图层不装配（见 `cli.py`）；调用失败 → 退回规则层，规则层也没命中就
不产块。客户端绝不抛进 ReAct 循环。

网关形态（与厂商自家的 HTTP 形态不同，别照厂商文档写）：`POST {base_url}/evaluate`，
body 为 `state`（共享状态文本）+ `questions`（每个问题一个类型与说明），答
`answers.<问题名>.choice` 与 `probabilities`。**归一化后的 choice 答案只回
`probabilities`，不回厂商原生的 `confidence`**，所以置信度取选中项的概率。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from paperflow.core.security.text import sanitize_surrogates

logger = logging.getLogger(__name__)

#: 指数退避基数（秒）：第 attempt 次重试前 sleep(BASE * 2 ** attempt)。
#: 与云端编码/精排客户端同一语义——只为可恢复错误退避，4xx 立即放弃。
RETRY_BACKOFF_BASE = 0.5

#: 判定问题的类型：从 criteria 的候选里选一个（网关的 choice 原语）
_QUESTION_TYPE = "choice"

#: 问题名（答案按它索引回来）
_QUESTION_KEY = "intent"

#: 判定口径：告诉模型「判什么」——把注意力钉在最后一条用户消息上，
#: 否则它会去总结整段对话。
_INSTRUCTIONS = (
    "下面是一段学术助手与用户的对话。请判断**最后一条用户消息**属于哪一类工作。"
    "只按它的诉求归类，不要按前面几轮的内容归类。"
)

#: 启动探测用的最小问题（两个选项，够判断链路是否通；不参与真实判定）
_PROBE_CRITERIA = {"ok": "这是一次连通性探测", "other": "其他任何情况"}


class JevUnavailable(RuntimeError):
    """判定服务不可用。`reason` 已是可以直接呈现给用户的中文短句。

    三种常见原因必须能区分开——它们的处置完全不同，统一写成「不可达」会让人
    去查网络而实际是账户没绑卡。
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class JevDecision:
    """一次判定的结果。

    Attributes:
        choice: str，模型选中的类别名（调用方负责校验它是已知类别）。
        confidence: float | None，选中项的概率。**没有判定消费者**，只作展示与排查。
    """

    choice: str
    confidence: float | None


def _reason_for_status(status: int, detail: str) -> str:
    """把 HTTP 状态翻成可行动的中文原因。

    Args:
        status: int，响应状态码。
        detail: str，响应体摘要（截断后拼进原因，便于排障）。

    Returns:
        中文原因短句。
    """
    if status == 401:
        return f"网关拒绝了 API key（401）{detail}"
    if status == 403:
        return (f"账户未完成验证（403）——该网关要求账户先绑定信用卡，"
                f"连免费额度也要先绑{detail}")
    if status == 404:
        return f"端点或模型不存在（404）{detail}"
    if status == 429:
        return f"被限流（429）{detail}"
    if status >= 500:
        return f"服务端错误（{status}）{detail}"
    return f"请求被拒（{status}）{detail}"


def _brief(response: httpx.Response) -> str:
    """摘一小段响应体用作原因后缀（不把整页塞进错误信息）。"""
    text = (response.text or "").strip().replace("\n", " ")
    return f"：{text[:160]}" if text else ""


class JevClient:
    """Decision 模态的最小客户端：一次请求判定一个类别；探测另有一个入口。

    Attributes:
        _base_url: str，网关地址（不含尾斜杠）
        _api_key: str，网关 key
        _model: str，模型名
        _timeout: float，单次调用读超时（秒）
        _max_retries: int，可恢复错误的重试次数
        _zero_data_retention: bool，是否要求零数据保留
        _only_provider: str，钉住的服务提供方（空 = 不钉）
        _transport: httpx.BaseTransport | None，测试注入点（MockTransport），生产为 None
    """

    def __init__(self, base_url: str, api_key: str, model: str, *,
                 timeout: float = 5.0, max_retries: int = 1,
                 zero_data_retention: bool = True, only_provider: str = "",
                 transport: httpx.BaseTransport | None = None):
        """记录连接参数（此时不建连接、不发起请求）。

        Args:
            base_url: str，网关地址（如 `https://ai-gateway.vercel.sh/v1`）
            api_key: str，网关 key；留空时探测会直接给出「未配置」
            model: str，模型名（`<厂商>/<模型>`）
            timeout: float，单次调用读超时（秒）
            max_retries: int，可恢复错误的重试次数
            zero_data_retention: bool，是否要求零数据保留
            only_provider: str，钉住的服务提供方（空 = 不钉）
            transport: httpx.BaseTransport | None，测试注入的假传输（生产 None）
        """
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._zero_data_retention = zero_data_retention
        self._only_provider = only_provider
        self._transport = transport

    async def decide(self, state: str, criteria: dict[str, str],
                     instructions: str = _INSTRUCTIONS) -> JevDecision | None:
        """判定一次；任何失败都记日志并返回 None（调用方退回规则层）。

        Args:
            state: str，共享状态文本（对话史 + 本轮输入）。
            criteria: dict[str, str]，候选类别 → 该类的判定口径。
            instructions: str，判定口径说明（问什么）。

        Returns:
            JevDecision；服务不可用、响应无法解析、或模型给出的类别不在候选里时
            返回 None（**绝不猜测**）。
        """
        try:
            payload = await self._evaluate(state, criteria, instructions)
        except JevUnavailable as exc:
            logger.warning("判定服务调用失败，退回规则层：%s", exc.reason)
            return None
        answers = payload.get("answers") if isinstance(payload, dict) else None
        answer = (answers or {}).get(_QUESTION_KEY) or {}
        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in criteria:
            logger.warning("判定服务返回了未知类别（%r），按无结果处理", choice)
            return None
        probabilities = answer.get("probabilities") or {}
        raw = probabilities.get(choice) if isinstance(probabilities, dict) else None
        confidence = float(raw) if isinstance(raw, (int, float)) else None
        return JevDecision(choice=choice, confidence=confidence)

    async def probe(self) -> None:
        """启动探测：发一个最小判定请求，确认「key 能用 + 账户已验证 + 端点可达」。

        探测会真的调一次判定（网关的验证状态只有推理端点才检查，列模型的端点不查
        ——用免费端点探测会把「账户没绑卡」误判成可用）。一次探测的输入 token 极少，
        成本可忽略。

        Raises:
            JevUnavailable: 不可用；`reason` 指出具体原因（未配置 / key 无效 /
                账户未验证 / 网络不可达 …）。
        """
        if not self._api_key:
            raise JevUnavailable("未配置网关 api_key（config.yaml 的 intent.jev.api_key）")
        if not self._base_url:
            raise JevUnavailable("未配置网关地址（config.yaml 的 intent.jev.base_url）")
        await self._evaluate("探测：这是一次连通性检查。", _PROBE_CRITERIA,
                             "这是一次连通性探测，请任选一项。")

    # ── 内部：请求与重试 ───────────────────────────────────────────────

    async def _evaluate(self, state: str, criteria: dict[str, str],
                        instructions: str) -> dict:
        """发一次 `/evaluate` 并返回响应 JSON；失败抛 JevUnavailable。

        每次调用新建一个 `AsyncClient`：客户端不做跨调用复用，是因为探测发生在
        CLI 启动（`asyncio.run` 的临时循环）而真实判定发生在 REPL 的主循环里——
        跨事件循环复用连接池是错的，而这个层每轮最多一次调用，重连成本可忽略。

        Args:
            state: str，共享状态文本。
            criteria: dict[str, str]，候选 → 口径。
            instructions: str，问什么。

        Returns:
            解析后的响应 dict。

        Raises:
            JevUnavailable: 连接失败、超时、状态码非 2xx、或响应不是 JSON 对象。
        """
        body: dict = {
            "model": self._model,
            "state": sanitize_surrogates(state),
            "questions": {
                _QUESTION_KEY: {
                    "type": _QUESTION_TYPE,
                    "instructions": instructions,
                    "criteria": criteria,
                },
            },
        }
        # 数据与提供方选项：零保留是本层的既定要求（发出去的是用户对话史）。
        # **被服务端拒绝时按不可用处理，绝不摘掉这一项重试**——要不要在无保留
        # 保证的前提下发送对话史是隐私决定，不能由代码替用户默认答应。
        gateway_options: dict = {}
        if self._zero_data_retention:
            gateway_options["zeroDataRetention"] = True
        if self._only_provider:
            gateway_options["only"] = [self._only_provider]
        if gateway_options:
            body["providerOptions"] = {"gateway": gateway_options}

        last_reason = "未知错误"
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(
                        base_url=self._base_url, timeout=self._timeout,
                        transport=self._transport,
                        headers={"Authorization": f"Bearer {self._api_key}"}) as client:
                    response = await client.post("/evaluate", json=body)
                if response.status_code >= 500 or response.status_code == 429:
                    last_reason = _reason_for_status(response.status_code, "")
                    if attempt < self._max_retries:
                        await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                        continue
                    raise JevUnavailable(last_reason)
                if response.status_code >= 400:
                    # 4xx 重试必然同样失败：认证/参数/资格问题都要人去改配置。
                    raise JevUnavailable(_reason_for_status(
                        response.status_code, _brief(response)))
                payload = response.json()
                if not isinstance(payload, dict):
                    raise JevUnavailable("响应不是 JSON 对象")
                return payload
            except httpx.HTTPError as exc:
                last_reason = f"网络不可达（{type(exc).__name__}: {exc}）"
                if attempt < self._max_retries:
                    await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                    continue
                raise JevUnavailable(last_reason) from exc
            except ValueError as exc:                     # JSON 解析失败
                raise JevUnavailable(f"响应无法解析为 JSON（{exc}）") from exc
        raise JevUnavailable(last_reason)
