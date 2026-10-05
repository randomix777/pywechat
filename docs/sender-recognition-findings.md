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

## 还值得试的（都没验）

1. **换 pywinauto 后端**：`backend="win32"` 走经典 Win32 API，不走 UIA。
   COM 错误是 UIA 层的，换后端可能既不崩又能读到别的信息。**最便宜，先试这个。**
2. **升级/降级 pywinauto**：`-2147220991` 像版本不兼容。
3. **换更旧的微信 3.x**：那一代界面把「发送人 内容」渲染进可访问文本，
   现有代码就是照它写的。
4. **让微信把消息存到本地**：设置里打开「保留聊天记录」，然后找 `db_storage`。

## 复现命令

    .\.venv\Scripts\python.exe -X utf8 examples\raw_text_probe.py "群名" 5