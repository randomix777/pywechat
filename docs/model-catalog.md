# 模型列表拉取、用户选择模型、默认免费模型 —— 交付说明

本文只说**做了什么、验证到什么程度、还差什么**。
Agent 核心见 [`business-agent-core.md`](business-agent-core.md)；
商户操作见 [`merchant-software.md`](merchant-software.md)。

---

## 1. 先核实了当前 OpenCode 的实际接口

不猜模型名，不把 `/doc` 当模型列表。对 opencode-ai 1.18.18 实测：

| 端点 | 实测结果 |
| --- | --- |
| `GET /provider` | `{all:[224 个供应商], connected:[9 个], default:{}}` —— **这才是模型目录** |
| 供应商字段 | `id / name / key / options.baseURL / options.headers / env / models` |
| 模型字段 | `id / name / limit.context / limit.output / cost / capabilities / status` |
| `GET /doc` | 162 条路径，**不含模型列表** |
| `GET /config/providers` | 供应商环境变量要求（会回显 `env` 名称，不含值） |

三个必须分清的事实，代码和测试都按这个分：

1. **「已连接」≠「可用」** —— `connected` 只说明凭据存在。
2. **「目录里有」≠「验证通过」** —— `discovered` 和 `selectable` 是两个状态，
   中间隔着一次**真实调用**。
3. **凭据在 `/provider` 的响应里** —— 所以在 `OpenCodeBackend.list_providers()`
   这一层就丢弃 `key`/`options`/`env`，**不是**在更下游过滤。调用方就算忘了过滤也泄露不出去。

`opencode-go`（"OpenCode Go"）33 个模型、已连接；本轮实际验证用
`opencode-go/qwen3.6-plus`。

---

## 2. 数据流

```
OpenCode GET /provider
  → OpenCodeBackend.list_providers()   规范化 + 在此丢弃凭据
  → CatalogStore / ModelCatalog        拉一次、全客户端读缓存、后台刷新
  → OperatorModelPolicy                运营允许列表与能力策略（决定免费与否）
  → ModelEntry.for_client()            只有别名
  → 客户端界面（只有别名、显示名、说明、收费类别、状态）
```

客户端提交任务时**只提交别名**。`provider_id` / `model_id` 由服务端
`ModelEntry.resolve_target()` 解析，并冻结进 `job_model_snapshot`。
`model_catalog.reject_client_model_override()` 挡下 `provider` / `model_id` /
`upstream_url` / `api_key` / `base_url` / `system_prompt` 等字段。

---

## 3. 免费模型的准确含义

三件事分开：

| | 决定者 | 例子 |
| --- | --- | --- |
| 上游价格/订阅成本 | 供应商 | 包含在 OpenCode Go 订阅里 → 上游不额外计费 |
| **对客户是否免费** | **运营配置** `pricing_class` | 只有这里说了算 |
| 扣多少 | **`cost_units_per_job`** | 「每分析一条消息扣 N 次额度」里的 N |

`test_free_is_decided_by_operator_not_by_upstream_price` 固定了这条：
上游 `cost.input == 0` 的模型，对客户**仍然不是免费**。
模型名带 `free`、上游价格为 0、包含在订阅里，**都不构成免费**。

`charge_note` **只是展示文案**，不参与任何扣费计算。收费说明由
`ModelEntry.charge_line()` 从 `cost_units_per_job` 生成；运营自己写的
`charge_note` 只有在它写的次数与策略数字**一致**时才被采用，写错了就以
策略为准（`test_charge_note_cannot_contradict_the_structured_policy`）。
实际扣费同样只认冻结的策略数字，不看适配器自报的 `cost_units`
（`test_paid_job_charges_the_frozen_cost_units_not_the_adapter_value`：
策略配 3 次就真的扣 3 次，而不是适配器写死的 1 次）。

### 3.1 免费额度的完整生命周期

免费任务走 `free_usage` 表（按天、按运营配的时区），**不预留也不扣订阅
额度**；收费任务照常检查余额，不能借免费通道绕过。`charge_note` 与两条通道
的额度都无关。

| 情形 | 免费预留 | 订阅预留 | 说明 |
| --- | --- | --- | --- |
| 成功 | 转成用量（`used+1`） | 释放并按冻结值结算 | `free_settled=1`，重复调用不再计数 |
| 排队中被取消 | **释放** | 释放 | 曾漏释放，见第 10 节 |
| 设备被吊销 | **释放** | 释放 | 曾漏释放，见第 10 节 |
| 等待超时被收掉 | **释放** | 释放 | 确定没执行 |
| 确定失败（后端不可达 / 输出不可用） | 释放 | 释放 | 不计客户用量，记 `internal_retry` |
| **结果未知**（超时 / 5xx） | **保留，等对账** | 保留 | 绝不盲目重发；对账后才落定 |
| 网关重启（有上游会话） | 保留，转待对账 | 保留 | 重启不等于上游没跑 |

