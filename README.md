# 构建夜市健康咨询后续关怀路由器基础服务

本项目提供中医文化夜市的通用后台基础能力，负责活动机构、服务站点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。新的业务模块可以在这些稳定边界之上增加领域状态、规则和接口。

## 目录

- `src/night_market_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 诊后联系分流

义诊结束后的联系不共用名单，由 `followup_service.py` 在基础层之上实现：

- **两条名单物理分开**：常规养生提醒进入 `scheduled_messages`，只在参与者授权的渠道范围内发送；专家标记需尽快进一步检查的事项进入 `followup_handoffs`，在固定期限内交给授权人员（第一层复核专家，逾期升级到管理员）。系统只按事先登记的专家类别路由，任何环节都不生成诊疗判断。
- **可注入时钟**：发送窗口（最早发送时刻 + 截止时刻）、交接期限、升级宽限和到期动作（转人工 / 升级 / 记逾期）均由 `Clock` 决定。
- **发送前复核**：真正调用渠道前，在短事务内原子占用消息并再次核对授权状态、模板版本（含正文哈希）；授权撤回立即取消未发送内容，模板版本被管理员召回则转人工。
- **模板版本固化**：排队时保存模板版本号、模板正文哈希与渲染快照；发布新版本不改变已排队文本。
- **最小回执**：成功投递只保留渠道、供应商消息引用和时间，不含参与者编号、类别与正文。
- **幂等与并发**：所有写操作支持 `request_id` 幂等回执；重试使用固定的消息编号作为渠道幂等键；发送采用声明令牌 + 比较交换，崩溃残留声明超时后可被接管；撤回与供应商受理对撞时保留最小回执并转人工核对。
- **恢复与解释**：dispatcher 无状态，重启后从 SQLite 恢复未完成任务；`GET /followups/{summary_ref}/explain` 解释一条联系为何安排、跳过或转人工。

分流相关接口：`POST /followup-categories`、`POST /message-templates`（`/message-templates/recall` 召回版本）、`POST /consents`、`POST /followups`（`/followups/dispatch-due`、`/followups/expire-overdue`、`/followups/takeover`）、`POST /handoffs/acknowledge`、`POST /handoffs/escalate-due`、`POST /receipts/confirm`、`GET /followups/pending` 与 `GET /followups/{id}/explain`。发送类接口需要在部署时注入实现 `MessageGateway` 协议的渠道网关。

分流模块的离线验收：

```bash
PYTHONPATH=src python3 -m night_market_foundation.followup_acceptance
```

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m night_market_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记活动机构、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。
