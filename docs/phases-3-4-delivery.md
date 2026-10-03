# 阶段 3–4 交付说明：商户可操作的闭环

本文只说**做了什么、验证到什么程度、还差什么**。
架构背景见 [`business-agent-core.md`](business-agent-core.md)；
商户操作说明见 [`merchant-software.md`](merchant-software.md)。

---

## 1. 本轮新增/改动的模块

| 文件 | 职责 | 是否新建体系 |
| --- | --- | --- |
| `wechat_state.py` | 微信七态检测、启动、可停止监测 | 新（纯检测） |
| `conversations.py` | 会话发现、搜索、同名消歧、选择约束 | 新（纯发现） |
| `history_read.py` | 时间范围、进度、取消、恢复、实际覆盖、串行调度 | 新（读历史） |
| `receipts.py` | 回执生成、幂等入队、发送前核对、对账 | **复用现有 `AgentStore.outbox`** |
| `runmode.py` | 运行模式、开关、设置持久化 | 新（配置） |
| `service.py` | 闭环编排 + `request_id/job_id/run_id/order_id` 追溯 | 新（编排） |
| `wizard.py` | 六步向导状态机 | 新（流程） |
| `dashboard.py` | 首页数据（备货汇总、异常、未确认、待核对付款） | 新（只读视图） |
| `application.py` | 界面与业务之间唯一的编排层 | 新（编排） |
| `merchant_ui.py` | Tkinter 界面 | 改写 |
| `merchant_app.py` | 启动入口，去掉 `--group` | 改写 |
| `agent.py` | outbox 新增 3 个状态 | **改现有，不新建** |
| `orders.py` | `confirm()` 替代旧版本 | **修现有缺陷** |
| `gateway_server.py` | 413 前先 drain 请求体 | **修现有缺陷** |

**没有新建**：第二套登录、第二套任务队列、第二套订单状态机、第二套计费、
第二份 SQLite 订单实现、第二套发送系统。

---

## 2. 商户实际操作流程

双击 → 检测微信（未启动点「打开微信」）→ 选会话 → 读历史 → 查额度 →
智能整理 → 首页 → 开启自动接单。

向导走完后配置存 `%LOCALAPPDATA%\pyweixin\settings.json`，**下次启动不重走**。
只有两种情况回到向导：首次启动、微信账号切换（此时旧会话绑定和客户记忆作废）。

界面不再要求 `--group`、环境变量或 API Key。

---

## 3. 真实执行路径

界面上点「开始智能整理」走的是：

```
本地上下文 OrderContextProvider
  → PendingQueue.enqueue
  → MerchantClient.submit_parse        （网关任务，job_id）
  → OpenCodeAdapter（云端 Agent 内部循环）
  → order_agent.validate_output        （结构校验）
  → order_agent.interpret              （自动记单决策）
  → AgentCore 返回 AgentResult
  → OrderStore.apply_cloud_suggestion  （订单规则校验 + 落库）
  → ReceiptOutbox.enqueue              （回执幂等入队）
  → ReceiptOutbox.dispatch             （发送前四项核对）
```

**不是**测试专用路径。`OrderPipeline` 是界面和测试共用的同一个类
（`tests/test_receipt_flow.py` 里的 `test_receipt_is_queued_only_after_the_order_exists`
与 `examples/merchant_demo.py` 走的是同一段代码）。

每次运行记录 `request_id` / `job_id` / `run_id` / `order_id`，
`JobTrace.trace_text()` 输出可读追溯行。

**假后端结果明确标记**：`model_executed=False` 一路传到界面，
首页显示「当前为演示数据，非真实分析结果」，结果摘要追加「（演示结果）」。

---

## 4. 自动记单

`order_agent.policy.decide_recording()` 的判定已通过
`service.OrderPipeline` 接进持久化流程。`test_normal_order_is_recorded_without_asking_the_merchant`
验证正常订单不要求逐单确认。

状态区分：订单 `draft/review/cancel_review/confirmed/cancelled/superseded`、
回执 `queued/sending/sent/sent_unknown/failed/held/suppressed`、
记忆 `model_inferred/auto_captured/customer_confirmed/host_confirmed`。

