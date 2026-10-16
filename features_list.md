# feature_list

## ADD

> - src\wechat_ai_assistant\WeChatAuto.py\class Contacts\get_tags\获取通讯录内所有标签

## Changed

> - src\wechat_ai_assistant\WeChatTools.py\class Tools\is_my_bubble\修改发送人判断逻辑改为左侧头像像素判断，并增加对于超长消息的判断逻辑，准确率更高
> - src\wechat_ai_assistant\WeChatTools.py\class Tools\select_chatList\适配不同主界面内不同长度、类型消息选中后开启多选遍历
> - src\wechat_ai_assistant\utils.py\traverse_messages\对系统消息(时间,拉人进群等统一为SystemInfo内)筛选
> - src\wechat_ai_assistant\WeChatAuto.py\Messages\pull_messages\上述三个改动均为保证方法的稳定

## ToDo

- 维护内部方法保证稳定性...
