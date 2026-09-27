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


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          followup: FollowupService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    path_parts = [part for part in parsed.path.split("/") if part]
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
        if followup is not None and path_parts and path_parts[0] == "followup":
            return _followup_route(followup, method, path_parts, body, actor_id)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _followup_route(followup: FollowupService, method: str,
                    parts: list[str], body: dict[str, Any], actor_id: str
                    ) -> tuple[int, dict[str, Any]]:
    """分派义诊后续联系相关接口。"""

    if method == "POST" and parts == ["followup", "participants"]:
        receipt = followup.register_participant(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and parts == ["followup", "consent-revocations"]:
        receipt = followup.revoke_consent(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and parts == ["followup", "encounters"]:
        receipt = followup.record_encounter(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and parts == ["followup", "expert-decisions"]:
        receipt = followup.record_expert_decision(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and parts == ["followup", "templates"]:
        receipt = followup.register_template(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "POST" and parts == ["followup", "clock-ticks"]:
        result = followup.run_due()
        return 200, result.__dict__
    if method == "POST" and parts == ["followup", "staff-tasks", "claim"]:
        task = followup.claim_task(actor_id=actor_id, **body)
        return 200, task.__dict__
    if method == "POST" and parts == ["followup", "staff-tasks", "confirm"]:
        receipt = followup.confirm_received(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if method == "GET" and len(parts) == 4 and parts[1] == "followups" and parts[3] == "explanation":
        explanation = followup.explain(actor_id=actor_id, followup_id=parts[2])
        return 200, {
            "followup": explanation.followup.__dict__,
            "timeline": list(explanation.timeline),
            "attempts": [item.__dict__ for item in explanation.attempts],
            "tasks": [item.__dict__ for item in explanation.tasks],
        }
    return 404, {"error": "route_not_found", "message": "接口不存在"}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    followup: FollowupService | None = None

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
                                followup=self.followup)
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
    # 出站网关由部署方注入；未注入时发送尝试按可重试失败落库，不会丢失任务。
    Handler.followup = FollowupService(database)
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
