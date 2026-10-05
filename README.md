# 供水管网隔离预演服务

面向物业抢修场景的供水管网阀门变更校验与停水隔离预演服务。

- **技术栈**：Python 3.11+ / FastAPI / SQLite（标准库 `sqlite3`，无额外 ORM）
- **核心能力**
  - 录入：水源（供水上限）、不可并联水源对、单向管段（容量 + 阀门状态）、用水点（需求 + 优先级）
  - 阀门变更：**整批校验、原子生效**；凡使不可并联水源经开启管段连通的批次整体拒绝，阀门保持原状态
  - 隔离预演：给定拟关闭阀门，**不写库**；按优先级从高到低、同级按用水点编号分配各点可获得的最大流量，共享管段容量与水源上限不重复占用
  - 输出：各点供水量、欠供原因（`NO_PATH` / `SOURCE_LIMIT` / `PIPE_CAPACITY`）、受限管段
  - 错误定位：未知端点（404）、负容量（422）、非法阀门编号（404）均返回带 `location` 的错误体

## 快速开始

### Docker Compose（推荐，一键启动）

```bash
docker compose up --build
```

依赖安装、数据库初始化（建表）与启动全部自动完成。数据持久化在命名卷 `water_data` 中。

### Docker（单容器）

```bash
docker build -t water-isolation-service .
docker run -d -p 8000:8000 -v water_data:/data --name water-isolation water-isolation-service
```

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动时自动完成数据库初始化（幂等建表），无需手工迁移。

### 地址与端口

| 项目 | 地址 |
|---|---|
| 服务地址 | http://127.0.0.1:8000 |
| 交互式文档 (Swagger) | http://127.0.0.1:8000/docs |
| OpenAPI JSON | http://127.0.0.1:8000/openapi.json |

## 配置

通过环境变量配置（`docker-compose.yml` 中已给出默认值）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_PATH` | 容器内 `/data/network.db`，本地 `./data/network.db` | SQLite 数据库文件路径 |
| `PORT` | `8000` | 服务监听端口（Compose 映射到宿主机 8000） |

## 一键演示

服务启动后执行：

```bash
./examples/demo.sh                 # 默认 http://127.0.0.1:8000
./examples/demo.sh http://host:port
```

脚本依次演示：录入水源/水源对/管段/用水点 → 基准预演 → 关闭阀门预演 → 非法并联变更被拒 → 合法批次生效 → 各类定位错误。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/sources` | 录入水源：`{name, node, max_supply}` |
| GET | `/sources` | 水源列表 |
| POST | `/incompatible-pairs` | 录入不可并联水源对：`{source_a, source_b}` |
| GET | `/incompatible-pairs` | 水源对列表 |
| POST | `/pipes` | 录入单向管段：`{from_node, to_node, capacity, valve_open}`，自动创建阀门并返回 `valve.id` |
| GET | `/pipes` | 管段列表（含阀门状态） |
| GET | `/valves` | 阀门列表 |
| POST | `/valves/changes` | 阀门变更批次：`{changes:[{valve_id, open}]}`，整批校验、原子生效 |
| POST | `/demands` | 录入用水点：`{node, demand, priority, name?}` |
| GET | `/demands` | 用水点列表 |
| POST | `/rehearsal` | 隔离预演：`{close_valves:[...]}`，只演算不写库 |
| POST | `/admin/reset` | 清空全部数据（演示/测试用） |

### 调用示例

```bash
BASE=http://127.0.0.1:8000

# 录入水源（供水上限 100，位于节点 SRC1）
curl -s -X POST $BASE/sources -H 'Content-Type: application/json' \
  -d '{"name":"S1","node":"SRC1","max_supply":100}'

# 录入不可并联水源对
curl -s -X POST $BASE/incompatible-pairs -H 'Content-Type: application/json' \
  -d '{"source_a":1,"source_b":2}'

# 录入单向管段（容量 80，阀门默认开启；valve_open:false 表示常闭联络管）
curl -s -X POST $BASE/pipes -H 'Content-Type: application/json' \
  -d '{"from_node":"SRC1","to_node":"J1","capacity":80}'

# 录入用水点（需求 50，优先级 10，数值越大越优先）
curl -s -X POST $BASE/demands -H 'Content-Type: application/json' \
  -d '{"node":"N1","demand":50,"priority":10,"name":"医院"}'

# 隔离预演：拟关闭阀门 2（不写库）
curl -s -X POST $BASE/rehearsal -H 'Content-Type: application/json' \
  -d '{"close_valves":[2]}'

# 阀门变更批次：整批校验、原子生效
curl -s -X POST $BASE/valves/changes -H 'Content-Type: application/json' \
  -d '{"changes":[{"valve_id":3,"open":true}]}'
```

