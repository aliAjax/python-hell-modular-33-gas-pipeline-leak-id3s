# 燃气管线泄漏检测与隔离协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8333`。

- `app.py`：参数、依赖和服务生命周期。
- `src/domain.py`：管段、传感值和来源记录校验。
- `src/rules.py`：泄漏评分、反馈合并与失效重算、阀门顺序、修复、试压、恢复状态机。
- `src/repository.py`：SQLite、半小时合并窗口、幂等来源、乐观版本和审计链。
- `src/service.py`：角色权限、辖区校验和业务编排。
- `src/http_api.py`：JSON 接口与首页。
- `src/audit.py`：可校验的审计事件。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8333
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询（含链校验结果 `verified`）。

## 泄漏事件账

- **反馈合成事件**：巡线、传感器、电话的反馈都走 `POST /api/items`。同一管段任一已知反馈前后半小时（含边界）内的新反馈并入既有未终态事件，不再各开一单；每条来源的类型、编号和观测时间保留在 `sources` 里。并入返回 `200`，新开事件返回 `201`。
- **晚到更正**：来源记录（`POST /api/items/<id>/sources`）改动管段或压降时，旧评分立即重算，未执行的隔离方案作废，已关阀门保留在 `closed_valves` 中保持关闭，事件退回 `reported` 待核验；审计链追加 `event_invalidated`。终态（已撤销/已恢复）事件只记账不改状态。
- **并发只留一份**：事件写入在单个写事务内完成，两名值班员同时提交合并只产生一个事件；撤销等操作必须带 `expected_version`，同时提交时只有一份生效，另一方收到 `version_conflict`/`invalid_state`。
- **辖区越权拒绝**：事件可带 `region`（缺省取提交人 `X-Region`）。处理其他辖区的事件（动作或来源提交）返回 `403 region_mismatch`，`regulator` 角色除外。
- **幂等重试**：来源按 `(source_type, external_id)` 去重。写盘失败后按来源编号原样重试不会重复入账，重复送达返回 `200` 且不新增来源；未提供编号时按内容生成稳定编号。
- **重启对账**：事件、阀门（`closed_valves`）和审计记录全部落 SQLite，服务重启后 `GET /api/items/<id>` 与 `GET /api/items/<id>/audit` 仍能对上，审计链可重算校验。

测试覆盖完整抢修流程、半小时合并窗口、晚到更正失效重算、并发合并/撤销、幂等重发、辖区越权、重启对账、阀门顺序、试压阈值、现场危险条件、权限和版本冲突。模型不替代 SCADA、管网水力计算或正式应急预案。
