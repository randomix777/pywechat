# OpenCode 核实记录与尚未验证项

本文件区分两类内容：**已核实的事实**（有出处）和**尚未验证的能力**（不得当作已通过）。
最近一次核实日期：本次开发会话，依据 OpenCode 官方文档与已发布的 OpenAPI 规范。

---

## 1. 已核实（VERIFIED）

来源：`https://opencode.ai/docs/server/`、`/docs/agents/`、`/docs/permissions/`、`/docs/config/`、
`https://opencode.ai/config.json`、`https://opencode.ai/openapi.json`、npm registry、GitHub 发布页。

| 编号 | 结论 | 依据 |
| --- | --- | --- |
| V1 | npm 包名为 **`opencode-ai`**；裸包名 `opencode` 在 registry 返回 404 | registry 查询 |
| V2 | 仓库为 **`anomalyco/opencode`** | 文档页脚 “Edit page” 链接 |
| V3 | 固定版本 **`opencode-ai@1.18.32`**（`latest` dist-tag，GitHub `v1.18.32`） | registry + GitHub |
| V4 | v2（`@opencode-ai/cli@0.0.0-beta-*`）**仅预发布**，配置 schema 不兼容（`agent`→`agents`、`permission` 对象→`permissions` 数组、`bash`→`shell`），**本方案不采用** | docs/v2 + registry |
| V5 | `opencode serve [--port N] [--hostname H] [--cors O]`；端口默认 4096，但 CLI 默认值为 0，4096 占用时**静默退回随机端口**，故必须显式传 `--port` | docs/server + 源码 |
| V6 | 服务端认证由 **`OPENCODE_SERVER_PASSWORD`**（非空）开启 HTTP Basic；用户名默认 `opencode`，可用 `OPENCODE_SERVER_USERNAME` 覆盖；**默认关闭**；无 `--auth` 参数 | docs/server + 源码 |
| V7 | `GET /global/health` → `{"healthy": bool, "version": str}` | docs/server |
| V8 | 会话端点：`POST /session`、`POST /session/{id}/message`、`POST /session/{id}/prompt_async`（204）、`POST /session/{id}/abort`、`DELETE /session/{id}`、`GET /session/{id}/message`、`GET /agent` | docs/server + openapi.json |
| V9 | OpenAPI 3.1 规范由 `GET /doc` 提供 | docs/server |
| V10 | **不存在 OpenAI 兼容端点。** v1 规范（162 path）与 v2 规范（113 path）中 `chat/completions`、`/v1/`、`completions`、`openai` 全部 0 命中；源码路由组枚举亦无对应分组。仓库中出现的 `openai` 只是**出站** provider 适配器 | openapi.json 全量检索 + 源码 |
| V11 | 自定义 Agent 目录为**复数** `agents/`；单数 `agent/` 仅向后兼容；markdown 文件名即 Agent 名，正文即系统提示 | docs/agents + docs/config + 源码 glob |
| V12 | 权限默认值**宽松**：多数权限默认 `allow`，`doom_loop`/`external_directory` 默认 `ask`；无头模式下 `ask` 会永久挂起 | docs/permissions |
| V13 | 权限对象可用的键共 15 个：`read edit glob grep list bash task external_directory todowrite question webfetch websearch lsp doom_loop skill`；额外键（如 `"*"`、MCP 通配）合法 | config.json `$defs` |
| V14 | `tools` 布尔配置自 v1.1.1 起**废弃**，已并入 `permission` | docs/permissions |
| V15 | 模型标识格式为 `<providerId>/<modelId>` | docs/troubleshooting |
| V16 | 顶层 `share` 键存在，取值 `manual` / `auto` / `disabled`；`"disabled"` 合法 | config.json |
| V17 | 顶层 `autoupdate`（boolean \| `"notify"`）、`default_agent`（string）存在 | config.json |
| V18 | `provider.<id>.enabled` **不存在**（`ProviderConfig` 为 `additionalProperties: false`）。限制 provider 应用 `enabled_providers` / `disabled_providers` 或 `experimental.policies` | config.json |
| V19 | `experimental.policies` 条目形状为 `{action, resource, effect}`，`action` 目前仅 `"provider.use"`，`effect` 为 `allow`/`deny`，**后者匹配优先**。**只 deny 不 allow 会让所有 provider 不可用，导致自我锁死** | config.json + docs/policies |
| V20 | **没有** `mcp: false` 之类的总开关；`"mcp": {}` 因为配置是**合并而非替换**而是无效手段 | config.json + docs/config |
| V21 | 会话数据位于数据目录下的 SQLite，可用 `OPENCODE_DB` 指定 | 源码 + docs/troubleshooting |