**客户没回复 → `draft`（客户未确认），不等于明确确认，不等于付款。**
客户声称付款 → `claim_payment=1` + 首页「待核对付款」，永不标为已付款。

### 一致性

| 要求 | 实现 | 测试 |
| --- | --- | --- |
| 订单没保存不发「已记单」 | 落库后才 `_maybe_queue_receipt` | `test_receipt_is_queued_only_after_the_order_exists` |
| 崩溃不重复发回执 | 业务幂等键 = outbox 主键，`INSERT OR IGNORE` | `test_same_order_version_queues_only_once` |
| 发送结果未知不重发 | `sent_unknown` 状态，等对账 | `test_crash_during_send_does_not_resend` |
| 租约崩溃后可恢复 | `recover_outbox()` | `test_receipt_lease_recovers_after_crash` |
| 未过期租约不被抢 | 恢复只回收过期租约 | `test_unexpired_lease_is_not_stolen` |
| 纠错替代旧记录 | `confirm()` 替代 `draft/review/confirmed` 旧版本 | `test_correction_supersedes_the_old_order` |

---

## 5. 自动回复

- 复用 `order_agent.receipt`，模型不参与撰写金额
- 缺价：`allow_price_pending=False` → 不发；`True` → 显示「价格稍后确认」
  （`test_missing_price_does_not_queue_a_zero_receipt` /
  `test_price_pending_queues_a_pending_receipt`）
- 群回执不含地址电话（`test_receipt_contains_no_address_or_phone`）
- 短消息合并窗口：配置项已就位（`merge_window_seconds` /
  `max_merge_wait_seconds`，默认 45/90 秒）；**合并执行本身本轮未接进监听路径**
- 发送前核对账号、会话、开关、订单版本、暂停
- 自身回执识别（`is_own_receipt`），不触发建单
- 历史/回放/补采来源不发回执（`NO_RECEIPT_SOURCES`）
- 不清空草稿：**未实现**。当前 `sender` 走 `Messages.send_messages_to_friend`，
  走的是已有发送路径，**没有专门验证过草稿保护** —— 列为未验证项

---

## 6. 全链路恢复

| 场景 | 处理 | 测试 |
| --- | --- | --- |
| 明确未提交的失败 | 有限重试（`network_error`/`rate_limited`，≤2 次） | `test_network_error_before_submission_is_retried` |
| 可能已提交的断连 | 不重跑，标 `outcome_unknown` | `test_unknown_outcome_is_not_re_executed` |
| 同一请求重复提交 | 幂等键命中，返回首次结果 | `test_retrying_the_same_request_id_is_free` |
| 网关/客户端重启 | 租约回收、待发回执与读取进度持久化 | `test_queued_receipts_survive_a_restart` 等 |
| 超时/取消 | 不重复扣额度 | 由 `AgentCore` 分类保证 |
| 后台线程 | 单线程、幂等停止、无残留 | `test_background_polling_does_not_multiply_threads` |

UI 状态区分准确：网络断开、登录过期、套餐到期、额度不足是四种不同文案
（`test_expired_subscription_and_login_expiry_read_differently`）。

---

## 7. 本轮发现并修复的缺陷

| # | 缺陷 | 影响 | 修复 |
| --- | --- | --- | --- |
| 1 | 网关 413 时先关连接再回包 | 客户端收到连接中止，商户看到「网络错误」而非「内容过大」 | `_drain()` 先读完再回包。8/8 稳定通过（修复前 5 次里 2 次失败） |
| 2 | `confirm()` 只替代 `confirmed` 的旧版本 | 纠错后旧**草稿**仍存活，商户看到同一单两行 | 改为替代 `draft/review/cancel_review/confirmed` |
| 3 | 暂停后回执永久卡在 `held` | 恢复发送后回执永远发不出去 | `set_mode` 离开暂停时 `release_held()` |
| 4 | 整单缺价时不生成回执 | 客户完全不知道单被记下 | `price_pending` 时仍渲染商品并写明价格待定 |
| 5 | `Wizard.read_history` 不接受 `resume` | 恢复功能无法从上层调用 | 补参数并透传 |
| 6 | `describe_missing` 查未填充的 context | 永远误报「没有录入商品资料」 | 改为直接查库 |

