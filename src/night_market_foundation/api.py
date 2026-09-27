"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .followup_service import FollowupService
from .service import DomainService
from .storage import Database


def _explanation(payload) -> dict[str, Any]:
    return {
        "summary_ref": payload.summary_ref,
        "participant_ref": payload.participant_ref,
        "tier": payload.tier,
        "category": payload.category,
        "decisions": payload.decisions,
        "messages": payload.messages,
        "handoff": payload.handoff,
    }


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          followup_service: FollowupService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        if followup_service is not None:
            status, payload = _followup_route(followup_service, method, parsed, body, actor_id)
            if status is not None:
                return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _followup_route(followup: FollowupService, method: str, parsed, body: dict[str, Any],
                    actor_id: str) -> tuple[int | None, dict[str, Any]]:
    """分派诊后联系分流相关接口。"""

    path = parsed.path
    query = parse_qs(parsed.query)
    if method == "POST" and path == "/followup-categories":
        result = followup.register_category(actor_id=actor_id, **body)
        return (200 if result["replayed"] else 201), result
    if method == "POST" and path == "/message-templates":
        result = followup.publish_template(actor_id=actor_id, **body)
        return (200 if result["replayed"] else 201), result
    if method == "POST" and path == "/message-templates/recall":
        result = followup.recall_template(actor_id=actor_id, **body)
        return 200, result
    if method == "POST" and path == "/consents":
        result = followup.record_consent(actor_id=actor_id, **body)
        return (200 if result["replayed"] else 201), result
    if method == "POST" and path == "/followups":
        result = followup.record_followup(actor_id=actor_id, **body)
        return (200 if result["replayed"] else 201), result
    if method == "POST" and path == "/followups/dispatch-due":
        return 200, followup.dispatch_due(actor_id=actor_id or "system-dispatcher")
    if method == "POST" and path == "/followups/expire-overdue":
        return 200, {"expired": followup.expire_overdue(actor_id=actor_id or "system-scheduler")}
    if method == "POST" and path == "/handoffs/escalate-due":
        return 200, followup.escalate_due(actor_id=actor_id or "system-scheduler")
    if method == "POST" and path == "/handoffs/acknowledge":
        result = followup.acknowledge_handoff(actor_id=actor_id, **body)
        return 200, result
    if method == "POST" and path == "/followups/takeover":
        result = followup.takeover(actor_id=actor_id, **body)
        return 200, result
    if method == "POST" and path == "/receipts/confirm":
        receipt = followup.confirm_delivery(**body)
        return 200, receipt.__dict__
    if method == "GET" and path == "/followups/pending":
        return 200, followup.list_pending()
    if method == "GET" and path.startswith("/followups/") and path.endswith("/explain"):
        summary_ref = path.split("/")[2]
        explanation = followup.explain(actor_id=actor_id, summary_ref=summary_ref)
        return 200, _explanation(explanation)
    return None, {}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    followup_service: FollowupService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                followup_service=self.followup_service)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    # 渠道网关由部署环境注入；未注入时发送类接口会返回明确错误而不会静默丢弃
    Handler.followup_service = FollowupService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
