# 发送人识别：真机排查结论

这份文档记录**在真机上逐条验证过的结果**，避免以后重走。
每一行都是实测的，不是推理。

测试环境：微信 4.x（`xwechat` 新架构），群 `6⃣️5⃣️TJ锦绣中华水果群4⃣️`（424 人）。

## 2026-10-13 补记七：整理角色（多用途第一刀）

* 工具从"只给微商接单"变多用途，第一刀是角色预设（`src/pyweixin/roles.py`），
  不是改名：订单助手（老链路不动）/ 活动助手（报名/时间地点/物资/通知归类）/
  通用整理（提问/事项/通知/闲聊），整理页每次单选，默认订单助手。
* 订单之外走本机规则引擎：不调模型、不扣费（不过额度门）、不建单、
  不占"已整理"状态（换角色可重跑），产出只进人工核对。拿不准进闲聊，
  非文字进"非文字"桶并写明要人工看。
* 云端模型的提示词在服务端，客户端改不了 —— 模型级角色等网关支持新任务
  类型再接，同一角色接口换引擎即可。

## 2026-10-13 补记六：读历史也走解密库（第 3 步不再碰界面）

* `message/message_0.db`：会话表 `Msg_<md5(username)>`，列含发送人
  （文本正文 `发送人:\n正文` 前缀）、`local_type`（1 文本 / 3 图片 /
  34 语音 / 43 视频 / 10000 系统通知）、`create_time`（秒）、`sort_seq`
  （毫秒排序）、`server_id`（稳定 ID）。备注名走 `contact` 表批量解。
* 手机导入会建新分片（`message_1.db`…）：只读 message_0 会漏整个导入群
  （真机 case：导入群 0 条）。读时扫全部 `message_*.db` 再按时间合并、
  server_id 去重；WAL 同样逐库合。
* WAL 必须合：今天的新消息还在 `-wal` 里。帧头 24B 的 commit 标记决定
  取舍，没提交的尾巴不要；帧页逐页验签，坏帧跳过。`wcdb.apply_wal`。
* `HistoryReader` 新增 `wcdb` 源（群聊私聊都走），members 默认在有钥匙时
  自动升级，失败回落老链路；整轮复用同一份解密产物，读完即删。
  界面发送人选项加"数据库直读"，有钥匙才可选、自动选中。
* 精度：同 UIA 时代一致 —— 文字进解析，图片/语音/视频进待核对，
  系统通知剥标签进待核对。私聊里自己发的显示自己昵称（不特殊标"我"）。
* 长文本存成 ZSTD 整帧（魔数 `28 b5 2f fd`）：不解连发送人前缀都看不到，
  第 5 步内容全空就是这么来的。读时先解帧再拆发送人（`zstandard` 已进依赖）。
  图片行的帧里是图片 CDN 元数据，暂不下载，kind 照写图片进待核对。
* 冷群（本地没存过它的消息表）：不记 0 条，记 error 覆盖带指引
  （"没有同步历史信息到微信桌面端，请手动同步"），覆盖行显示"读失败：原因"，
  不回落其它路（回落也读不到）。

## 2026-10-13 补记五：解密库接通，第 2 步一步到位

