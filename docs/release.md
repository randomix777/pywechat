# 发版流程：打 tag → 出包 → 传 Release → 客户端静默更新

客户端的自动更新只认 GitHub Releases（见 `src/wechat_ai_assistant/updater.py`），
约定的每一条都必须做到，否则客户端会静默跳过（不报错、不安装）。

## 1. 改版本号（必须一致）

* `src/wechat_ai_assistant/version.py` 的 `__version__`
* `src/pyproject.toml` 的 `version`（以及 `src/setup.py`，如果还在用）

## 2. 打包（免安装便携版，onedir）

```powershell
.\.venv\Scripts\pyinstaller.exe deploy\merchant.spec
Copy-Item deploy\portable.flag dist\微信AI智能助理\portable.flag -Force
Copy-Item deploy\portable-readme.txt dist\微信AI智能助理\先看我-免安装版.txt -Force
Copy-Item deploy\启动微信AI智能助理.bat dist\微信AI智能助理\启动微信AI智能助理.bat -Force
```

产物：`dist/微信AI智能助理/`（启动脚本、中文说明、`微信AI智能助理.exe`、`_internal/` 和便携标记）。
**不要发 onefile**：pywinauto/pywin32 的原生 DLL 在 onefile 下每次启动
都要解包，又慢又容易被杀软误报（见 `deploy/merchant.spec` 头注）。

用户右键 ZIP 选择“全部解压”，再双击“启动微信AI智能助理.bat”，不需要安装程序。
便携版的数据保存在同级 `data/`，复制整个文件夹即可备份或迁移。自动更新会保留该目录。
请解压到普通可写目录；Windows 加密的登录凭证换电脑后需要重新登录。

发版前冒烟：双击 `dist/微信AI智能助理/微信AI智能助理.exe`，
走一遍"登录 → 选群 → 读历史"，标题栏版本号对得上才算数。

## 3. 生成便携 ZIP（文件名是约定，错一个字客户端就认不出）

```powershell
$ver = "1.9.9"  # 与 version.py 一致，去掉开头的 v
Compress-Archive -Path "dist\微信AI智能助理" `
  -DestinationPath "微信AI智能助理-win64-$ver.zip" -Force
(Get-FileHash "微信AI智能助理-win64-$ver.zip" -Algorithm SHA256).Hash.ToLower() `
  | Out-File "微信AI智能助理-win64-$ver.zip.sha256" -NoNewline -Encoding ascii
```

* zip 里**只有** `微信AI智能助理/` 这一层目录（多套一层也行，客户端只扒一层）。
* `.sha256` 文件内容是单个文件的十六进制哈希（可带文件名后缀，取第一段）。
* **没有 sha256 文件的包客户端拒绝更新** —— 省这一步等于把商户电脑
  交给任意下载。

## 4. 发 Release

1. `git tag v<版本>` 并推送；
2. 在 GitHub 建 Release（tag 同名），上传 `.zip` + `.zip.sha256` 两个文件；
3. Release 正文写人话（修了什么、要不要立即更新），客户端暂不展示，
   先记着以后有用。

仓库默认查 `randomix777/wechat-ai-assistant` 的 releases，换地方发版要同步改
`src/wechat_ai_assistant/updater.py` 的 `DEFAULT_REPO`。

## 5. 生效与回滚

* 客户端每次启动时检查最新 Release；有新版就下载、校验、替换并自动重启进入新版。
* 无新版、网络不可用或下载/校验失败时继续打开当前版本。
* 更新只在启动阶段发生，不会中断已经打开并正在使用的客户端。
* 新版 15 秒起不来自动回滚旧版（旧目录保留到新版确认存活）。
* 出大问题：删掉 Release（或删掉安装包 asset），客户端查不到就地不动；
  已暂存未应用的可让商户删 `_updates/` 与 `pending.json`。
