# WeChat AI Assistant 快速开始

欢迎使用 **WeChat AI Assistant**，面向 Windows 微信桌面版的 AI 商务助手。

!['微信安装包'](/pics/wechat.png "微信")

## 开始前必看

[微信4.1+自动化说明](/Weixin4.0.md)

当前应用使用 `wechat_ai_assistant` Python 命名空间；原 `pyweixin` 导入名保留为兼容入口。`pywechat` 仍用于微信 3.9.x 自动化。

## 环境要求

| 依赖项 | 版本要求 |
| :--- | :--- |
| **操作系统** | Windows 10 / 11 (**64位**) |
| **Python** | Python >= 3.10（支持TypeHint） |
| **微信** | 3.9.x 或 4.x (根据使用的包决定) |

## 项目结构

```block
src/
│   ├── pywechat/
│   │   ├── WeChatAuto.py
│   │   └── ...
│   ├── wechat_ai_assistant/
│   └── pyweixin/  # 旧导入名兼容层
│       ├── WeChatAuto.py
│       └── ...
├── setup.py
├── pyproject.toml
├── requirements.txt
└── README.md
```

## 安装与使用(Python Package)

旧版 `pywechat127` 发行包已发布于 PyPI，版本为 1.9.8。新产品包名已改为 `wechat-ai-assistant`；该名称的 PyPI 首次发布尚未进行。

### 全局使用

```bash
# 首次发布后可用：pip install wechat-ai-assistant
```

### 本地开发模式安装

```bash
cd src
pip install -e .
```

### 如果需要快速本地开发与测试，还可以在当前目录下相对导入使用

```block
workspace/
│   ├── pywechat/
│   │   ├── WeChatAuto.py
│   │   └── ...
│   ├── wechat_ai_assistant/
│   └── pyweixin/  # 只保留兼容入口，转发到 wechat_ai_assistant
│   │
│   └── test.py
```

不过需要先安装依赖(如果没有的话):

```bash
pip install -r requirements.txt
```

## 安装与使用(Skill)

**`OpenClaw`**

最新版本1.0.4

```bash
openclaw skills install pyweixin-rpa
```

### 其他平台(cmd命令)

下边几个平台亦可使用github链接导入(后续会发布一个新repo)

**`codex`**

```bash
cd %USERPROFILE%
cd .codex\skills
robocopy "D:\autowechat\Skill\OtherPlatforms\pyweixin-rpa" pyweixin-rpa /E
```

**`cursor`**  

```bash
cd %USERPROFILE%
cd .cursor\skills
robocopy "D:\autowechat\Skill\OtherPlatforms\pyweixin-rpa" pyweixin-rpa /E
```

注意将"D:\autowechat\Skill\OtherPlatforms\pyweixin-rpa"替换为实际路径，如果上述命令失败亦可手动复制到对应目录然后重启codex,cursor

**`claude`**

Customize内点击Create Skill，选择Upload a Skill

!['Claude导入'](/pics/claude导入自定义skill.png "Claude")

选择pyweixin-rpa.skill文件上传

<p align="center">
  <img src="/pics/claude选择skill.png" alt="claude选择skill">
</p>

上传后便可以使用了

!['Claude接入skill'](/pics/claude接入pyweixin-rpa.png "Claude")

### Have fun in WechatAutomation (＾＿－)