跨午夜完成的任务按**预留**那天结算（`free_day` 冻结进任务快照）。每日额度按
`free_quota_timezone` 重置；时区解析不出来会**告警**并回落到 UTC ——
Windows 没有系统时区库，所以 `tzdata` 是必需依赖，不是可选项。

---

## 4. 默认选择与不可用处理

实测（`examples/catalog_live_check.py`，对真实 224 个供应商）：

```
① 拉取真实上游目录（OpenCode GET /provider）
   后端 ok=True 版本=1.18.18
   未配置策略时的目录：0 条（未放行的模型不下发）
② 运营配置：把这个模型放为默认免费模型
   状态=discovered（discovered 表示还没验证过，不可选）
   客户默认=None
   合格免费模型数=0（未验证时应为 0）
③ 客户看到的目录
   {"default_alias":"", "free_available":false, "models":[]}
   未发现上游标识泄露
```

**验证之前**：`free_available=false`、`default_alias=""`、可选列表为空。
没有合格免费模型时任务被拒（`409 免费模型暂不可用`），已读取消息和待处理任务保留，
**不自动切收费模型**，也**不用 FakeBackend 冒充**。

服务端拒绝之后，**客户端也必须把消息留在本地队列里**。以前 `free_quota_exhausted`、
`no_free_model`、`platform_budget_exhausted` 都不在客户端的挂起名单里，
结果消息被标成 `rejected` 丢掉，商户看到的还是「解析排队中」——
一条待接的活就这么消失了。现在这些码一律挂起并显示具体原因
（`test_held_codes_keep_the_message_pending`）。

`test_free_quota_exhaustion_does_not_fall_back_to_paid` 固定了这点：
免费额度用完后返回 402，订阅 `reserved_units` 和 `used_units` 都还是 0。

---

## 5. 订单能力验证

`src/pyweixin/capability_check.py` 用**合成数据**发真实请求，7 项准入标准：

1. 中文数量转 JSON 数字（`两箱` → `2`）
2. 商品与单位正确匹配目录
3. 不编造来源证据（引用必须逐字出现在输入里）
4. 「老样子」引用到历史订单明细
5. 追加 / 改量 / 查询意图能区分
6. 结构化输出能通过现有 schema
7. 信息不足时返回歧义而不是硬猜

**真实模型实测**（`opencode-go/qwen3.6-plus`，200.9 秒，5/5 通过）：

```
PASS chinese_number        PASS append
PASS inquiry               PASS history_reference
PASS insufficient
备注：冒烟验证通过不等于生产准确率已达标
验证后状态=selectable
现在是默认免费模型吗：free-standard
客户视图：Qwen3.6 Plus 免费=True 每天可免费分析 50 次 默认=True 状态=可用
```

结果落库在 `model_verification`（后端、实际 provider/model、提示词版本、逐项检查、
验证时间）。**少量通过只算准入冒烟，不代表生产准确率已达标。**

`FakeBackend` 不进正式目录：`build_catalog()` 跳过没有真实 `provider_id`/`model_id`
的条目（`test_fake_backend_is_not_a_catalog_source`）。

---

## 6. 接入任务、Agent 与计费

任务创建时冻结 `job_model_snapshot`：别名、真实 provider/model、目录版本、
收费类别与策略版本、每日免费上限、提示词与插件版本。**快照与任务在同一个
事务里落库** —— 之前它是提交之后另写一行的，工作线程可能先抢到任务，那时
快照还不存在：免费任务会按默认模型跑完再按收费结算，而免费预留永远结算不掉。

`AgentCore._run_with_job_model()` 用**任务自己的快照**执行，并且把 provider/model
当作**参数**传给适配器。适配器是所有工作线程共用的单例，改它的属性会让同时在跑
的两个任务互相换模型 —— 这一条已由 `test_concurrent_jobs_do_not_swap_each_others_model`
固定。

客户在设置里换模型、目录刷新、上游下线，**都不改变已经冻结的路由**
（`test_changing_the_setting_does_not_change_a_running_job`）。

