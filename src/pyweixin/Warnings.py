'''
微信UI自动化过程中可能引发的Warning
'''
class LongTextWarning(Warning):
    '''微信消息字数超长警告'''
    #微信字数限制2000字，超长部分不发送，这时需要转化为txt发送
    pass
class NoChatHistoryWarning(Warning):
    '''聊天记录不足指定的个数'''
    pass
class ChatHistoryUnavailable(RuntimeError):
    '''聊天记录列表**没能就位** —— 读取失败，不是「没有聊天记录」。

    这两件事必须能被分辨：前者要让商户看到「读取失败，重试」并留下日志，
    后者才是真的这个会话没有消息。混为一谈会让商户以为群里没生意。

    真实成因是微信的历史面板异步渲染：窗口出现了，消息列表还在加载。
    '''
    pass
class LanguageDetectionFailedWarning(Warning):
    '''微信当前使用的语言探测失败'''
    pass