# GraphQL Cost Guard

面向 GraphQL 网关的**静态查询成本分析服务**（FastAPI + graphql-core + SQLite）。
客户端提交 SDL Schema、查询文档、变量样例与成本预算，服务返回最坏情况下的查询成本、
逐路径（per-path）成本解释，并提供基于请求指纹的结果缓存。

## 功能特性

- **片段与别名展开**：自动内联 `fragment` / inline fragment，路径使用 alias（响应键）命名。
- **最坏情况成本模型**：按字段深度与**列表乘数**复合累计：

  ```
  contribution(field) = complexity(field) × ∏(根到该字段的列表乘数)
  total_cost          = Σ 所有字段的 contribution
  ```

  等价于递归定义 `cost(f) = multiplier(f) × (complexity(f) + Σ cost(children))`。
- **列表乘数解析优先级**：
  1. SDL 字段上的 `@cost(multiplier: N)` 指令；
  2. 查询中的分页参数 `first` / `last` / `limit` / `size` / `take`（字面量或从变量样例解析）；
  3. 列表字段使用默认乘数 `COSTGUARD_DEFAULT_LIST_MULTIPLIER`（默认 10），非列表为 1。
- **`@cost` 指令**：`@cost(complexity: N)` 覆盖字段基础复杂度（默认 1），`@cost(multiplier: N)` 覆盖乘数。
- **片段循环检测**：DFS 检测 fragment 依赖图中的环，返回完整循环路径（如 `A -> B -> A`），含自引用。
- **超预算定位**：按文档遍历顺序累计成本，返回**首个使累计成本超过预算的路径**。
- **最坏情况语义**：
  - `@skip` / `@include` 条件无法从变量样例静态确定时，字段仍计入；
  - interface / union 上的所有 inline fragment 选择全部求和（上界）；
  - 支持深度与展开节点数上限，防止病态查询耗尽资源。
- **指纹缓存**：对（schema、query、variables、budget、operation、选项、成本模型版本）
  做规范化 SHA-256 指纹，结果持久化在 SQLite，支持命中计数、列表、详情与删除。
- **Schema 兼容性检查**（`POST /v1/schema-compatibility`）：对比基线与候选 SDL，
  从既有客户端视角判断候选 schema 是否可平滑升级：同名字段的命名类型与列表层级必须
  保持不变；返回类型只允许**加强**非空约束（`String` → `String!`），字段参数只允许
  **放宽**非空约束（`Int!` → `Int`）；报告被移除字段、新增的无默认值必填参数，以及
  每条违规的类型路径与原因。

## 快速开始（Docker Compose）

前置条件：已安装 Docker 与 Docker Compose v2。

```bash
docker compose up --build
```

服务启动后（容器内数据持久化到命名卷 `costguard-data`）：

| 入口 | 地址 |
| --- | --- |
| 服务端口 | http://localhost:8000 |
| 健康检查 | http://localhost:8000/healthz |
| Swagger 交互文档 | http://localhost:8000/docs |
| OpenAPI 规范 | http://localhost:8000/openapi.json |

停止并清理：

```bash
docker compose down            # 停止（保留缓存数据卷）
docker compose down -v         # 停止并删除 SQLite 数据卷
```

## 配置

所有配置通过 `COSTGUARD_` 前缀的环境变量提供（Compose 中可通过宿主机同名环境变量覆盖）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST_PORT`（仅 Compose） | `8000` | 宿主机映射端口，容器内固定监听 8000 |
| `COSTGUARD_DB_PATH` | `/data/costguard.db`（容器内） | SQLite 数据库文件路径 |
| `COSTGUARD_DEFAULT_LIST_MULTIPLIER` | `10` | 列表字段的默认最坏情况乘数（≥ 1） |
| `COSTGUARD_MAX_DEPTH` | `50` | 允许分析的最大字段深度 |
| `COSTGUARD_MAX_EXPANDED_NODES` | `20000` | fragment 展开后最多遍历的选择节点数 |
| `COSTGUARD_COST_MODEL_VERSION` | `v1` | 成本模型版本（参与指纹计算，升级模型后缓存自动失效） |

示例：在宿主机 9000 端口启动、默认乘数取 20：

```bash
HOST_PORT=9000 DEFAULT_LIST_MULTIPLIER=20 docker compose up --build
```

请求级覆盖：`POST /v1/analyze` 的 `options.default_list_multiplier` 可覆盖默认乘数，
`options.validate_document` 可关闭标准 GraphQL 校验。

## API

### `POST /v1/analyze` — 提交分析

```bash
curl -X POST http://localhost:8000/v1/analyze \
  -H 'Content-Type: application/json' \
  -d '{
    "schema_sdl": "type Query { user(id: ID!): User } type User { id: ID! name: String friends(first: Int): [User!]! }",
    "query": "query($n: Int) { user(id: \"1\") { name friends(first: $n) { name } } }",
    "variables": {"n": 5},
    "budget": 100
  }'
