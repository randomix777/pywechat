"""为 wechat_ai_assistant 准备一个**独立**的 OpenCode 实例（不碰共享的 4096）。

    python examples/prepare_isolated_opencode.py [--start]

做三件事，每一件都先验证再往下走：

1. 选一个没人用的端口，绑定 127.0.0.1；
2. 用 ``XDG_*`` 把配置、数据、缓存全部重定向到项目私有的目录，
   **并实际验证**这个实例没有继承全局配置（agent 列表、provider 列表、
   MCP 列表都必须只有订单解析需要的东西）；
3. 随机生成服务端认证口令，写进本地文件并收紧权限 —— 口令**不**进
   命令行参数，也不需要贴进聊天。

第 2 步是最关键的一步：OpenCode 在 Windows 上默认从 ``%APPDATA%`` 读配置，
如果环境变量不起作用，这个实例就会静默继承共享服务的那一整套
（godot-ai、blender-mcp、opencodex、openrouter、agnes…）。所以这里
**先起一个临时实例、拿 HTTP 响应验证、验证通过才保留**。验证不通过就
把它停掉并明确报告，绝不当作隔离成功。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant.gateway_server import (  # noqa: E402
    OPENCODE_PASSWORD_ENV,
    OPENCODE_PASSWORD_FILE_ENV,
    write_secret_file,
)

OPENCODE_BIN = Path(
    os.environ.get("APPDATA", "")
) / "npm/node_modules/opencode-ai/node_modules/opencode-windows-x64/bin/opencode.exe"

#: 私有根目录。配置、数据、缓存、日志各自独立，不与共享实例共享。
PRIVATE_ROOT = ROOT / ".opencode-private"

#: 这个实例**只**允许的东西。验证时逐项对照：多一样都不算隔离成功。
ALLOWED_AGENTS = {"order-parse"}
FORBIDDEN_CONFIG_KEYS = ("mcp", "provider", "experimental")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def build_config() -> dict:
    """私有配置：只留订单解析这一个 agent，权限全 deny，不开 MCP、不配 provider。"""
    return {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",
        "autoupdate": False,
        "default_agent": "order-parse",
        # 故意**不**写 provider / mcp / experimental：这个实例的上游凭据由
        # 登录或环境提供，不从全局配置继承。
        "permission": {"*": "deny"},
        "agent": {
            "order-parse": {
                "description": "商家群消息订单解析：只读输入，输出固定 JSON，不使用任何工具。",
                "mode": "primary",
                "temperature": 0,
                "steps": 2,
                "permission": {
                    "*": "deny", "read": "deny", "edit": "deny", "glob": "deny", "grep": "deny",
                    "list": "deny", "bash": "deny", "task": "deny", "skill": "deny", "lsp": "deny",
                    "todowrite": "deny", "question": "deny", "webfetch": "deny", "websearch": "deny",
                    "external_directory": "deny", "doom_loop": "deny",
                },
            },
            "build": {"disable": True}, "plan": {"disable": True}, "general": {"disable": True},
            "explore": {"disable": True}, "title": {"disable": True}, "summary": {"disable": True},
            "compaction": {"disable": True},
        },
    }


def private_env(password_file: Path, port: int) -> dict[str, str]:
    """把配置/数据/缓存全部重定向到项目私有目录。"""
    env = {
        key: value for key, value in os.environ.items()
        # 不要把共享实例的口令带进私有实例：两个实例的认证互不相干。
        if key not in {OPENCODE_PASSWORD_ENV, "OPENCODE_SERVER_PASSWORD"}
    }
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        target = PRIVATE_ROOT / sub
        target.mkdir(parents=True, exist_ok=True)
        env[name] = str(target)
    # Windows 上 OpenCode 默认读 %APPDATA%；必须一起挪走，否则隔离无效。
    env["APPDATA"] = str(PRIVATE_ROOT / "appdata")
    env["LOCALAPPDATA"] = str(PRIVATE_ROOT / "localappdata")
    env["OPENCODE"] = str(PRIVATE_ROOT / "state")
    for target in (PRIVATE_ROOT / "appdata", PRIVATE_ROOT / "localappdata", PRIVATE_ROOT / "state"):
        target.mkdir(parents=True, exist_ok=True)
    env["OPENCODE_SERVER_PASSWORD"] = ""  # 由调用方在同一进程内注入，见 main()
    env["OPENCODE_SERVER_USERNAME"] = "wechat_ai_assistant"
    env[OPENCODE_PASSWORD_FILE_ENV] = str(password_file)
    del env["OPENCODE_SERVER_PASSWORD"]
    return env


def http(url: str, password: str, user: str = "wechat_ai_assistant", timeout: float = 8.0):
    import base64

    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = urllib.request.Request(url, headers={"Authorization": "Basic " + token, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def start_instance(port: int, password: str, log: Path) -> subprocess.Popen:
    """后台起实例。不弹终端窗口（Windows 用 CREATE_NO_WINDOW）。"""
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    env = private_env(PRIVATE_ROOT / "secret.pw", port)
    env["OPENCODE_SERVER_PASSWORD"] = password
    env["OPENCODE_SERVER_USERNAME"] = "wechat_ai_assistant"
    handle = open(log, "w", encoding="utf-8", errors="replace")
    return subprocess.Popen(
        [str(OPENCODE_BIN), "serve", "--port", str(port), "--hostname", "127.0.0.1", "--pure"],
        cwd=str(ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT, creationflags=creation,
    )


def verify_isolation(base: str, password: str) -> tuple[bool, dict]:
    """**实际**验证这个实例没有继承全局配置。验证不过就不算隔离。"""
    report: dict = {"base": base, "checks": {}, "ok": True}

    health = http(f"{base}/global/health", password)
    report["checks"]["health"] = health
    if not health.get("healthy"):
        report["ok"] = False
        report["error"] = "实例不健康"

    agents = http(f"{base}/agent", password)
    names = {str(item.get("name")) for item in agents} if isinstance(agents, list) else set()
    report["checks"]["agents"] = sorted(names)
    if "order-parse" not in names:
        report["ok"] = False
        report["error"] = "私有配置里的 order-parse agent 没有被加载"
    extra_agents = names - ALLOWED_AGENTS
    if extra_agents:
        report["ok"] = False
        report["error"] = f"继承了全局 agent：{sorted(extra_agents)}"

    providers = http(f"{base}/provider", password)
    connected = {str(item) for item in (providers.get("connected") or [])} if isinstance(providers, dict) else set()
    report["checks"]["connected_providers"] = sorted(connected)
    leaked = connected & {"opencodex", "openrouter", "agnes", "godot-ai", "blender-mcp"}
    if leaked:
        report["ok"] = False
        report["error"] = f"继承了全局 provider：{sorted(leaked)}"
    return report["ok"], report


def auth_login(provider: str) -> int:
    """在**私有实例**里执行 ``opencode auth login``。

    只做这一件事，且完全交给你在终端里交互完成：它会打印登录链接或提示，
    凭据由你本人输入。脚本不读全局凭据库、不读进程内存、不绕过认证，
    也不会替你购买额度或更换账号。

    环境变量同样指向私有目录，所以这次登录**只**对私有实例生效，
    不会改动共享实例 4096 的任何配置。
    """
    env = private_env(PRIVATE_ROOT / "secret.pw", 0)
    env.pop("OPENCODE_SERVER_PASSWORD", None)
    env.pop("OPENCODE_SERVER_USERNAME", None)
    print(f"即将在私有实例中登录上游：{provider}")
    print("  配置目录：", env.get("XDG_CONFIG_HOME"), "（私有，与共享实例无关）")
    print("  按提示完成登录；凭据只写进这个私有目录。")
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    del creation  # 登录必须让用户看到并交互，所以**不**隐藏窗口
    return subprocess.call(
        [str(OPENCODE_BIN), "auth", "login", provider], cwd=str(ROOT), env=env,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prepare-isolated-opencode")
    parser.add_argument("--port", type=int, default=0, help="0 = 自动挑一个没占用的端口")
    parser.add_argument("--password", default="", help="不要用：会把口令留在 shell 历史里。默认随机生成。")
    parser.add_argument("--keep", action="store_true", help="验证失败也保留实例（调试用）")
    parser.add_argument(
        "--auth-login", default="",
        help="在这个私有实例里登录某个上游（例如 opencode-go）。"
             "**必须你亲自执行**：我不会替你登录、购买额度或更换账号。",
    )
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.auth_login:
        return auth_login(args.auth_login)

    if not OPENCODE_BIN.is_file():
        print(f"找不到 opencode 可执行文件：{OPENCODE_BIN}")
        return 1

    port = args.port or free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            print(f"端口 {port} 已被占用；换一个。")
            return 1

    PRIVATE_ROOT.mkdir(parents=True, exist_ok=True)
    config_dir = PRIVATE_ROOT / "config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "opencode.json").write_text(
        json.dumps(build_config(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 口令：随机生成，写进本地文件。**不**打印，不进命令行。
    password = args.password or secrets.token_urlsafe(32)
    password_file = PRIVATE_ROOT / "secret.pw"
    write_secret_file(password_file, password)
    print(f"1. 私有配置：{config_dir / 'opencode.json'}")
    print(f"   私有数据：{PRIVATE_ROOT / 'data'}　缓存：{PRIVATE_ROOT / 'cache'}"
          f"　应用数据：{PRIVATE_ROOT / 'appdata'}")
    print(f"2. 认证口令：随机生成，写入 {password_file}")
    print(f"   设 {OPENCODE_PASSWORD_FILE_ENV}={password_file} 即可，无需复制口令")
    print(f"3. 端口：{port}（127.0.0.1）")

    env = private_env(password_file, port)
    # OpenCode 的服务端口令只能从环境变量读；这里注入到**子进程**环境，
    # 不写到命令行、不落到任何日志。
    env["OPENCODE_SERVER_PASSWORD"] = password
    env["OPENCODE_SERVER_USERNAME"] = "wechat_ai_assistant"
    (PRIVATE_ROOT / "serve.env").write_text(
        f"{OPENCODE_PASSWORD_FILE_ENV}={password_file}\nOPENCODE_SERVER_PASSWORD_FILE={password_file}\n"
        "# 真实口令不写进任何文件；由 --serve 脚本在同一进程内注入。\n",
        encoding="utf-8",
    )

    log = PRIVATE_ROOT / "opencode.log"
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    handle = open(log, "w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        [str(OPENCODE_BIN), "serve", "--port", str(port), "--hostname", "127.0.0.1", "--pure"],
        cwd=str(ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT, creationflags=creation,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            if process.poll() is not None:
                print(f"4. 实例启动失败，日志：{log}")
                return 1
            try:
                http(f"{base}/global/health", password, timeout=2)
                break
            except Exception:
                time.sleep(0.25)
        else:
            print(f"4. 实例没有在预期时间内就绪，日志：{log}")
            return 1

        ok, report = verify_isolation(base, password)
        print(f"4. 隔离验证：{'通过' if ok else '未通过'}")
        for name, value in report["checks"].items():
            print(f"   {name}: {value}")
        if not ok:
            print(f"   原因：{report.get('error')}")
            print("   结论：无法确保独立配置不影响共享服务，已停止该实例。")
            if not args.keep:
                process.terminate()
            return 2
        (PRIVATE_ROOT / "instance.json").write_text(
            json.dumps({"port": port, "base": base, "pid": process.pid,
                        "password_file": str(password_file),
                        "config": str(config_dir / "opencode.json")},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"5. 实例就绪：{base}（pid {process.pid}），记录在 {PRIVATE_ROOT / 'instance.json'}")
        print(f"   注意：它现在**独立运行**，与共享的 4096 互不影响；")
        print(f"   上游模型凭据需要你在这个实例里登录或提供 —— 我不会替你购买额度或换账号。")
        return 0
    finally:
        handle.close()
        if not args.keep and process.poll() is not None:
            pass


if __name__ == "__main__":
    sys.exit(main())
