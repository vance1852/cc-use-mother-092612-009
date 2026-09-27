"""定义真实渠道发送与人工通知的最小出站端口。

领域服务不直接触碰短信/微信等外部系统，而是依赖这个可替换端口。
入参只有渠道、脱敏目标标识与排队时固化的文本，不包含任何摘要或病情字段，
因此渠道侧无法从消息内容反推健康细节。每次调用带幂等键，重复回执不产生重复消息。
"""

from __future__ import annotations

from typing import Any, Protocol


class SendResult:
    """渠道发送结果，使用标准结果类型表达成功/可重试失败/永久失败。"""

    __slots__ = ("ok", "retryable", "provider_message_id", "detail_code")

    def __init__(self, *, ok: bool, retryable: bool = False,
                 provider_message_id: str | None = None, detail_code: str | None = None) -> None:
        self.ok = ok
        self.retryable = retryable
        self.provider_message_id = provider_message_id
        self.detail_code = detail_code

    @classmethod
    def delivered(cls, provider_message_id: str) -> "SendResult":
        return cls(ok=True, provider_message_id=provider_message_id)

    @classmethod
    def retry_later(cls, detail_code: str = "provider_unavailable") -> "SendResult":
        return cls(ok=False, retryable=True, detail_code=detail_code)

    @classmethod
    def rejected(cls, detail_code: str) -> "SendResult":
        return cls(ok=False, retryable=False, detail_code=detail_code)


class MessageGateway(Protocol):
    """真实消息渠道的出站端口。"""

    def send(self, *, channel: str, target_token: str, text: str,
             idempotency_key: str) -> SendResult:
        """向脱敏目标标识发送已固化文本；同一幂等键重复调用必须返回同一结果。"""


class StaffNotifier(Protocol):
    """通知授权人员有高风险交接任务的出站端口。"""

    def notify(self, *, actor_id: str | None, task_id: str, role: str,
               due_at: str, site_id: str) -> None:
        """只传递任务标识与期限，不传递任何健康信息。"""


class RecordingGateway:
    """离线验收与测试使用的内存网关，按幂等键去重并可被配置为前若干次失败。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_retryable_until: dict[str, int] = {}
        self._provider_seq = 0

    def send(self, *, channel: str, target_token: str, text: str,
             idempotency_key: str) -> SendResult:
        # 同一幂等键重复提交时复用首次结果，模拟渠道侧幂等去重。
        for item in self.sent:
            if item["idempotency_key"] == idempotency_key:
                return SendResult.delivered(item["provider_message_id"])
        remaining = self.fail_retryable_until.get(channel, 0)
        if remaining > 0:
            self.fail_retryable_until[channel] = remaining - 1
            return SendResult.retry_later()
        self._provider_seq += 1
        provider_message_id = f"pm-{self._provider_seq:08d}"
        self.sent.append({"channel": channel, "target_token": target_token, "text": text,
                          "provider_message_id": provider_message_id,
                          "idempotency_key": idempotency_key})
        return SendResult.delivered(provider_message_id)


class RecordingNotifier:
    """记录人工通知的测试替身，按任务编号幂等。"""

    def __init__(self) -> None:
        self.notifications: list[dict[str, Any]] = []

    def notify(self, *, actor_id: str | None, task_id: str, role: str,
               due_at: str, site_id: str) -> None:
        for item in self.notifications:
            if item["task_id"] == task_id:
                return
        self.notifications.append({"actor_id": actor_id, "task_id": task_id, "role": role,
                                   "due_at": due_at, "site_id": site_id})