```

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_sdl` | string，必填 | GraphQL SDL Schema |
| `query` | string，必填 | GraphQL 查询文档 |
| `variables` | object | 变量样例，用于解析分页参数与 `@skip/@include`（与 operation 默认值合并） |
| `budget` | int | 成本预算，默认 1000 |
| `operation_name` | string\|null | 文档含多个 operation 时必填 |
| `refresh` | bool | `true` 时绕过缓存重新分析 |
| `options.default_list_multiplier` | int\|null | 请求级乘数覆盖 |
| `options.validate_document` | bool | 是否先做标准 GraphQL 校验，默认 true |

响应关键字段：

- `status`：`ok` / `over_budget` / `invalid`（SDL 或查询语法/校验错误）/ `error`（片段循环、超深度等）。
- `fingerprint`：请求指纹，用于后续查询缓存结果。
- `cached`：本次是否命中缓存。
- `summary`：`total_cost`、`budget`、`within_budget`、`remaining_budget`、
  `max_depth`、`field_count`、`fragments_used` 等。
- `paths[]`：**逐路径解释**（文档顺序），每项含 `path`、`field`、`alias`、
  `depth`、`complexity`、`multiplier`、`multiplier_source`、
  `effective_multiplier`、`cost`、`cumulative_cost`、`via_fragment`。
- `first_over_budget_path`：超预算时首个超限路径（含该字段成本与累计成本）。
- `errors[]` / `warnings[]`：错误码（如 `FRAGMENT_CYCLE`、`VALIDATION_FAILED`、
  `MAX_DEPTH_EXCEEDED`、`AMBIGUOUS_OPERATION`）与行列位置。

### `POST /v1/schema-compatibility` — Schema 兼容性检查

接收客户端当前使用的**基线 SDL** 与待发布的**候选 SDL**，判断既有 GraphQL 客户端
能否不改代码继续使用候选 schema。该接口不访问缓存，直接同步返回报告。

```bash
curl -X POST http://localhost:8000/v1/schema-compatibility \
  -H 'Content-Type: application/json' \
  -d '{
    "baseline_sdl": "type Query { users(limit: Int): [User!]! } type User { id: ID! name: String }",
    "candidate_sdl": "type Query { users(limit: Int, after: ID): [User!] } type User { id: ID! name: String! }"
  }'
```

兼容性规则（对基线与候选中同名、同所属类型的字段）：

| 维度 | 允许的变更 | 违规 code |
| --- | --- | --- |
| 命名字段类型 | 必须不变（`User` 不能变 `Account`） | `FIELD_NAMED_TYPE_CHANGED` |
| 返回类型列表层级 | 嵌套层数与每层的列表位置必须不变 | `FIELD_LIST_DEPTH_CHANGED` |
| 返回类型非空 | 只允许**加强**：`String` → `String!`、`[T]` → `[T!]!`；去掉 `!` 是破坏性变更 | `FIELD_NULLABILITY_WEAKENED` |
| 参数命名类型 | 必须不变 | `ARGUMENT_NAMED_TYPE_CHANGED` |
| 参数列表层级 | 必须不变 | `ARGUMENT_LIST_DEPTH_CHANGED` |
| 参数非空 | 只允许**放宽**：`Int!` → `Int`；新增 `!` 会拒绝旧请求 | `ARGUMENT_NULLABILITY_STRENGTHENED` |
| 新增参数 | 可空或带默认值即可；非空且无默认值要求每个旧调用补参 | 列入 `added_required_arguments` |
| 删除字段 | 旧查询在候选 schema 上校验失败 | 列入 `removed_fields` |