> **「路由已冻结」不等于「上游下架了也能跑」。** 冻结只保证这一单发出去的
> provider/model 不会变。真实情况是：上游把这个模型下架了，这一单照样会失败 ——
> 区别在于它会**明确失败**（不扣费、释放预留、消息留在待处理），而不是换一个
> 模型蒙混过去。见 `test_delisting_the_model_does_not_change_a_queued_job_route`
> 与 `test_a_delisted_model_that_fails_does_not_charge_or_switch`。

幂等：`request["model"] = 别名` 计入 `request_hash`，所以同一个幂等键换模型
和换内容一样被判冲突（`409 idempotency_key_reuse`）——换模型必须新建任务。
`test_same_key_different_model_conflicts` 固定了这点。

`failover_to` 是**保留字段，当前不实现自动换模型**。上游失败时任务失败并保留待处理，
不会偷偷改用收费模型（`test_failover_to_never_switches_to_a_paid_model`）。

`business_agent` 保持通用：**没有加入套餐、充值或计费逻辑**。
`ModelApiBackend` 仍是通用执行后端；模型解析与计费全在网关。

### 计费版本：收费任务必须带版本

规则在 `_check_pricing_version`，与 `_resolve_model`、冻结快照、预留额度
同在 `ProductGateway._catalog_lock` 里，**没有策略变化窗口**：

| 情况 | 结果 |
| --- | --- |
| 收费任务**没带**版本 | `409 pricing_version_required` + 当前计费事实。不建任务、不预留、不执行 |
| 带了但**过期** | `409 pricing_changed` + 最新事实，**要商户重新确认** |
| 免费模型变成收费，带旧版本 | `pricing_changed` —— 旧请求不能静默按新价继续 |
| 免费模型变成收费，完全没带版本 | `pricing_version_required` —— 同样拦住 |
| 免费任务**没带**版本 | 放行（不扣订阅额度） |
| 带了且一致 | 放行 |

**幂等重放先于价格校验**（`_idempotent_replay`）：同键同请求返回**原任务与
原计费快照**，绝不按当前价格重新收费；同键不同请求仍然 `idempotency_key_reuse`，
不能借幂等机制换模型或换正文。

客户端只把新事实**展示**出来，`confirm_pricing()` 必须由人触发才写回新版本
—— 自动重发等于替商户同意涨价。`pricing_version` 取自 `GET /v1/models` 的
`version`，与网关冻结的 `pricing_version` 是同一个值。

### 两个数字不要混

| 字段 | 含义 | 单位 |
| --- | --- | --- |
| `cost_units_per_job` | 每条任务扣多少**分析额度** | 次数（收费模型；免费模型恒为 0） |
| `daily_free_limit` | 每天免费分析**次数**上限 | 次数/天（免费模型） |

上一版报告把它们混成一句"cost_units_per_job 的值 50 次/天"，那是**报告写错
了**：50 是 `daily_free_limit`，`cost_units_per_job` 在示例里免费模型为 0、
收费模型为 1。代码、配置、界面文案与测试现已一致（示例配置里新增
`_two_numbers` 说明，策略字段加了单位注释）。

### 平台预算与全局并发

平台自己的上限通过环境变量配置（见 `deploy/gateway.env.example`）：
`PYWEIXIN_PLATFORM_DAILY_JOBS`、`PYWEIXIN_PLATFORM_MAX_CONCURRENCY`，可选的
`PYWEIXIN_PLATFORM_COST_CENTS_PER_JOB` / `PYWEIXIN_PLATFORM_DAILY_COST_CENTS`。

- **不伪造人民币成本。** 订阅费用无法可靠按请求折算，默认只记请求数与并发；
  没有单价时运营视图里 `cost_known=false`、`settled_cost_cents_today=null`。
- 免费任务不占付费预算，否则免费额度一开就会把付费通道堵死。
- 预算用尽时停止接收**新**执行任务（`409 platform_budget_exhausted`），
  已在跑的任务照常跑完，客户本地数据一个字节都不动。
- 只用现有的 `jobs` 与 `usage_ledger` 记账，没有新增数据库，也没有消息队列。
- 传进网关的裸 `PlatformBudget` 会自动接上账本 —— 裸配置对象的
  `can_accept` 恒为真，不接账本等于预算保护静默失效。

---

## 7. 界面

- 「第 5 步 智能整理」和「模型设置」都有模型卡片
- 卡片显示：显示名、免费/收费、`每天可免费分析 X 次` 或扣费说明、状态、
  「免费模型（推荐）」
- 首次使用自动拉取，「刷新模型列表」按钮手动刷新
- 拉取失败显示最近缓存并写明更新时间，**不伪装成最新**
- 记住客户选择（`settings.json` 的 `model_alias`）；失效时回落到默认免费模型并说明
- 换模型**只影响新任务**，历史订单和客户记忆不受影响
  （`test_history_and_memory_are_untouched_by_a_model_switch`、
  UI 检查「切模型只改设置不动历史订单」）
