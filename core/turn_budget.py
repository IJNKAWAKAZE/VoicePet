"""维护单个对话轮次的时间、token 和工具调用预算"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from numbers import Real


class TurnBudgetExceededError(RuntimeError):
    """单轮任一资源达到配置上限"""

    code = "runtime.budget"
    retryable = False

    def __init__(self, kind: str) -> None:
        messages = {
            "duration": "单轮总耗时已达上限",
            "tokens": "单轮 token 已达上限",
            "tool_calls": "单轮工具调用次数已达上限",
        }
        if kind not in messages:
            raise ValueError("单轮预算类型无效")
        super().__init__(messages[kind])
        self.kind = kind


@dataclass(frozen=True, slots=True)
class TurnBudgetLimits:
    """单轮资源限制的不可变配置"""

    max_duration: float = 120.0
    max_tokens: int = 16_384
    max_tool_calls: int = 4

    def __post_init__(self) -> None:
        if isinstance(self.max_duration, bool) or not isinstance(
            self.max_duration,
            Real,
        ):
            raise TypeError("单轮总耗时上限类型无效")
        if not math.isfinite(float(self.max_duration)) or self.max_duration <= 0:
            raise ValueError("单轮总耗时上限必须是有限正数")
        for value, name in (
            (self.max_tokens, "token"),
            (self.max_tool_calls, "工具调用"),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"单轮{name}上限类型无效")
            if value <= 0:
                raise ValueError(f"单轮{name}上限必须大于零")


class TurnBudget:
    """记录一个对话轮次已经消耗的有限资源"""

    def __init__(
        self,
        limits: TurnBudgetLimits | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._limits = limits or TurnBudgetLimits()
        self._clock = clock or time.monotonic
        self._started_at = float(self._clock())
        if not math.isfinite(self._started_at):
            raise ValueError("单轮预算时钟必须返回有限数值")
        self._total_tokens = 0
        self._tool_calls_used = 0

    @property
    def limits(self) -> TurnBudgetLimits:
        return self._limits

    @property
    def total_tokens(self) -> int:
        return self._total_tokens

    @property
    def tool_calls_used(self) -> int:
        return self._tool_calls_used

    def elapsed_ms(self) -> int:
        """返回单调时钟计算的非负已用毫秒数"""

        return int(self._elapsed_duration() * 1000)

    def remaining_duration(self) -> float:
        """返回不会小于零的剩余秒数"""

        return max(0.0, self._limits.max_duration - self._elapsed_duration())

    def ensure_available(self) -> None:
        """在启动下一步操作前检查时间和 token"""

        if self.remaining_duration() <= 0:
            raise TurnBudgetExceededError("duration")
        if self._total_tokens >= self._limits.max_tokens:
            raise TurnBudgetExceededError("tokens")

    def output_token_limit(self, request_limit: int = 1024) -> int:
        """把单请求输出上限压缩到当前剩余 token"""

        if isinstance(request_limit, bool) or not isinstance(request_limit, int):
            raise TypeError("单请求输出 token 上限类型无效")
        if request_limit <= 0:
            raise ValueError("单请求输出 token 上限必须大于零")
        self.ensure_available()
        remaining = self._limits.max_tokens - self._total_tokens
        return min(request_limit, remaining)

    def record_usage(self, input_tokens: int, output_tokens: int) -> int:
        """累加 Provider 返回的非负 token 统计"""

        for value in (input_tokens, output_tokens):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("LLM token 统计类型无效")
            if value < 0:
                raise ValueError("LLM token 统计不能小于零")
        self._total_tokens += input_tokens + output_tokens
        return self._total_tokens

    def reserve_tool_call(self) -> int:
        """为下一次工具提议原子占用一个本轮名额"""

        self.ensure_available()
        if self._tool_calls_used >= self._limits.max_tool_calls:
            raise TurnBudgetExceededError("tool_calls")
        self._tool_calls_used += 1
        return self._tool_calls_used

    def _elapsed_duration(self) -> float:
        current = float(self._clock())
        if not math.isfinite(current):
            raise ValueError("单轮预算时钟必须返回有限数值")
        return max(0.0, current - self._started_at)