响应示例（对应一个会破坏旧客户端的候选 schema）：

```json
{
  "compatible": false,
  "removed_fields": [
    {"path": "User.name", "type": "String"}
  ],
  "added_required_arguments": [
    {"path": "Query.users(after)", "field": "Query.users", "argument": "after", "type": "ID!"}
  ],
  "violations": [
    {
      "code": "FIELD_NULLABILITY_WEAKENED",
      "path": "Query.users",
      "reason": "Non-null constraint on the list was removed ('[User!]!' -> '[User!]'); clients may now receive null where the baseline promised a value.",
      "baseline_type": "[User!]!",
      "candidate_type": "[User!]"
    },
    {
      "code": "ARGUMENT_NULLABILITY_STRENGTHENED",
      "path": "Query.users(limit)",
      "reason": "Argument type 'Int' became non-null ('Int' -> 'Int!'); existing requests that omit this value may be rejected.",
      "baseline_type": "Int",
      "candidate_type": "Int!"
    }
  ],
  "errors": []
}
```

说明：

- `compatible` 为 `true` 当且仅当三个报告列表均为空。
- `path` 为类型路径：字段是 `Type.field`，参数是 `Type.field(argument)`。
- 新增字段、新增类型、新增可空/带默认值参数等纯增量变更不产生任何条目。
- 类型被整体删除或类型种类改变（object → scalar 等）时，其基线字段会逐个出现在
  `removed_fields` 中。
- 任一侧 SDL 语法错误不会返回 5xx：HTTP 状态码仍为 200，`compatible=false`，
  错误放在 `errors[]`（`details.side` 标明是 `baseline` 还是 `candidate`）。

### 缓存管理

```bash
# 列出缓存的分析（分页 + 可选状态过滤）
curl "http://localhost:8000/v1/analyses?limit=50&offset=0&status=over_budget"

# 按指纹查看原始请求 + 完整分析结果（并计一次命中）
curl http://localhost:8000/v1/analyses/<fingerprint>

# 删除一条缓存
curl -X DELETE http://localhost:8000/v1/analyses/<fingerprint>

# 其他
curl http://localhost:8000/healthz
curl http://localhost:8000/v1/config
```

缓存指纹覆盖 schema、查询、变量、预算、operation 名、请求选项与成本模型版本；
任何一项不同都会产生不同指纹，因此同一查询使用不同预算会分别缓存。

## 成本模型示例

Schema：

```graphql
directive @cost(complexity: Int, multiplier: Int) on FIELD_DEFINITION

type Query {
  user(id: ID!): User
}
type User {
  name: String
  email: String @cost(complexity: 3)
  friends(first: Int): [User!]!
}
```

查询：

```graphql
{ user(id: "1") { name email friends(first: 5) { name } } }
```

| 路径 | 乘数 | 有效乘数 | 成本 |
| --- | --- | --- | --- |
| `Query.user` | 1 | 1 | 1 |
| `Query.user.name` | 1 | 1 | 1 |
| `Query.user.email` | 1 | 1（complexity=3） | 3 |
| `Query.user.friends` | 5（`argument:first`） | 5 | 5 |
| `Query.user.friends.name` | 1 | 5 | 5 |

`total_cost = 15`。嵌套列表会随深度复合，例如
`friends(first:10) { friends(first:10) { name } }` 中 `name` 的有效乘数为 100。

## 本地开发

需要 Python 3.11+。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# 启动（默认监听 8000，数据库位于 data/costguard.db）
uvicorn app.main:app --reload --port 8000

# 运行测试
pytest
```

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由（analyze / schema-compatibility / analyses / config / healthz）
│   ├── analyzer.py      # 核心：片段展开、循环检测、乘数解析、逐路径成本计算、指纹
│   ├── compatibility.py # 基线/候选 SDL 兼容性检查（字段移除、必填参数、类型/非空规则）
│   ├── db.py          # SQLite 持久化（WAL，线程安全）
│   ├── models.py      # Pydantic 请求/响应模型
│   └── config.py      # COSTGUARD_* 环境变量配置
├── tests/             # pytest：分析器 27 例 + 兼容性 37 例 + API 12 例
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```