- 界面已写明：换模型只影响之后提交的分析；已整理过的消息重新分析会再花一次
  分析次数，但不会生成第二张订单，也不会改动已生效的订单
- 免费次数用完 / 免费模型暂不可用 / 服务暂停接收时，界面显示的是具体原因
  和「已为您保留」，**不会**把消息丢掉后还说「解析排队中」

界面文案不含 `token`/`api key`/`model`/`opencode`/`sqlite` 等技术词
（UI 检查「商户看得见的文案不含技术词」）。

---

## 8. 运营配置示例

`deploy/model-policy.example.json`：

```json
{
  "models": [
    {
      "alias": "free-standard",
      "provider_id": "opencode-go",
      "model_id": "qwen3.6-plus",
      "pricing_class": "free",
      "daily_free_limit": 50,
      "free_quota_timezone": "Asia/Shanghai",
      "is_default": true,
      "max_concurrent_per_tenant": 2,
      "requests_per_minute": 20,
      "cost_units_per_job": 0,
      "policy_version": "1"
    },
    {
      "alias": "paid-deep",
      "provider_id": "opencode-go",
      "model_id": "qwen3.6-plus",
      "pricing_class": "metered",
      "max_concurrent_per_tenant": 1,
      "requests_per_minute": 10,
      "cost_units_per_job": 1,
      "policy_version": "1",
      "charge_note": "该模型会消耗分析额度，每分析一条消息扣 1 次额度"
    }
  ]
}
```

实际扣费只认 `pricing_class` / `cost_units_per_job` / `daily_free_limit` /
`max_concurrent_per_tenant` / `requests_per_minute` / `policy_version`；
`charge_note` 只是展示文案。

启动时通过 `PYWEIXIN_MODEL_POLICY` 指定；网关会把目录持久化到
`product.db`，重启后读回。完整字段说明见文件内的 `_billing_basis` /
`_free_quota_timezone` / `_failover_to` 注释。

---

## 9. 商户看得见的症状清单

这轮审计挖到的问题，绝大多数不是功能错，而是**界面在说谎或装死**：进度条写
「准备中」（其实已完成）、失败消息凭空消失、按钮亮着却按了没用、拖小窗口
功能直接消失。

商户**看得见**这些，但**说不出口** —— 他只会觉得「这软件不好用」。

这些症状和"哪条断言钉住它"的对照，跑 `examples/guard_map.py` 就能拿到。
它还会直接列出**还没有护住的**症状 —— 那才是下一步该做的地方。

> 不往本文抄清单：清单会过期，脚本不会。断言被删了、对照表会立刻报出来。

---

## 10. 默认执行后端

**默认后端是 DeepSeek，直接调用官方接口。** OpenCode 保留为可选的遗留
后端，**不是产品运行的前提**：不配 OpenCode 地址、不装 OpenCode、不起进程，
产品照样跑。

| 环境变量 | 作用 | 是否必需 |
| --- | --- | --- |
| `PYWEIXIN_DEEPSEEK_API_KEY_FILE` | 密钥所在的**文件路径** | 走 DeepSeek 时必需 |
| `PYWEIXIN_DEEPSEEK_MODEL` | 模型，默认 `deepseek-flash` | 否 |
| `PYWEIXIN_DEEPSEEK_BASE_URL` | 接口地址，默认官方地址 | 否 |
| `PYWEIXIN_DEEPSEEK_TIMEOUT` | 单次超时秒数，默认 60 | 否 |
| `PYWEIXIN_OPENCODE_URL` | 配了就走 OpenCode 遗留路径 | 否 |

**密钥只从文件读**，不认环境变量明文、不走命令行：环境变量会进部署日志，
命令行会进 `ps` 输出。路径不是秘密，可以进配置；密钥本身只存在于那个文件里。

三个后端都没配时，退回本地规则适配器，并在界面上**明说那不是模型结果** ——
悄悄用一个桩冒充模型解析，比明确不可用更糟。

> 事实来源见 `packages/business_agent/src/business_agent/backends/deepseek.py`
> 的模块文档（官方文档链接写在最前面，不是凭记忆写的）。

### 未查明的故障

**读取历史偶尔返回 0 条，原因至今没有查明。**

同一个 6 人群（实际 7 条消息），不同时候读到过 7、35、0、0。已排除的假设：

