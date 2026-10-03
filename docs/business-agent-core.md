# 可复用 Agent 核心：架构、接口与接入

本文说明 `packages/` 下三个独立包的边界、接口和接入方式，并标注**哪些能力已经验证、哪些仍未验证**。

---

## 1. 为什么需要提取

改造前的 AI 能力全部锁在 `src/pyweixin` 里，且依赖方向已经反了：`opencode.py` 反向 `import .orders`，`gateway._validate_job_request` 把 `message/customer/products/history/order_state` 硬编码成订单字段。结果是「换个业务就得重写网关」。

改造后的依赖方向是单向的：

```
产品集成层  src/pyweixin/integration/     微信输入、本地查询、订单应用、发消息、计费
      ↓
订单领域插件  packages/order_agent/        意图、输出结构、自动记单、回执、记忆建议
      ↓
通用 Agent 核心  packages/business_agent/  契约、校验、上下文、执行、证据、用量
      ↓
执行后端接口  ExecutionBackend
```

**通用核心不 import pyweixin**。这条规则由 `tests/test_business_agent_core.py::BoundaryTests` 和
`tests/test_second_business_reuse.py::SecondDomainTests::test_the_two_domains_share_only_the_core` 强制执行，
不是靠文档约定。

核心同样不依赖：微信/UIA/Windows 窗口操作、Tkinter 或任何产品 UI、当前 SQLite 表结构、商家登录/套餐/充值/支付、固定模型供应商、OpenCode 专属会话对象、订单领域字段。

---

## 2. 目录

```
packages/
  business_agent/               通用核心，零运行时依赖
    src/business_agent/
      contracts/                版本化可序列化契约 + Protocol 接口
        request.py              AgentRequest / Scope / Event / AsOf / ContextBundle / Policy / Budget
        result.py               AgentResult / Evidence / Ambiguity / SuggestedAction / MemoryProposal / Usage / Versions
        errors.py               ErrorCategory 与错误分类
        protocols.py            ExecutionBackend / ContextProvider / MemoryReader / ToolRegistry / DomainPlugin / RunRepository
      core/
        runtime.py              AgentCore：校验 → 上下文 → 执行 → 校验结果 → 记录
        context.py              ContextAssembler：按 as_of 强制时间边界
        tools.py                ScopedToolRegistry：作用域绑定 + 白名单
        runs.py                 InMemoryRunRepository（含幂等查询）
        validation.py           JSON 提取与结构校验
      backends/
        opencode.py             OpenCode v1 会话 API
        model_api.py            OpenAI-compatible Chat Completions
        fake.py                 确定性桩，永远自报 is_model = False
      testing/                  契约测试脚手架与断言
  order_agent/                  订单领域插件
    src/order_agent/
      contract.py               订单输出契约与结构校验
      plugin.py                 OrderPlugin：提示词、输出、解释
      policy.py                 自动记单判定 + 最少追问
      receipt.py                回执渲染（按已校验字段，不让模型写金额）
  support_triage/               第二业务示例（无微信依赖）
    src/support_triage/         客服消息分类与回复建议
```

---

## 3. 数据契约

### 请求

`AgentRequest` 至少包含：`request_id`、`idempotency_key`、`schema_version`、`scope`（由可信宿主绑定）、`task_type`、`events`（当前事件与发送时间）、`as_of`（时间边界与时区）、`context`（相关上下文及来源标识）、`policy`（业务策略）、`budget`（执行时限与资源预算）。

构造时会拒绝自相矛盾的请求：任何事件的 `occurred_at` 晚于 `as_of` 都会直接抛 `InputError`，
避免「用未来的订单解释当时的『跟昨天一样』」这类泄漏。

### 结果

`AgentResult` 至少包含：`run_id`、`status`、`result_version`、`output`（业务结构化输出）、`evidence`（引用证据）、
`ambiguities`（歧义）、`suggested_actions`（建议动作）、`memory_proposals`（记忆变更建议）、
`missing_information`、`usage`（用量）、`error_category`/`error_detail`，以及
`versions`（后端、模型、提示词与策略版本）。

### 错误分类

| 分类 | 可重试 | 说明 |
| --- | --- | --- |
| `config_error` | 否 | 配置缺失 |
| `input_error` | 否 | 不可重试的输入错误 |
| `network_error` | **是** | 后端未连通，肯定没执行 |
| `rate_limited` | **是** | 明确要求退避 |
| `timeout` | 否 | 结果可能未知 |
| `cancelled` | 否 | 宿主取消 |
| `outcome_unknown` | **否** | 提示已发出，后端状态不明 |
| `output_invalid` | 否 | 结构校验失败 |
| `business_ambiguity` | 否 | 业务歧义，**正常结果** |

`outcome_unknown` 不可重试是这套分类里最重要的一条：提示已经发出，重跑可能让同一笔工作执行两次、
让商家被扣两次额度。核心遇到它会返回 `status=outcome_unknown` 并保留 `backend_handle`，交由对账决定。

