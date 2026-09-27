# 构建夜市健康咨询后续关怀路由器基础服务

本项目提供中医文化夜市的通用后台基础能力，负责活动机构、服务站点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。新的业务模块可以在这些稳定边界之上增加领域状态、规则和接口。

在基础层之上，`followup`（义诊后续联系路由）模块把义诊结束后的联系严格分成两条互不混用的链路：

- **参与者提醒链路（普通养生 / 建议复查）**：只在参与者勾选的渠道与用途授权范围内排队，站点本地 9:00–20:00 为可联络窗口；养生提醒 7 天、复查提醒 3 天为发送窗口，到期未发自动过期；文本在排队时固化模板版本，之后模板更新不影响已排队内容；真正发送前在同一事务内再次复核授权与固化模板，失败按 1/5/15 分钟退避重试，最多 3 次。
- **授权人员交接链路（专家标记需尽快进一步检查）**：不对参与者发任何消息，24 小时原始期限内由当班专家（reviewer）接手；到期未确认按原期限升级给管理负责人（admin），重复扫描与重复通知天然幂等；人工确认只记录 `accepted`/`contacted_participant`/`referred_clinic` 代码，不记录病情。

两条链路共守的红线：后续类别只能来自专家给出的固定枚举（`wellness_reminder` / `recommended_recheck` / `urgent_followup`），系统不接受摘要里的诊断字段、也绝不从摘要内容推断诊疗结论；撤回授权立即取消所有尚未发送的提醒；成功投递的记录只保留渠道、尝试序号与渠道侧消息编号，无法反推出健康细节。所有定时状态（排队、重试、过期、交接、升级）全部落库，调度器本身无状态——进程重启后调用一次 `recover()`/时钟扫描即可凭表内状态恢复全部未完成任务；后台可通过解释接口看到每条联系「为何安排、为何跳过、为何转人工」的完整事件时间线。

## 目录

- `src/night_market_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `followup.py` / `followup_models.py`：固定类别、渠道、用途枚举与发送窗口、升级期限策略；
  - `followup_storage.py`：参与者授权、义诊摘要、专家决定、模板、路由、发送尝试与交接任务表（建表幂等）；
  - `followup_gateway.py`：渠道发送与人工通知的可替换出站端口；
  - `followup_service.py`：路由排队、模板固化、到期发送、重试、撤回、升级与人工接管；
  - `followup_acceptance.py`：后续联系链路的离线端到端验收。
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

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

后续联系链路的离线验收：

```bash
PYTHONPATH=src python3 -m night_market_foundation.followup_acceptance
```

该验收会在临时库中走完授权登记、专家三类标记、窗口发送、跳过原因、24 小时到期升级、人工确认、重复扫描幂等与重启恢复，并核对最终状态与审计链。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

后续联系链路的接口（写入均带 `request_id` 幂等键，时间由服务端注入时钟决定）：

- `POST /followup/participants`：登记/更新参与者授权的渠道与用途；
- `POST /followup/consent-revocations`：撤回授权并取消未发送提醒；
- `POST /followup/encounters`：接收白名单字段的最少必要现场摘要；
- `POST /followup/expert-decisions`：接收专家类别并创建路由（仅 reviewer/admin）；
- `POST /followup/templates`：登记模板新版本（仅 admin，旧版本不可变）；
- `POST /followup/clock-ticks`：按当前时钟推进发送、重试、过期与升级；
- `POST /followup/staff-tasks/claim`、`POST /followup/staff-tasks/confirm`：授权人员认领与确认接手；
- `GET /followup/followups/<id>/explanation`：后台解释一条联系为何安排、跳过或转人工。
