"""定义消息发送网关抽象与可分类的发送结果。

真正的渠道适配由调用方实现，服务层只依赖该协议，
便于在测试中注入固定结果并验证重试幂等。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


# 发送结果类型
RESULT_ACCEPTED = "accepted"
RESULT_TRANSIENT = "transient"
RESULT_REJECTED = "rejected"


@dataclass(frozen=True)
class DeliveryResult:
    """描述一次网关调用的稳定结果。"""

    result: str
    provider_message_ref: str | None = None
    error_code: str | None = None
    retry_after_seconds: int | None = None

    @property
    def accepted(self) -> bool:
        return self.result == RESULT_ACCEPTED

    @property
    def transient(self) -> bool:
        return self.result == RESULT_TRANSIENT

    @property
    def rejected(self) -> bool:
        return self.result == RESULT_REJECTED


class MessageGateway(Protocol):
    """发送渠道必须实现的最小接口。"""

    def send(self, *, channel: str, address: str, body: str, idempotency_key: str) -> DeliveryResult:
        """发送一条消息。

        idempotency_key 在同一条消息的多次重试间保持不变，
        网关与调用方都据此去除重复投递。
        """


class ScriptedGateway:
    """按消息编号或默认值返回预设结果的测试网关。"""

    def __init__(self, outcomes: dict[str, DeliveryResult] | None = None,
                 default: DeliveryResult | None = None) -> None:
        self._outcomes = outcomes or {}
        self._default = default or DeliveryResult(RESULT_ACCEPTED, provider_message_ref="prov-default")
        self.calls: list[dict[str, Any]] = []

    def send(self, *, channel: str, address: str, body: str, idempotency_key: str) -> DeliveryResult:
        self.calls.append({"channel": channel, "address": address, "body": body,
                           "idempotency_key": idempotency_key})
        # 一次性结果：同一条消息重试时，后续调用回落到默认结果
        if idempotency_key in self._outcomes:
            return self._outcomes.pop(idempotency_key)
        return self._default