* 网上方案：[`wcdb-key-tool`](https://github.com/TANGandXUE/wcdb-key-tool)（MIT，
  `third_party/wcdb-key-tool/`）。关键情报：4.1+ 内存里只剩 passphrase，
  没有 raw key —— 之前全堆暴力注定打空；Windows 主路径是定位
  `com.Tencent.WCDB.Config.Cipher` 对象解候选 key，再 HMAC 验签。
* 实测：20/20 密钥一次提中，20 库全解密且 HMAC 通过。`contact` 表备注/
  昵称明文，`chatroom_member` 给人数，`SessionTable` 给最近会话。
* 效果：第 2 步 82 群全部真名（之前 21 个），0.1 秒（之前 14 秒），
  人数真实（424 人对上文档记载群），好友 488 个。`src/pyweixin/wcdb.py`
  只读拷贝、用完即删，失败回落内存路；第 2 步加「提取密钥（一次）」按钮。
* 钥匙跟微信大版本走，大版本升级换盐后要重提（验签失败会明说，不会静默错）。
  微信还会新建分片/库（手机导入建 message_1.db）：磁盘上有没钥匙的库时
  点读取自动补提（5 分钟节流），按钮可手动重提。
* 隐私：解密产物只落工作目录、`cleanup` 读完即删；密钥文件放应用数据目录。

## 2026-10-13 补记四：`contact_fts.db` 解密研究（只读手段走不通）

* 动机：第 2 步内存投票只能认出常用群（21/200），`contact_fts.db` 里有
  全量群名（`contact_fts_v5` 表），解开则第 2 步一步到位。
* 已确认：拷贝可得（文件锁是间歇性的，直接复制成功，无需 VSS/挂起进程，
  用完即删）；页几何符合 SQLCipher（4096/80）；二进制里有 SQLCipher
  pragma 字符串（`cipher_default_kdf_algorithm`、`PBKDF2_HMAC_SHA512`…）；
  三个库的 salt 都能在内存 schema 页里定位。
* 走不通：内存里没有 `x'<64hex><32hex>'` ASCII 形态的 key（0 命中）；
  全私有堆 52.7 万高熵 32B 候选直接 AES 试解 page1 无命中（已修正
  page1 前 16 字节是明文 salt 的切片错误）；salt 邻域是表结构元数据
  不是 key 结构；KDF（SHA512/256000、SHA1/64000、SHA1/4000）与
  page-size（1024/2048/4096）组合在小候选集上无命中；`.material`、
  MMKV 均为加密或混淆。
* 结论：key 只能走注入式手段（hook `sqlite3_key`/PRAGMA），有被微信检出
  风险，不在主力号上做。重试探针沉淀在 `inspect/localdb_heapkey.py`
  （微信升级后可重跑）。第 2 步用解密库直读，内存只做回落。

## 2026-10-13 补记三：第 2 步群名问题（本机 200 群只有 9 个有名）

* 现象：`list_group_conversations` 扫出 200 个群 wxid，191 个显示原始编号。
* 根因有三：① 每群只试前 5 处内存，资料 record 藏在第几十次命中之后
  （实测首个中文名在第 39 次）；② 认出名字也不显示 —— 重画只查备注表，
  内存辨认出的 `Conversation.name` 被 wxid 原文盖掉；③ `resolve_group_wxid`
  第一命中就返回，群名出现在多群消息里时会串群（实测把 A 群指到 B 群）。
* 修法（`localdb` / `conversations` / `application` / `merchant_ui`）：
  单遍上限 5→20；反查改全量投票，并列分不清返回
  None；wxid 输入直接认；显示优先级 备注 > 内存真名 > 编号；
  真名在前编号沉底。内存时代的「按群名找」「备注改名」两个手动拐杖
  已随解密库接通而移除（第 2 步全真名，不需要了）。
* 精度边界（实测）：内存里没有资料行的群，投出来的是重复出现的消息正文
  （"好的，谢谢"这种）—— wxid 归属是对的，只是显示名不对，读一次历史
  （面板标题校准）即纠正。带控制符/换行的脏串直接不要。
* 磁盘 `db_storage` 在这台机器上有文件但被进程锁死 + 全加密，不走。

## 2026-10-13 补记二：4.1.15 上 UIA 整条路都死了（群名/成员/磁盘都堵死）

* 主窗口自绘黑盒：会话列表、通讯录、搜索全打不开；
* 历史窗口依赖主窗口打开（ChatHistoryButton），也连带失效；
* 磁盘库（`db_storage/*.db` 与 -wal）**全加密**，非 SQLite 头，
  不解密读不出来。
* **活下来的两条路**：① 登录态走 wxid 数据目录（进程映射表）；
  ② 消息走内存 carve（`src/pyweixin/localdb.py`）。
* 群名补齐：**读历史时面板标题带群名**，顺手落备注映射
  （`application._record_group_alias` → `wxid_aliases.json`）。
  加上读前滚成员名单、投票制命名，常用的群越读越全。

4.1.13 时界面至少还有 `mmui::` 节点可读；升到 4.1.15.55 后，
全系统零个 mmui 节点 —— 整个主窗口是 `Qt51514QWindowIcon` 套
`MMUIRenderSubWindowHW` 黑盒，UIA 树里只有 2 个 Pane。
pywinauto 的查找、遍历、点击、头像判断，在这个版本**全部失效**。

活下来的只有两条不碰界面的路：

* **登录态检测**：改从 wxid 数据目录认（进程内存映射表里找 MMKV 路径，
  找得到就是登着），不再依赖窗口类名；
* **消息读取**：`src/pyweixin/localdb.py` 从内存捡解密好的 SQLite 记录。

## 结论先说

**这个微信版本的界面不通过 UIA 暴露群消息的发送人。**
发送人是**自绘**的（画在气泡上方），不进 UIA 文本树。

## 逐条验证

| 路线 | 结果 | 证据 |
| --- | --- | --- |
| UIA 文本 | ❌ 只有正文 | `window_text = '拖地浓缩片50瓶一瓶100片，  5块\n\n蜂花各一箱…'` |
| 子控件 / `descendants()` | ❌ 全空 | 聊天记录面板与主窗口都是 |
| 群成员名单匹配 | ❌ 成员拿到了，没名字可匹配 | `get_groupMembers_info` 7.2 秒取到 **424 个**；但正文里没有成员名，`^{member}(\s|$)` 永不匹配 |
| 两次遍历对齐 | ❌ 必然失败 | 两遍 `select` 都是 False，文本完全相同（`tests/test_sender_two_paths.py` 证明） |
| 多选遍历 | ❌ COM 错误 + 挂死风险 | `_ctypes.COMError: (-2147220991)`；`Tools.select_chatHistoryList` 内含 `while True` 无界循环 |
| 剪贴板 `Ctrl+A`/`Ctrl+C` | ❌ 没选中任何东西 | 剪贴板内容仍是上一次的旧文本 |
| 「多选」按钮 | ❌ 不存在 | 面板只有 3 个按钮：`Voice Input`、`Minimize`、`Disable` |
| 右键 + 复制 | ❌ 同一个 COM 错误 | 崩在 `uia_element_info.ElementFromHandle` |
| 本地消息库 | ❌ 这台机器上没有 | `xwechat/radium/users/<id>/` 下只有 `applet`、`mmkv`、`udr`、`xworker`，无 `db_storage` |
| **换 pywinauto 后端 `win32`** | ❌ 不比 UIA 多给什么 | 两种后端都只看到 2 个子控件 |
| **全部 UIA 属性** | ❌ 发送人不在任何属性里 | 见下 |

## 两条决定性的证据

### 一、整个界面渲染在一个自绘窗口里

    Qt51514QWindowIcon      'Weixin'
    MMUIRenderSubWindowHW   'MMUIRenderSubWindowHW'

`MMUIRenderSubWindowHW` 是**自绘渲染面**，所有界面元素都画在这一个 HWND 上。
UIA 只能读到 WeChat **主动发布**的那部分，而它没发布发送人。

### 二、每个消息条目的全部 UIA属性

    class_name    = mmui::ChatTextItemView
    name          = '纠正\n\n神仙果$11/袋... 2026-10-4 19:58'   ← 只有正文和时间
    automation_id = 'chat_log_message_list.qt_scrollarea_viewport.chat_bubble_item_view'
    framework_id  = 'Qt'
    help_text / value / label / item_status / item_type = None

**`automation_id` 每条消息完全相同**（没有逐条标识），其余属性全 `None`。
发送人在 UIA 的任何位置都不存在——不是我们没读到，是**没被暴露**。

## 已排除的猜测

* **「群名带 emoji 导致搜不到」** —— 错。该群名搜索、打开、识别群聊**全部正常**。
  之前的 `NoSuchFriendError` 是测试里用了**不存在的会话名**。
* **「焦点不在微信」** —— 错。前台窗口确实是 `WeChat`（但 Ctrl+A 在聊天列表里
  本来就不选中任何东西，所以剪贴板保持旧内容）。

## 顺带确认能用的

* `Contacts.get_groupMembers_info` —— **正常**，7.2 秒 / 424 个成员
* `Messages.dump_chat_history` —— **正常**，1.0 秒读到消息
* 读取历史这条链本身没问题，**只有发送人拿不到**

## 代码里的真实 bug（已修）

`WeChatAuto.dump_chat_history` 原本 `select=save_detail`。pyweixin 传
`save_detail=False`（要发送人不要图片视频），于是「两次遍历」拿到的是同一种东西。
已改成 `need_select = not groupMembers and is_group_chat` —— **成员是主路，
多选是兜底**（多选会崩，必须能退回去）。

## 还值得试的

**界面这条路已经走完了**（上面十一条全是实测排除）。剩下的都不在界面上：

### ① 让微信把消息存到本地，再读数据库

这是 `WeChatMsg`、`pywxdump` 一类工具走的路，也是目前**最可能拿到发送人的非 OCR 方案**。

需要三步：
1. 微信设置里**打开「保留聊天记录」**（默认可能没开，所以本机找不到 `db_storage`）
2. 找到 `db_storage` 下的消息库
3. **解密** —— 微信 4.x 的库是加密的，密钥要从进程内存里取

代价：要读进程内存，属于非官方手段；有被微信判定为异常的风险；版本一变就可能失效。
但它是**唯一已被多个开源项目验证过**的路径。

### ② 换更旧的微信 3.x

那一代界面把「发送人 内容」渲染进可访问文本 —— 现有这套代码就是照它写的，
所以逻辑上自洽，真机上读不到只是因为**微信换了**。
代价：老版本有安全风险，且用户未必愿意降级。

### ③ 升/降 pywinauto

`-2147220991` 像是版本不兼容。但要说清楚：**它只影响「多选遍历」那条兜底路**，
不影响主路（成员名单 + 普通遍历），所以优先级最低。

## 复现命令

    .\.venv\Scripts\python.exe -X utf8 examples\raw_text_probe.py "群名" 5

## 后续：头像回填（已实现，默认开启）

UIA 文本里没有发送人，但气泡左侧头像是真实存在的（`Tools.is_my_bubble`
就是靠它判自己/对方），资料卡里的昵称备注也可读。于是加了一条新路：

* `utils.resolve_group_senders_via_avatar` —— 逐条点头像读资料卡；
  卡片文本树为空时自动改走右键复制读剪贴板；
* `utils.avatar_click_point` —— 不用固定偏移，截图实测头像像素团中心
  （只看顶部 60px，长图下半不算；DPI 换算），找不着才回落；
  滚出可视区的条目直接跳过（`_is_item_clickable`），不点错地方；
* `Messages.dump_senders_via_avatar` —— 从**聊天记录面板**从头翻到尾
  （聊天窗口只看得到最近的，不全）；`pull_senders_via_avatar` 读聊天
  窗口，保留给小批量试跑；
* 两遍制（防漏读）：第一遍纯遍历收内容 + 头像指纹，一个都不点；
  第二遍不同头像（`avatar_group_key` 去重）各点一次，同组一起回填。
  点不开也不丢条，只是认不出谁；
* 只比相邻两条，不做全局去重：聊天列表是虚拟化的
  （RecyclerListView），条目 id 会被复用，全局去重会把不同消息当
  同一条跳过 —— 第一遍漏读就是这么来的；
  第二遍点之前先验指纹，复用 id 串到别人头上时不点（认错比认不出严重）；
* `HistoryReader(sender_source="avatar")` —— 群聊改走面板这条，
  私聊不受影响；开关经 `MerchantApplication(sender_source=...)` 透传。
  默认回到了 `"members"`（上游原设计）：头像链为保发送人把读取搞丢条了，
  全量优先 —— 有数但匿名，好过有点名但缺数；
* 新增 `"localdb"` 来源（`src/pyweixin/localdb.py`）：备注名解群 wxid，
  从内存捡记录，发送人解备注名，解不出回落老链路。内存只有最近碰过的页，
  目标群得多翻翻。

代价写在前面：每条约 3~5 秒、抢鼠标焦点。面板自带时间戳，
所以记录有真实发送时间。
先跑 `examples/avatar_click_probe.py 群名 history`（只点一条）验证
坐标、弹窗与复制格式，再用 `max_clicks=3` 小批量试，最后才放开。