| 假设 | 实验 | 结果 |
| --- | --- | --- |
| 微信窗口收在托盘 | 恢复窗口后连跑 5 轮 | 5/5 稳定，排除 |
| 订单助手占着前台 | 前台状态下连跑 5 轮 | 5/5 稳定，排除 |
| 监视器与读取抢窗口 | `examples/real_race_repro.py` 并发对照 | 修复前后都是 7/7/7/7，排除 |

监视器共用 UI 锁那个改动**本身是对的**（两个线程驱动同一个窗口本就危险），
但它**不是**这个故障的原因。

界面侧已经做了能做的：商户看到的「读到 0 条」和「读到 N 条却入库 0 条」
是两种不同说法，后者目前还没抓到实例。

**不知道原因，就没法保证它不会再发生。** 复现脚本和记录见
`tests/test_unresolved_read_issue.py`——它会在有人把这件事写成"已修复"
时报警。

---

## 11. 验证结果

> 本节按"自动化 / 真实进程 / 真实界面 / 真实上游 / 真实微信"分开报告。
> **测试数量和生成订单数量都不等于正确性** —— 逐条判据见
> `tests/test_billing_free_path.py` 与 `examples/real_chain_check.py`。

### 自动化

```
python -m unittest discover -s tests   →  见 `examples/check_doc_facts.py`（不要抄这里的数字，会过期）
python -m unittest tests.test_business_landing  →  OK
python examples/real_model_check.py --routing-only  →  路由证明 8/8 通过
examples/check_hygiene.py              →  clean
examples/closing_checks.py             →  全部通过（含 ⑥ 别名一路走到请求体）
```

`tests/test_billing_free_path.py`、`tests/test_clarification_reanalysis.py`、
`tests/test_business_landing.py` 是本轮收尾新增的，逐条对应第 10 节的缺陷。
`tests/test_business_landing.py` 是**业务落地测试**（用 `ScriptedBackend`
返回预先写好的结构化结果），它证明的是"有效结果落到订单、版本、备货是否正确"，
**不是**模型理解能力；`ScriptedBackend.is_model` 恒为 False，任何准入都会拒它。
`tests/test_concurrency_budget.py` 用**真的并行 HTTP 请求**（`threading.Barrier`
同步发起）验证并发场景，不是把顺序调用写长一点。

### 真实进程

`examples/smoke_gateway.py` 16 步全过（真实 `python -m pyweixin.gateway_server` 进程）。
平台预算在真实进程里的读数：

```
platform budget: {"daily_paid_jobs": 40, "paid_jobs_today": 0,
  "max_paid_concurrency": 2, "running_paid_jobs": 0,
  "daily_cost_cents": null, "cost_cents_per_paid_job": null,
  "settled_cost_cents_today": null, "cost_known": false,
  "accepting": true, "configured": true}
```

`cost_known=false` 是**如实**的：订阅成本无法按请求折算，所以不编金额。

### 真实界面

`examples/merchant_ui_check.py`（真实 tkinter 窗口）覆盖模型卡片渲染、默认
免费选择、切模型不动历史订单、商户文案无技术词，以及每一屏的文案与控件
层级。**当前通过几项由 `examples/check_doc_facts.py` 实测为准** —— 这个
数字每次加断言都会变，抄进文档必然过期。

### 真实上游

**本轮仍然没有跑过真实模型。** 之前那一次（OpenCode 1.18.18 +
`opencode-go/qwen3.6-plus`，能力验证 5/5）是在第 10 节第 1 条缺陷**存在**的
时候跑的：请求体里根本没有 `model` 字段，实际执行用的是 OpenCode 侧 agent
配置里钉死的模型。本机 `~/.config/opencode/opencode.json` 里
`agent.order-parse.model` 正是 `opencode-go/qwen3.6-plus` —— 所以那次
「验证通过」验证的是 agent 配的那个模型，**不**证明"客户选哪个模型就执行
哪个模型"。

那次结果**作废**。原因不是它错了，而是它测的不是它声称在测的东西。

本机 `opencode serve --port 4096` 仍在运行（PID 3624，09:21 启动，服务里还有
godot-ai、blender-mcp、opencodex、openrouter 等其他 provider，是**共享
服务**）。它要求 HTTP Basic 认证，而密码只存在于那个进程的启动环境里，
当前会话、用户级环境变量、以及 `~/.config/opencode/` 下的任何配置文件中都
没有。未重启该服务，也未改动它的凭据。

### 本项目的独立实例（已准备并验证隔离）

`examples/prepare_isolated_opencode.py` 为本项目准备了一个**独立**的
OpenCode 1.18.18 实例，与共享的 4096 互不影响：

