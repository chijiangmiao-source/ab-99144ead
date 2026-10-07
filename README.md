# 束流保护演练服务（beamguard）

在持续到达的采样流中安全切换阈值规程的演练服务。值班员通过网页创建演练、
登记按整数采样序号生效的新规程、投递带稳定标识的观测，并实时查看连续水位、
等待队列、各序号实际采用的规程及首个拒因。

零依赖 Node.js（>= 20）实现，无需安装任何 npm 包。

## 核心语义

- **连续水位**：系统仅从序号 0 起按连续顺序裁决。水位 = 下一期望序号。
  较早序号缺失时，后续观测进入等待队列持久等待，水位不动；缺口补齐后
  依序排空（可能一次裁决多条）。
- **规程切换**：新规程按整数生效序号登记，对 `seq >= effectiveSeq` 的裁决
  生效（取 `effectiveSeq <= seq` 中最大者）。水位首次越过生效序号后，后续
  裁决固定采用新规程；已裁决历史绝不重算——因此生效序号低于当前水位的
  登记会被拒绝（`EFFECTIVE_SEQ_PASSED`），同一序号的重复登记同样被拒绝
  （`EFFECTIVE_SEQ_DUPLICATE`）。
- **裁决**：读数 > 规程阈值 → `REJECT`（记录首个拒因），否则 `ACCEPT`。
  每个序号仅有一份不可变裁决。
- **幂等与冲突**：
  - 相同投递标识 + 相同内容的重传 → 回显原裁决（`replayed: true`）；
  - 同标识改动序号或读数 → `409 DELIVERY_CONFLICT`；
  - 同序号出现不同读数 → `409 SEQ_CONFLICT`；
  - 所有拒绝均不改写等待记录或既有裁决。
- **崩溃恢复**：每次被接受的变更先原子落盘（临时文件 + fsync + rename +
  目录 fsync）再应答。无论在写入等待队列后还是推进水位后中断，重开服务
  恢复的结果与不中断执行一致；重传在恢复后仍回显原裁决。

## 一次性验收（verify）

```sh
docker compose up --build --exit-code-from verify
```

`verify` 容器依次执行，全部结束后自行退出并以退出码报告验收结果：

1. **项目构建检查**：`package.json` 解析 + 全部源码 `node --check`；
2. **状态机代码测试**：`node --test test/`（20 个用例，含“逐步崩溃恢复
   与不中断执行等价”的存储层证明）；
3. **HTTP 冒烟**：对真实接口执行——创建演练、登记序号 2 生效的新规程、
   **先提交 2**（等待）、崩溃重启后确认等待记录仍在、**再提交 0 和 1**、
   确认队列依序排空且 **2 采用新规程**裁决、再次崩溃重启确认状态与中断前
   逐字节一致、重传回显原裁决、各类冲突 409 且不改写既有状态。

退出码 0 = 验收通过，非 0 = 失败（输出中见 `FAIL` 行）。

## 运行服务

```sh
# Docker（含网页控制台，映射到本机 8080）
docker compose up --build app

# 或本地直接运行
DATA_DIR=./data PORT=8080 node src/server.js
```

打开 <http://localhost:8080> 使用网页控制台：创建演练 → 登记新规程 →
投递观测 → 查看水位 / 等待队列 / 裁决记录（每 2 秒自动刷新）。

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8080` | 监听端口 |
| `DATA_DIR` | `./data` | 演练状态持久化目录（compose 中为 `/data` 卷） |
| `ENABLE_CRASH_ENDPOINT` | 关 | 置 `1` 暴露测试专用 `POST /admin/crash`（verify 依赖） |

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 存活与本次启动标识 `bootId` |
| `POST` | `/api/drills` | 创建演练 `{name, baseThreshold}` → `201` |
| `GET` | `/api/drills` | 演练摘要列表 |
| `GET` | `/api/drills/:id` | 水位、规程表、等待队列、裁决记录 |
| `POST` | `/api/drills/:id/protocols` | 登记规程 `{effectiveSeq, threshold}` → `201` / `409` |
| `POST` | `/api/drills/:id/observations` | 投递观测 `{deliveryId, seq, reading}` → `200` / `400` / `409` |
| `POST` | `/admin/crash` | 测试专用崩溃钩子（需显式开启） |

错误统一为 `{"error": {"code", "message"}}`，码值见 `src/drill.js`。

## 项目结构

```
src/drill.js       状态机：水位推进、规程选择、幂等与冲突裁决（纯函数式）
src/store.js       原子落盘与恢复（tmp + fsync + rename）
src/registry.js    内存缓存 + “克隆-变更-落盘-换入”的变更边界
src/server.js      HTTP API 与静态页面
public/index.html  值班员网页控制台
test/              状态机、存储恢复、HTTP 层测试（node --test）
verify/smoke.js    一次性 HTTP 冒烟（乱序补传、规程切换、中断恢复）
scripts/check.js   构建检查（语法 + 清单）
scripts/verify.sh  verify 容器入口：构建检查 → 测试 → 冒烟
Dockerfile.app     服务镜像
Dockerfile.verify  验收镜像
docker-compose.yml app + verify 编排
```