**缺陷 1 是上一轮遗留的 flaky 测试的根因**——不是测试问题，是网关的真实行为。

---

## 8. 验证结果（分四类，不混淆）

### 自动化测试

```
python -m unittest discover -s tests   →  全部通过
```

> 本文记的是**当时那一轮**的交付情况。测试项数会一直增长，写死的数字很快
> 就对不上；当前实测数字跑 `examples/check_doc_facts.py` 就能拿到。

| 文件 | 数量 | 覆盖 |
| --- | --- | --- |
| `test_merchant_flow.py` | 25 | 微信七态、登录引导、会话消歧、进度/取消/恢复、额度不足与恢复、可追溯 |
| `test_receipt_flow.py` | 29 | 幂等、暂停、版本核对、未知结果、租约恢复、历史不回复、自身不循环、演示标记 |
| `test_merchant_ui_text.py` | 12 | 界面无技术词、首页内容、备货合并、租户隔离 |
| `test_order_scenarios.py` | 14 | 新单/老样子/追加/改量/取消/询价/付款声称/纠错/未知结果不重跑 |
| `test_recovery_flow.py` | 12 | 设置持久化、换账号清空、线程不叠加、重启恢复、崩溃恢复 |
| 原有 + 上一轮 | 271 | 网关、订单、OpenCode、Agent 核心、第二业务 |

### 真实进程

`examples/smoke_gateway.py` — 16 步真实 `python -m pyweixin.gateway_server` 进程演练，全部通过，
含幂等、额度预留/结算、跨商家隔离、备份恢复、建议落库、故障安全。

### 真实界面（本轮实际打开并截图）

`examples/merchant_demo.py` 真实创建 Tk 窗口，逐页渲染并截图到 `docs/screenshots/`：
01 微信未启动（含「打开微信」按钮）、02 等待扫码、03 已登录、04 会话列表、
05 历史+额度、06 额度不足、07 整理、08 首页、09 自动模式、10 暂停。

首页截图确认显示：微信状态、当前会话、当前模式、暂停按钮、今日订单 8、
需要核对 23、客户未确认 31、待核对付款 0、备货汇总 2 行、剩余额度 128、
「当前为演示数据，非真实分析结果」。

### 未验证（不得当作通过）

| 项 | 状态 |
| --- | --- |
| 真实微信七态的**真实**表现 | **未验证**（用的是 `SimulatedWeChat`） |
| 真实会话发现与群人数 | **未验证** |
| 真实历史可读范围 | **未验证** |
| **后台读取不抢焦点** | **未验证**。读取需要操作微信窗口 |
| **后台发送不抢焦点** | **未验证**。发送需要点击和粘贴 |
| 不清空用户草稿 | **未验证**，未专门实现 |
| 短消息合并执行 | **配置项就位，执行未接进监听路径** |
| 真实 OpenCode（`B1–B9`） | **未验证**，未连过真实服务器 |
| 真实模型解析质量 | **未验证**，无凭据无标注集 |
| 真实模型下自动记单准确率 | **未验证** |
| 真实客户群收发 | **未验证，也未执行**。演示全部发往内存列表 |

本轮 AI 相关测试**全部跑在确定性后端上**。
`SimulatedWeChat.simulated` / `HomeView.simulated` /
`OrderPipeline.simulated` / `JobTrace.model_executed` 一路传递到界面，
演示数据不会在无提示的情况下变成正式经营订单。

---

## 9. 真实支持的监听与发送能力（如实说明）

**当前支持的：**
- 读取历史：会打开聊天/历史窗口、滚动 UI。**需要占用本机前台**。
- 发送回执：走 `Messages.send_messages_to_friend`，需要搜索会话、点击、
  粘贴输入。**需要占用本机前台**。

**当前不支持、且不宣称的：**
- 不抢焦点的后台实时监听
- 不抢焦点的后台发送
- 锁屏 / RDP 断开 / 休眠恢复下的工作
- 短消息合并后只发一条回执

界面上「允许本机被自动接单占用」开关的存在，就是为了如实告知这一点。