| 项 | 值 |
| --- | --- |
| 地址 | 127.0.0.1:高位随机端口（见 `.opencode-private/instance.json`） |
| 配置 | `.opencode-private/config/opencode/opencode.json` —— 只有 `order-parse` 一个 agent |
| 数据 / 缓存 / 应用数据 | `.opencode-private/{data,cache,appdata,state}` |
| 认证口令 | 随机生成，写入 `.opencode-private/secret.pw`（已 `chmod 600` + `icacls` 收紧） |
| 启动参数 | `serve --port … --hostname 127.0.0.1 --pure`（`--pure` 不加载外部插件） |

**隔离是实测的，不是假设的**（脚本会先起临时实例、拿 HTTP 响应验证，
验证不过就停掉并报告）：

```
health: {'healthy': True, 'version': '1.18.18'}
agents: ['order-parse']                 ← 没有 build/plan/general/explore
connected_providers: ['google-vertex', 'google-vertex-anthropic', 'opencode']
                                         ← 没有 opencodex/openrouter/agnes/godot-ai/blender-mcp
auth list: 0 credentials（私有 auth.json 路径在 .opencode-private 下）
```

`XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_CACHE_HOME` 加 `APPDATA` /
`LOCALAPPDATA` / `OPENCODE` 在 1.18.18 上**确实**生效；`auth list` 显示私有
凭据文件位于私有目录，说明全局凭据库也没有被继承。

### 真实模型重跑仍缺的一步

私有实例里 **0 个凭据**，因此一次真实调用返回 `HTTP 500`（上游未认证）。
按约定**不计为通过**，也不重试、不换模型、不换供应商。

需要你亲自执行（我不会替你登录、购买额度或更换账号）：

```powershell
python examples/prepare_isolated_opencode.py --auth-login opencode-go
```

登录只写进这个私有目录，对共享实例 4096 没有任何影响。登录完成后：

```powershell
$env:PYWEIXIN_OPENCODE_URL        = (Get-Content .opencode-private\instance.json | ConvertFrom-Json).base
$env:PYWEIXIN_OPENCODE_USER       = "pyweixin"
$env:PYWEIXIN_OPENCODE_PASSWORD_FILE = "$PWD\.opencode-private\secret.pw"
python examples\real_model_check.py --max-calls 5
```

### 真实微信

**未验证。** 本机微信停在扫码页（`RealWeChatProbe` → `waiting_scan`，
"检测到微信登录窗口"）。真实会话一条都读不到。所有逐条业务结果都是**合成
数据**，已单独标注。真实发送始终关闭。

### 未验证

| 项 | 状态 |
| --- | --- |
| 真实模型（`opencode-go/qwen3.6-plus`）重跑能力验证与逐条验收 | **未验证**，缺 4096 端口的 Basic 密码；旧的"5/5 通过"已作废 |
| 多供应商多模型的完整对照 | **未验证**（只验证过 `opencode-go` 一个） |
| 上游模型下架后的**真实**演练 | **未验证**（逻辑有测试，见 3.1 与第 10 节；没有真的让上游下架） |
| 真实商户的免费额度数值是否合适 | **未验证**，50 次/天是拍的 |
| 真实模型的生产准确率 | **未验证**，5 项冒烟只是准入 |
| 真实微信会话 | **未验证**，微信停在扫码页 |
| 换模型重新分析的**真实**端到端 | **未验证**，入口保持关闭 |

---

## 12. 本轮修掉的真实缺陷

### 10.1 收尾核实查出来的（会直接算错钱或算错结果）

上一轮（10.2 之前的 17 条）已在上文分节记录。本轮新增：

