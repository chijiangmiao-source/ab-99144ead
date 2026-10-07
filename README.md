# 束流保护阈值规程切换系统

面向持续到达的采样流，支持在不重算历史裁决的前提下切换阈值规程。仅使用
Python 3.11 标准库（`http.server` + `sqlite3`），无第三方依赖。

## 语义

- **连续裁决**：系统只从序号 0 起按连续顺序裁决。较早序号缺失时，后续
  观测持久化进等待队列且水位不动；缺口补齐后在**同一事务**内依序排空。
- **规程切换**：规程按整数序号 `effective_from`（含）登记；裁决序号 `i`
  时固定采用 `effective_from <= i` 中最大的一份。水位首次越过生效序号后，
  影响更早序号的登记返回 `409 PROCEDURE_LATE`，历史结果永不重算。
- **稳定投递标识**：
  - 同 `delivery_id` + 同内容（序号、读数）重传 → 回显原裁决（`duplicate=true`）；
  - 同 `delivery_id` 改动序号或读数 → `409 DELIVERY_CONTENT_CHANGED`；
  - 同序号出现不同投递 → `409 SEQUENCE_TAKEN`。
  拒绝不改写等待记录与既有裁决；每次拒绝都留痕，演练状态中可查**首个拒因**。
- **持久化与恢复**：入队、裁决、水位推进均为 SQLite 原子事务（WAL 模式）。
  服务重开时 `recover()` 补排中断前已连续可达的等待项，结果与不中断执行
  完全一致；每个序号仅有一份不可变裁决。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 值班台网页（水位 / 等待队列 / 各序号规程与裁决 / 首个拒因） |
| GET | `/health` | 健康检查 |
| GET | `/api/drills` | 演练列表 |
| POST | `/api/drills` | 创建演练（`drill_id`、`name`、`base_procedure_code`、`base_threshold`） |
| GET | `/api/drills/{id}` | 演练全景（水位、规程、等待、裁决、拒因） |
| POST | `/api/drills/{id}/procedures` | 登记新规程（`effective_from`、`code`、`threshold`） |
| POST | `/api/drills/{id}/observations` | 投递观测（`delivery_id`、`seq`、`reading`） |

观测立即裁决返回 `200`（含 `verdict` 与本次顺带排空的 `drained`）；
缺号等待返回 `202 WAITING`；冲突返回 `409` 并给出稳定错误码与拒因。

## 本地运行

```bash
python3 -m app.main                      # DB_PATH / HOST / PORT 可用环境变量覆盖
# 浏览器打开 http://127.0.0.1:8080/
```

## Docker Compose 一次性验收

```bash
docker compose build
docker compose run --rm verify          # 退出码 0 = 验收通过
```

`verify` 服务是**可执行的一次性任务**：等主服务健康后，通过真实 HTTP 接口
完成冒烟（先提交序号 2、再提交 0 和 1，验证序号 2 采用新规程；重传回显；
各类冲突 409；等待与裁决不被改写；首个拒因可查），并在容器内真实
`SIGTERM` 中断、重开本地服务两次，核对恢复结果与不中断执行一致；同时运行
状态机代码测试（`unittest`）与项目构建检查（`compileall`）。全部结束后
自行退出，以退出码报告验收结果。

也可不依赖容器直接跑（先启动主服务）：

```bash
BASE_URL=http://127.0.0.1:8080 python3 scripts/verify.py
```

## 目录

```
app/statemachine.py   裁决状态机（SQLite 持久化、事务、恢复）
app/server.py         HTTP 接口与值班台静态页
app/main.py           服务入口
app/static/index.html 值班台网页
tests/test_statemachine.py  状态机代码测试（13 项）
scripts/verify.py     一次性验收（构建检查 + 单测 + HTTP 冒烟 + 中断恢复）
Dockerfile / docker-compose.yml
```
