# JYI 生产登录接入

客户端默认网关：`https://gateway.jyisolutions.com`。
授权页面：`https://ai.jyisolutions.com/connect/authorize`。
授权码兑换：`POST https://ai.jyisolutions.com/oauth/token`。
客户端 ID：`pyweixin-desktop`；作用域：`config:read`。这是 JYI 服务端已登记的兼容 ID。
回调：`http://127.0.0.1:<随机端口>/oauth/callback`。

点登录后自动打开系统浏览器，用户登录并批准连接。客户端校验 state，
使用 PKCE S256 兑换一次性授权码。JYI 返回的 `api_key`、`device_token`、
`connection_id` 通过 Windows DPAPI 保存，重启时只恢复与生产网关和授权域匹配的凭据。
旧产品服务器的登录凭据不会发送到 JYI。

服务端登记 SQL 在 `deploy/register_jyi_client.sql`，对应
`D:/Projects/Web/JYI/apps/ai-platform/worker/oauth-routes.ts` 的真实接口。
生产域名由该项目的两个 `wrangler.jsonc` 定义。

2026-10-13 已登记生产客户端，用户确认发布后部署了 JYI AI 平台版本
`c9cb6f4f-b54d-44c9-a42b-6f984f89a2f3`。此前 `/oauth/*` 返回 SPA 404，
原因是 Worker 入口只转发 `/api/*`；生产版本已修复并通过入口回归测试。

发布后检查：授权页面 200；未登录访问 `/oauth/authorize` 返回 JSON
401 `session_missing`；缺字段的 `/oauth/token` 与 `/oauth/credential` 请求返回
JSON 400 `bad_request`；未知 OAuth 路由返回 JSON 404 `unknown_endpoint`；
模型网关 `/healthz` 返回 200。真实用户的浏览器登录、批准授权和凭据兑换
尚未执行，以上探测不代表完整登录验收。

退出登录清除本机保存的凭据。要撤销服务器上的 Key，请在 JYI 的连接设备页面操作；
JYI 的撤销接口需要浏览器会话与 CSRF，设备凭据不能替代浏览器身份。

JYI 模型目录使用 `GET /v1/models`。其模型调用使用
`POST /v1/chat/completions`，目前没有旧产品服务器的订单解析、摘要任务和套餐接口。
本次接入完成生产登录，整理入口显示“已连接 JYI；智能整理接口尚未接入。”。
不会将授权响应的 `allocated_micros` 当作当前余额，也不会将 Key 发往旧任务接口。

本地开发仍可使用 `--gateway http://127.0.0.1:8080` 或设置
`WECHAT_AI_GATEWAY_URL`，原设备码登录流程继续适用于自带产品服务器。

---

## 文字分析接线（JYI chat 直调）

JYI 网关只有透传的 `/v1/chat/completions`，没有产品网关的任务队列，
所以提示词、校验、落地三件事挪到客户端（`src/wechat_ai_assistant/jyi_parse.py`）：

* 订单提示词复用 `order_agent` 的 SYSTEM_PROMPT，输出走同一套
  `validate_order_parse_result`，落地走同一套 `apply_cloud_suggestion`；
* 摘要提示词复用 `summary` 的 SUMMARY_SYSTEM_PROMPT；
* 钥匙用 JYI 登录态，不读本地任何密钥；
* 默认文字模型 `jyi-default`（使用 JYI `/v1/models` 返回的原始 ID），`WECHAT_AI_TEXT_MODEL`
  环境变量可覆盖（切模型先在小群实测意图识别）；
* 额度门对 JYI 不过（JYI 按账号计量，不在客户端结算），
  整理结果页写"用量按 JYI 账号计量"，不谎报"扣费 0"；
* 余额条要画出来，JYI 的 `/oauth/credential` 需下发：
  `quota_micro_usd`、`used_micro_usd`、`reserved_micro_usd`（可缺）、
  `plan_code`、`period_end`、`active`（可缺，默认 true），
  剩余额客户端自己算。

2026-10-16 已接入图片：文字与图片统一使用 `jyi-default` 和
`/v1/chat/completions`，默认路由为 OpenCode Go 的 DeepSeek V4.1 Flash。
客户端把图片压缩为 WebP 再编码成 Base64 data URL；默认开启最高档推理
（`thinking=enabled`、`reasoning_effort=max`），常规请求预算为 131,072 token。
网关按模型目录允许最高 384,000 token，每批最多 5 张图片，
请求最多 8 MiB；音频和视频暂不分析。

当前网关发布版本为 `6eb19c2e-9e64-4e0e-ae04-3084d385b2b5`，包含模型预算、
图片输入及 SSE 完成结算修复。已通过最高档推理单图及此前图文分批合并的真实验收。
调查与验证记录见
[AI 分析调查](ai-analysis-investigation-2026-10-16.md)。