`business_ambiguity` 不是失败。模型「拿不准」会走到 `needs_review`，而不是伪装成网络故障被反复重试。

---

## 4. 执行后端与「谁控制 Agent 循环」

三个后端，差异是刻意的：

| 后端 | Agent 循环由谁控制 | 核心能否在其外再包一层循环 |
| --- | --- | --- |
| `OpenCodeBackend` | OpenCode 内部 | **否** |
| `ModelApiBackend` | 核心（有界，≤ `budget.max_steps`） | 是，但只有这一处 |
| `FakeBackend` | 无 | 是 |

核心的 `_execute()` 只重试 `network_error` 和 `rate_limited`，且次数上限为 `max_attempts`（默认 2）。
`timeout` / `outcome_unknown` 直接上抛。长时重试与退避由宿主队列负责——两个队列串联重试
正是重复扣费的来源。

### OpenCode 已核实事实

`PINNED_OPENCODE_VERSION = "1.18.32"`，来源与逐条依据见 [`opencode-verification.md`](opencode-verification.md)：

- npm 包名 `opencode-ai`；仓库 `anomalyco/opencode`。
- **不存在 OpenAI 兼容端点**：`chat/completions`、`/v1/`、`completions` 在 v1（162 path）与 v2（113 path）
  两份规范中均 0 命中。本项目直接对接会话 API，不假设 `/v1/chat/completions` 可用。
  `probe()` 会显式记录 `openai_compatible_endpoint: False`。
- 服务端认证仅由非空 `OPENCODE_SERVER_PASSWORD` 开启 HTTP Basic，**默认关闭**。
  `OpenCodeBackend` 没有密码时拒绝构造。
- 权限默认值宽松（多数 `allow`，`ask` 在无头模式永久挂起），`order-parse` Agent 必须显式全部 `deny`。
- 每个任务一个全新会话，会话 id 作为 `backend_handle` 返回，供宿主清理或对账。
  **OpenCode 会话不是客户长期记忆。**

> **仍未验证**：真实 OpenCode 服务器、真实上游模型凭据、真实模型的解析质量。
> `B1–B9` 全部未通过真实验证，见 `opencode-verification.md` 第 2 节。本轮新增的
> `OpenCodeBackend` 使用注入式 transport 做契约测试，**没有连过真实服务器**。

---

## 5. 工具与上下文

### 两种上下文方式

- **ContextBundle**（宿主预查询）——微信产品的默认方式。商家数据库不会变成模型可以瞄准的工具。
- **受限制的只读工具**——用于有可信数据服务的其他宿主项目。

两种方式受同一条约束：`ContextAssembler._enforce_boundary()` 会丢弃任何 `occurred_at`
晚于 `as_of` 的上下文条目。插件或 Provider 出错时，这一道兜底仍然生效。

### 工具的作用域隔离

`ScopedToolRegistry` 由可信宿主构造并绑定作用域。模型只能提供参数，不能选择作用域：

- 传入不同 `Scope` 调用 → `InputError`
- 参数里出现 `tenant_id` / `merchant_id` / `account_id` / `user_id` / `token` → `InputError`
- 参数里出现 `sql` / `command` / `path` / `delete` / `url` → `InputError`
- 未注册的工具 → `InputError`
- `limit` 超过上限 → **收敛到上限**而不是拒绝（模型要 10000 行是校准问题，不是攻击）

---

## 6. 记忆的三分

核心不写长期事实，只返回 `MemoryProposal`。确认方式严格区分：

| confirmation | 含义 |
| --- | --- |
| `model_inferred` | 模型推断。**不得**因为被反复引用就升级为可信事实 |
| `auto_captured` | 自动记下的观察（例如一次变更请求） |
| `customer_confirmed` | 客户明确确认 |
| `host_confirmed` | 商家在本地明确确认 |

只有后两者 `is_trusted` 为真。每条建议保留 `effective_from`、`supersedes` 和 `source_refs`。

---

## 7. order_agent 的自动记单规则

模型输出**建议**，记不记由确定性代码决定。判定入口是
`order_agent.policy.decide_recording()`，全流程不依赖模型自报置信度。

自动记单需同时满足：

1. 身份明确（宿主已绑定客户，模型说出一个名字不算绑定）
2. 商品在商家目录内
3. 数量存在、为正、且未超上限
4. 单位与目录一致
5. 无未解决的重复消息候选
6. 改量/追加/取消能唯一关联到一张订单
7. 引用的证据原文确实出现在输入里
8. 未触发金额、数量或履约异常

`老样子` 缺少可对应的历史订单时进入 `no_eligible_historical_order`，不猜。

### 最少追问

`build_follow_up()` **最多返回一个**问题，按人实际会用的顺序找答案：

```
当前消息与引用 → 当前订单 → 同客户有效历史订单 → 已认可默认规则
```

能从历史可靠推断的字段不再询问；缺少非必要字段不阻止记单。
**缺少本地上下文时不问客户**，而是返回给宿主补充（`describe_missing`），
因为那是系统该知道的，不是客户该重复的。

---

