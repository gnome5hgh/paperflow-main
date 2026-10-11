# paperflow/core/security/middleware/policy_engine.py
"""
策略引擎中间件：在工具执行前做两级检查。

``before`` 阶段按顺序执行：

1. ``blocked_by_default``：工具被标记为默认禁止，直接抛 ``PolicyDenied``——
   这是不依赖风险档位的硬拒入口（连同工作区路径黑名单，是仅有的两个）；
2. 风险档位：工具 ``risk_level`` 高于会话免问上限 ``confirm_above`` 的调用
   抛 ``ConfirmRequired``，升级为人工确认——**超档不拒绝，只是要用户点头**；
   未知风险档（声明笔误）按最高档处理，fail-safe：只会多问一次，不会静默放行。

风险档位是「问不问」的唯一来源，没有独立的「需要确认」布尔与它并行——
整份判定只有一个比较式，没有任何按档分支。确认行为统一为三态（本次放行 /
本会话同键放行 / 拒绝），确认键为 ``(工具名, 有效目标路径)``：写类工具若只按
工具名确认一次，后续任意路径都会被静默写入，可能误覆盖用户手写笔记，因此
同一工具的不同路径仍需单独确认。
"""

from paperflow.core.constants import RISK_ORDER
from paperflow.core.security.middleware.base import (
    SecurityMiddleware, PolicyDenied, ConfirmRequired,
)
from paperflow.core.security.domain import ToolContext


class PolicyEngineMiddleware(SecurityMiddleware):
    """策略检查中间件：默认禁止、风险档位升级确认两级检查。

    Attributes:
        confirm_above: str，会话免问上限档（风险档高于它的工具执行前需用户确认）
        _confirmed: set[tuple[str, str | None]]，本会话已放行的 (工具名, 目标路径) 集合
    """

    def __init__(self, confirm_above: str = "medium"):
        """指定会话免问上限档；非法档位在构造期即失败。

        初始化已确认集合——存放本会话内用户放行过的 (工具名, 目标路径)，
        同一键不再重复询问。

        Args:
            confirm_above: 会话免问上限档（"low" / "medium" / "high" / "critical"），
                           默认 "medium"。风险档位**高于**此档的工具在执行前
                           升级为人工确认；不高于的静默放行。

        Raises:
            ValueError: 当 confirm_above 不在 RISK_ORDER 中时抛出（fail-fast）
        """
        if confirm_above not in RISK_ORDER:
            raise ValueError(
                f"非法风险阈值: {confirm_above}，合法值: {sorted(RISK_ORDER.keys())}"
            )
        self.confirm_above = confirm_above

        # 已确认集合，键为 (工具名, 目标路径)：同一工具的不同路径仍需单独确认。
        # 目标路径经 tool.effective_target_path(args) 导出：有 path 参数的工具取
        # args["path"]；有 pathless 便捷入口的工具（如 write_file 的 filename 模式）
        # 覆写为组合落盘路径——否则键会塌缩为 (工具名, None)，一次授权即放行
        # 全会话 pathless 写。返回 None 的确认工具键为 (工具名, None)，
        # 退化为按工具名确认的行为（防御式）。
        self._confirmed: set[tuple[str, str | None]] = set()

    async def before(self, ctx: ToolContext) -> None:
        """按 默认禁止 → 风险档位升级 的顺序检查工具，违规即抛异常。

        两级检查依次进行，任一环节终止后续检查并抛出对应异常：
            1. 默认禁止（blocked_by_default）：工具声明中标记为永久禁止 → PolicyDenied
            2. 风险档位（risk_threshold）：工具风险档高于会话免问上限 → ConfirmRequired

        只有两级检查全部通过，工具才被允许执行。

        Args:
            ctx: 工具调用上下文，包含工具定义、参数等信息

        Raises:
            PolicyDenied: 第1级检查失败时抛出
            ConfirmRequired: 第2级检查失败（风险档高于免问上限且未确认过）时抛出
        """
        # 未知工具（LLM 幻觉或注入）跳过策略检查，交给审计中间件记录错误
        if ctx.tool is None:
            return        # 未知工具交给 after 钩子做审计

        tool = ctx.tool

        # 记录本次评估的策略配置输入（供审计 replay：这条调用当时在什么配置下被评估）
        ctx.policy_context = {
            "confirm_above": self.confirm_above,
            "tool_risk": tool.risk_level,
            "blocked_by_default": bool(tool.blocked_by_default),
        }

        # ===== 第1级检查：默认禁止 =====
        # 工具声明 blocked_by_default=True 意味着无论风险档位如何都不可执行
        # （需管理员手动覆盖策略）——这是档位体系之外的显式硬拒。
        if tool.blocked_by_default:
            ctx.policy_fired = "blocked_by_default"
            raise PolicyDenied(
                reason=f"'{tool.name}' 被标记为默认禁止，需手动覆盖才可执行"
            )

        # ===== 第2级检查：风险档位 =====
        # 单一判据：档位高于免问上限 → 升级确认（超档不拒绝，只是要用户点头）。
        # 未知风险档（如拼写错误）按最高档处理，fail-safe——笔误只会多问一次，
        # 不会被静默放行。
        tool_risk = RISK_ORDER.get(tool.risk_level, RISK_ORDER["critical"])

        if tool_risk > RISK_ORDER[self.confirm_above]:
            ctx.policy_fired = "risk_threshold"
            # 确认键为 (工具名, 有效目标路径)：同一工具的不同路径需分别确认。
            # 路径经 effective_target_path 导出——pathless 入口（filename 模式）
            # 组合出落盘路径后与 path 模式同键控，不会塌缩为按工具名放行。
            confirm_key = (tool.name, tool.effective_target_path(ctx.args))
            if confirm_key not in self._confirmed:
                raise ConfirmRequired(
                    tool_name=tool.name,
                    params=ctx.args,
                    risk_level=tool.risk_level,
                    side_effects=tool.side_effects,
                    # lambda 捕获 confirm_key：回调只往已确认集合里加当前键，闭包安全
                    on_confirmed=lambda: self._confirmed.add(confirm_key),
                )