| 缺陷 | 影响 | 修复 |
| --- | --- | --- |
| `_pending_message_ids()` 返回**全部**消息 | 点第二次"开始智能整理"会对已处理消息再发真实请求、再扣一次费，然后被判 `duplicate` | 排除已分析过的消息；换模型重分析走独立差异流程，入口在端到端验过前保持关闭 |
| `wizard.analyze` 本地算扣费（`charged += 1`） | 既把免费任务算成收费，也和 `cost_units_per_job` 对不上 | 汇总只读网关下发的 `billing.charged_units` |
| `_review()` 为身份不明/非文字/疑似重复建**空订单** | 空订单进订单数、进待核对数，商户以为有生意要做 | 这三类改开独立澄清任务；`_review` 只留给系统重试类 |
| 歧义分支建**不带明细**的审核订单 | 商户看不到软件理解成什么，也没法在核对界面改 | 把可解析的明细写进去 |
| `claim_payment` 列从来没被写过 | 付款声称在对账界面完全不可见 | 唯一定位到订单就挂 `claim_payment=1`，`acknowledged` 恒为 false |
| `payment_claim` 被"没有明细"分支截胡 | 在到达付款处理前就被当成"客户没说清要什么" | 付款声称提前分流 |
| `_order_customer_candidates` 用了不存在的 `customers.group_id` | `no such column`，澄清路径直接崩 | 改查 `aliases` |
| 差异把 `2.0` 和 `2` 判成不同 | 每次重跑都报一堆假差异，真正的变化被淹没 | 数量按数值比较 |
| 差异的 `customer_changed` 只要建议写了客户名就为真 | 每条都报"客户变了" | 只在指认的客户**不是同一个人**时才算 |
| 差异的消息身份从模型输出里取 | `result_json` 没有 `message_id`，永远算成"从无到有" | 从解析运行取 |
| 过期检查漏了"已挂上更新的待核对版本" | 旧比较结果覆盖新订单 | 三种"订单行往前走"都判 stale |
| `_migrate_parse_runs()` 排在需要 `run_key` 的 DDL 之后 | v2 库升级直接崩 | 迁移提到建表脚本之前 |
| **追加**产生同一 SKU 的两行明细 | 订单页看着像两笔生意 | 追加累加到同一行；改量才是替换 |
| `_review_task` 把 `cancel_review` 按成 `review` | 商户分不出"要取消"和"要改量" | 取消保持 `cancel_review` |
| 备货按 `history_order_id` 压制前一单，**不看 `change_type`** | 「老样子」新单会把被参照的那一单从备货里抹掉 → 少备 | 只有 `modify`/`cancel` 才压制；`new` 是另一笔生意 |
| 收费任务**没带**计费版本也被放行 | 不知情收费 | `pricing_version_required`，不建任务不预留 |
| 幂等重放要走价格校验 | 事后涨价会把老任务按新价重收 | 幂等重放先于价格校验，返回原任务与原快照 |
| 校验/冻结/预留之间无锁 | 存在"按旧价确认、按新价扣费"的窗口 | 三步同在 `_catalog_lock` 内，目录写入口拿同一把锁 |
| **系统解析失败建空订单**占位 | 商户看到没有明细的"待核对订单"，分不清是客户没说清还是系统没跑成 | `open_system_failure` 记在解析运行上，可引用真实订单但不改其内容状态 |
| `open_system_failure` 另建一条 `parse_run` | 失败次数翻倍 | 优先挂到已失败的运行上 |
| 差异判据里 `all([])` 恒真 | 假绿灯 | `has_items` 前置，且要求期望的副作用（问/记/建单）都发生 |

### 10.2 上一轮修掉的（订单目录与计费基础）

| # | 缺陷 | 影响 | 修复 |
| --- | --- | --- | --- |
| 1 | 适配器没有 `provider_id`/`model_id` 透传，网关改的是**没人读的副本** | **客户选的模型根本没进执行后端**；能力验证验证的也是默认模型 | 路由作为**参数**一路传到 `OpenCodeBackend.run` 写进请求体；并发下不会互相换模型 |
| 2 | 收费按适配器自报的 `cost_units`（恒为 1） | 运营配「扣 3 次」实际只扣 1 次，**显示与扣费不一致** | 只认冻结的 `cost_units_per_job`；对账补记同一口径 |
| 3 | 任务与快照分两次提交 | 免费任务可能被先抢到，用默认模型跑完再按**收费**结算 | 快照与任务同一事务落库 |
| 4 | 取消排队中的免费任务只释放订阅预留 | `free_usage.reserved` 永不归还，**白扣商户一次免费** | `release_all_reservations` 统一收口两条预留 |
| 5 | 设备吊销只释放 `reserved_units>0` 的任务 | 免费任务的额度同样不还 | 同上；重复吊销仍幂等 |
| 6 | 排队超时的任务无人收尾 | 永久占着并发名额与预留，商户看到一个永不动的任务 | `expire_stalled_jobs` 只收"确定没执行"的 |
| 7 | `PlatformBudgetLedger.running_paid_jobs` 数的是 `charged_units` | 排队/执行中的付费任务一个都数不到，**全局并发上限从未生效** | 数 `reserved_units>0 OR charged_units>0` |
| 8 | `build_gateway` 从不接平台预算 | 生产里平台预算保护**等于没配** | 环境变量装配 + 裸配置自动接账本 |
| 9 | 客户端把 `free_quota_exhausted`/`no_free_model`/`platform_budget_exhausted` 当失败 | 消息被从本地队列**丢掉**，状态还显示「解析排队中」 | 一律挂起，显示具体原因 |
| 10 | 结算的估算成本按额度数乘单价 | 一个任务扣多份额度时高估 | 按**任务**计数 |
| 11 | `charge_note` 覆盖按策略生成的说明 | 文案写「扣 1 次」而策略是 3 次 → 显示与扣费不一致 | 冲突时以策略为准 |
| 12 | 每日额度时区解析失败被静默吞掉 | 配了 `Asia/Shanghai` 也按 UTC 记账，**静默的计费错误** | 告警 + 回落；`tzdata` 进依赖 |
| 13 | `run_parse` 丢弃 `deadline` | 冻结的 `max_task_seconds` 不约束实际执行 | 用剩余时间作为这次调用的读超时 |
| 14 | `model_catalog` 用了未导入的 `Callable` | 注解求值时 `NameError` | 补导入 |
| 15 | `merchant_app --check` 只在 `__main__` 里重配 stdout | 非 UTF-8 控制台上自检直接崩 | 移进 `main()` |
| 16 | `settle_free`/`release_free` 等用 `utc_now()` 而非 store 时钟 | 测试假时钟与账本对不上 | 统一走 `self._clock()` |
| 17 | 验收脚本把空明细算作「数量正确」 | 5/7 的假绿灯；「老样子」无历史时也报「引用历史并建单」 | 判据收紧：空明细/缺意图/无证据一律不符合预期 |