## 8. 回执

回执由 `render_receipt()` 按**已校验的订单字段**渲染，模型不参与撰写。

`unit_price_cents` 缺失时抛错，除非显式设置 `price_pending=True`——此时回执写「价格稍后确认」，
而不是打印一个没人核对过的 0.00 合计。这一条有测试固定：
`test_a_missing_price_raises_instead_of_defaulting`。

---

## 9. 第二业务如何接入
`packages/support_triage/` 是一个完整、无微信依赖的第二业务：客服消息分类与回复建议。

| | order_agent | support_triage |
| --- | --- | --- |
| 输入 | 客户下单消息 | 任意客服消息 |
| 输出 | intent + items + evidence | category + urgency + reply_suggestion |
| 策略 | 能否自动记单 | 是否需要转人工 |
| 依赖 | 仅 business_agent | 仅 business_agent |

```bash
python -m support_triage --text "我要退款" --json
```

它不导入 pyweixin、订单数据库、Windows UI、产品登录或计费模块。
`test_the_example_runs_with_no_product_on_the_path` 在**只有两个包的干净解释器子进程**里运行它，
以证明这一点不是口头承诺。

### 新增一个业务域的最小步骤

1. 实现 `DomainPlugin`：`validate_request` / `build_prompt` / `validate_output` / `interpret`。
2. 选一个后端（`FakeBackend` 先跑通，`ModelApiBackend` 或 `OpenCodeBackend` 上生产）。
3. 宿主实现 `ContextProvider`（预查询）和 `RunRepository`（需要持久化时）。
4. `AgentCore(backend=..., plugin=...)`，然后 `core.run(request)`。

不需要写执行循环、超时、取消、幂等、证据校验或用量统计——那些已经在核心里。

---

## 10. 安装

独立安装（包之间只依赖彼此，不依赖本仓库）：

```bash
pip install -e packages/business_agent
pip install -e packages/order_agent
pip install -e packages/support_triage
```

`business_agent` **零运行时依赖**，可以在没有微信、没有 Windows、没有 provider SDK 的环境里
安装、导入和测试。

源码树内直接运行（无需安装）：`src/pyweixin/_packages.py` 会把
`packages/*/src` 加入 `sys.path`。这只在集成层调用，通用核心永远不会反向 import 本仓库。

---

## 11. 本轮对现有产品做了什么（复用，不新建）

| 改动 | 之前 | 之后 |
| --- | --- | --- |
| OpenCode 后端 | `pyweixin/opencode.py` 自带一份 HTTP 实现 | 该文件缩为适配器，HTTP/会话/探测/错误分类全部委托 `business_agent.backends.opencode` |
| 依赖方向 | `opencode.py` **反向 import** `orders`（通用后端依赖订单域） | 订单校验改走 `order_agent.contract`，反向依赖消除 |
| 订单输出校验 | `orders.OpenAICompatibleParser.schema` 为唯一定义 | `order_agent.contract.OUTPUT_SCHEMA` 为唯一定义，`orders` 侧继续使用同一校验函数 |
| 订单上下文 | 散在 `local_parse` 与 `orders` | `pyweixin/integration/order.py` 的 `OrderContextProvider` 统一从**现有** `OrderStore` 读取 |
| 落地订单 | `apply_cloud_suggestion` | 不变；`OrderStoreApplier` 复用同一入口，不存在第二条落地路径 |

`pyweixin/opencode.py` 保留原有公开接口（`OpenCodeAdapter` / `LocalRuleAdapter` /
`AdapterRegistry` / `validate_parse_result` / `extract_json_object` / 错误类型），
只把异常类型翻译回产品自己的类型，因此网关 worker、对账路径和 171 项既有测试不受影响。

**没有新建**：第二套登录、第二套任务队列、第二套订单状态机、第二套计费、第二份 SQLite 订单实现。

---

## 12. 测试

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

当前结果：**261 项通过**（原有 171 项 + 新增 90 项）。

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_business_agent_core.py` | 契约、错误分类、幂等、重试边界、取消、超时、工具作用域隔离、三后端契约一致性、依赖方向 |
| `tests/test_order_agent_plugin.py` | 输出契约、自动记单/拦截、最少追问、记忆确认等级、回执、金额格式化 |
| `tests/test_second_business_reuse.py` | 第二业务复用、跨域零耦合、干净解释器运行 |
| `tests/test_agent_integration.py` | 复用现有 OrderStore、租户隔离、时间边界、幂等落地、无平行体系 |

### 尚未验证（不得当作通过）

- 真实 OpenCode 服务器、真实上游模型凭据、真实模型解析质量。
- 真实微信采集、后台不抢焦点、锁屏/RDP 断开。
- 自动记单在真实商户数据上的准确率。
- 任何真实客户群的收发消息。

本轮所有 AI 相关的自动化测试都跑在确定性后端上。`FakeBackend` 永远自报
`model_executed = False`，`test_a_simulated_result_is_never_reported_as_a_model_run`
固定这一点：**模拟通过不等于模型能力通过。**