### 预演响应示例

```json
{
  "state_written": false,
  "closed_valves": [2],
  "allocation_order": "priority_desc,demand_id_asc",
  "results": [
    {
      "demand_id": 1, "name": "医院", "node": "N1", "priority": 10,
      "demand": 50.0, "allocated": 40.0, "shortage": 10.0,
      "reasons": [{"code": "PIPE_CAPACITY", "message": "管段容量受限", "pipe_ids": [4]}],
      "limited_pipes": [4]
    },
    {
      "demand_id": 3, "name": "消防", "node": "N2", "priority": 10,
      "demand": 20.0, "allocated": 0.0, "shortage": 20.0,
      "reasons": [{"code": "NO_PATH", "message": "阀门关闭或管网断开，无可用供水路径"}],
      "limited_pipes": []
    }
  ],
  "total_demand": 140.0,
  "total_allocated": 40.0
}
```

欠供原因码：

| code | 含义 |
|---|---|
| `NO_PATH` | 阀门关闭或管网断开，无可用供水路径 |
| `SOURCE_LIMIT` | 水源供水上限受限（附 `source_ids`） |
| `PIPE_CAPACITY` | 管段容量受限（附 `pipe_ids`，即受限管段） |

## 规则说明

- **并联判定**：某对不可并联水源，若各自经"开启且容量 > 0"的单向管段可达同一节点，即视为并联冲突。阀门批次只拒绝会**新增**冲突的变更；被拒绝时整批回滚，阀门保持原状态（409）。
- **分配规则**：所有水源经超级源点汇总（上限为各水源 `max_supply`），在**同一张残量网络**上按 `优先级降序、用水点编号升序` 依次求最大流；先分配的用水点消耗的管段容量与水源额度对后续不再可用，绝不重复占用。
- **受限管段**：在演算后的残量网络最小割上，取位于该用水点上游的饱和管段边，即该点的瓶颈管段。
- **预演不写库**：`/rehearsal` 仅在内存中叠加"当前阀门状态 + 拟关闭阀门"进行演算，响应中 `state_written` 恒为 `false`。

## 错误格式（定位错误）

所有错误统一为：

```json
{
  "error": {
    "code": "VALVE_NOT_FOUND",
    "message": "阀门编号不存在",
    "location": "body.close_valves",
    "details": {"unknown_valve_ids": [999]}
  }
}
```

| 场景 | HTTP | code | location |
|---|---|---|---|
| 未知端点 | 404 | `ENDPOINT_NOT_FOUND` | `path` |
| 负容量 / 参数非法 | 422 | `VALIDATION_ERROR` | `details[].location`，如 `body.capacity` |
| 非法阀门编号（变更） | 404 | `VALVE_NOT_FOUND` | `body.changes` |
| 非法阀门编号（预演） | 404 | `VALVE_NOT_FOUND` | `body.close_valves` |
| 并联冲突 | 409 | `PARALLEL_SOURCES_CONFLICT` | `body.changes` |
| 批次内阀门重复 | 400 | `DUPLICATE_VALVE_IN_BATCH` | `body.changes` |
| 水源不存在（建水源对） | 404 | `SOURCE_NOT_FOUND` | `body.source_a` / `body.source_b` |

## 项目结构

```
├── app/
│   ├── main.py       # FastAPI 入口：路由、整批校验、错误处理
│   ├── network.py    # 演算核心：Dinic 最大流、并联连通性校验、最小割瓶颈定位
│   ├── db.py         # SQLite 连接与幂等建表
│   └── schemas.py    # 请求体模型（负容量等在此拦截）
├── examples/demo.sh  # 端到端调用示例
├── requirements.txt
├── Dockerfile        # 依赖安装 + 启动（建表由应用启动时完成）
├── docker-compose.yml# 一键启动：docker compose up --build
└── README.md
```
