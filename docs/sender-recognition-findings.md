# 发送人识别：真机排查结论

这份文档记录**在真机上逐条验证过的结果**，避免以后重走。
每一行都是实测的，不是推理。

测试环境：微信 4.x（`xwechat` 新架构），群 `6⃣️5⃣️TJ锦绣中华水果群4⃣️`（424 人）。

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