### 10.3 更早一轮修掉的（聊天记录解析等既有缺陷）

| 缺陷 | 影响 | 修复 |
| --- | --- | --- |
| 聊天记录时间戳正则不认 ISO 形式且只剥一次 | **消息正文会残留时间戳**，让解析出的证据永远对不上客户原话 | 中文/英文两套正则都覆盖 ISO 形式，且循环剥离 |
| `parse_group_messages` 依赖语言探测结果 | 语言探测失败回落到中文后，英文界面的时间戳不被剥离 | 同上 |
| 适配器固定 `cost_units=1` | **免费任务完成后仍被扣 1 次订阅额度** | 计费按任务冻结的快照，不按适配器返回值 |
| `model` 字段在规范化时被丢弃 | 客户选了模型但后端用默认 | 规范化保留别名 |
| 目录层用客户端错误类型 | 别名不存在时返回 500，商户以为是服务器坏了 | 网关翻译成 409 |
| 别名由上游 id 派生 | **客户能从别名看出模型名**，绕过"只选别名" | 改为运营命名，目录按声明的上游目标匹配 |
| `smoke_gateway.py` 缺 `sys.path` | 脚本跑到后半段才崩 | 补上 |

---

## 13. 仍需人工决定或补料的

| 项 | 需要什么 |
| --- | --- |
| 真实模型重跑 | 私有实例已就绪且隔离已验证，只差**上游登录**：`python examples/prepare_isolated_opencode.py --auth-login opencode-go`。登录必须由你亲自完成 |
| 共享实例 4096 | **保持不动**。它的口令只存在于那个进程的启动环境里；需要它时请自行在该进程的环境里查看，不要从内存里提取 |
| 真实微信逐条验收 | 在**微信官方窗口**扫码登录；并由商户指定：测哪个会话、读取哪段时间范围、多少条 |
| 换模型「重新分析」入口 | 差异流程已实现且有测试，**入口仍关闭**（`REANALYSIS_ENABLED = False`）。端到端验过再打开 |

---

## 14. 本轮补充：路由核对与"请求模型 ≠ 实际模型"

任务与能力验证现在**都**把 provider/model 当作参数传到底层请求体（不再改
适配器属性），并且会**核对上游实际跑的是哪个模型**：

| 情况 | `model_confirmed` | 记什么 |
| --- | --- | --- |
| 上游自报与请求一致 | True | 无警告 |
| 上游自报**不一致** | False | 「上游实际执行的模型与请求不一致：请求 X，实际 Y」，**不算**该模型的验证成功 |
| 上游**没回**模型标识 | False | 「未能独立确认实际执行的模型」—— 不编造一致性证据 |

请求模型与自报模型分别记在 `jobs.model_requested` / `jobs.model_reported`，
脱敏后的请求结构证据记在 `jobs.request_evidence`（只有方法、路径模板、
agent、model 和 payload 的键，**没有认证头和密钥**）。

`examples/real_model_check.py --routing-only` 不需要凭据就能验证前六项
（注入传输层断言请求体与并发路由）；`--max-calls N` 给第二部分的真实请求
设硬上限，跑不完的用例标为"按上限未运行"，**不假装跑过**。