---

## 2. 尚未验证（UNVERIFIED / 不得宣称通过）

> 本轮**没有可用的 OpenCode 服务器，也没有上游模型凭据**。
> 下列各项**全部未通过真实验证**。本地通过的只是使用模拟适配器（`LocalRuleAdapter`）的队列、额度、取消与恢复路径。
> **模拟结果不能替代真实验证。**

### 2.1 阻塞项

| # | 项目 | 现状 | 如何验证 |
| --- | --- | --- | --- |
| B1 | OpenCode 容器能否正常启动并通过 `GET /global/health` | 未验证 | 启动容器，跑 `docker compose ps` 与健康检查 |
| B2 | 固定的 `ghcr.io/anomalyco/opencode:1.18.32` 镜像是否存在且可拉取 | 未验证 | `docker pull ghcr.io/anomalyco/opencode:1.18.32` |
| B3 | `order-parse` Agent 是否被**实际加载** | 未验证 | `GET /agent` 必须返回该 Agent；仅写文件不算 |
| B4 | **`permission` deny 是否真的生效** | 未验证 | 见下方 B4 说明 |
| B5 | `POST /session/{id}/message` 实际响应结构中 `parts[].type == "text"` 的取值路径 | 未验证 | 抓一次真实响应；适配器已做容错提取，但仍需确认 |
| B6 | `GET /doc` 是否确实返回原始 JSON | 文档写的是 HTML，源码返回 JSON | 实际 curl 一次 |
| B7 | 上游模型 Key 有效，且该账号**允许**承载付费商家用量 | **完全未核实** | 供应商条款 + 实际计费确认 |
| B8 | 真实模型的解析质量（证据正确率、客户归属、订单差异） | 未验证 | 脱敏标注集，按时间切分留出集评估 |
| B9 | 会话清理后磁盘数据确实被删除 | 未验证 | 任务结束后检查 `opencode-data` 卷 |

### B4：如何真正验证权限生效

“写了一份配置”不等于“权限生效”。必须实测：

1. 启动 OpenCode，`GET /agent` 确认 `order-parse` 存在。
2. 用网关适配器向该 Agent 发一次请求，确认返回正常解析结果（说明 Agent 可用）。
3. 构造一个**应当被拒绝**的工具调用场景，或直接审查运行期权限：用 `opencode debug config` 确认 deny 规则已被加载。
4. 确认 `POST /session/{id}/share` 从未被网关调用；`share` 为 `disabled`。
5. 在容器内确认**没有**挂载 `/var/run/docker.sock`、商家目录或源码工作区。

若第 3 步无法证明 deny 生效，**必须明确报告问题并停止对外暴露**。

### 2.2 本地已验证的范围（不含真实环境）

以下 145 个自动化测试通过（`python -m unittest discover -s tests`），覆盖：

- 浏览器验证到客户端自动登录的完整流程（含首次改密强制）
- 授权码过期、一次性领取、拒绝、限速
- 刷新令牌轮换与重放检测、设备吊销、退出所有设备
- 跨商家任务访问被拒、身份不来自请求体
- 重复提交、同键异内容、并发额度预留不超卖
- 适配器超时/非法输出/取消/服务重启恢复（使用**模拟适配器**）
- 上游凭据不进入客户端接口与运营接口
- 会话清理、数据库备份与恢复
- 登录或网关故障不影响本地订单

**这些验证的是本项目的逻辑，不是 OpenCode 的行为，也不是真实模型的准确率。**

---

## 3. 上线前必须完成的事项

- [ ] 用真实凭据跑通 B1–B9。
- [ ] 核实上游账号的**允许用途**（V-B7）。在核实前，**不得**把个人订阅当作可承载付费客户的算力。
- [ ] 在 `/etc/opencode/opencode.json`（托管目录，唯一不可被项目/用户配置覆盖）重复一遍 deny 规则，并实测确认合并后仍然生效。
- [ ] 确认 `experimental.policies` 里 deny 之后**有对应的 allow 条目**，否则 OpenCode 会自我锁死。
- [ ] 把 `opencode.json` 与 `order-parse.md` 中的 `PLACEHOLDER_PROVIDER` / `PLACEHOLDER_MODEL` 换成实际值，两处必须一致。
- [ ] 完成 HTTPS、备份与恢复演练（见 `product-gateway.md` 第 4 节）。
- [ ] 按 `pilot-validation.md` 的三道关口执行影子运行；三个关口目前**全部未通过**。
