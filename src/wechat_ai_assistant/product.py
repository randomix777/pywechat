"""Product account, device authorization, subscription and job persistence.

This module owns the gateway's own SQLite file. It deliberately does not share
connections with :mod:`wechat_ai_assistant.orders`: the merchant's authoritative order data
lives on the merchant's Windows PC, and the gateway only ever stores parse
*suggestions* plus billing state. Keeping two files on the same data volume also
avoids cross-connection write contention between the job worker and the account
endpoints.

The browser device flow implemented here is a product-owned device authorization
grant. Its shape follows RFC 8628, but it is not OAuth and must not be described
as an OAuth/OIDC certification.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

LOGGER = logging.getLogger("wechat_ai_assistant.product")

PRODUCT_SCHEMA_VERSION = 1

ACCESS_TOKEN_TTL = 900
REFRESH_TOKEN_TTL = 30 * 24 * 3600
WEB_SESSION_TTL = 12 * 3600
DEVICE_CODE_TTL = 600
DEVICE_POLL_INTERVAL = 5.0
#: Attempts allowed on a short user_code before the confirming browser is
#: throttled. The code is only 8 characters, so guessing must be rate limited
#: even though the space is large.
USER_CODE_ATTEMPT_LIMIT = 15
USER_CODE_ATTEMPT_WINDOW = 300.0

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_OUTCOME_UNKNOWN = "outcome_unknown"
JOB_AWAITING_RECONCILE = "awaiting_reconcile"
JOB_STATES = (
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    JOB_FAILED,
    JOB_CANCELLED,
    JOB_OUTCOME_UNKNOWN,
    JOB_AWAITING_RECONCILE,
)
#: States that keep a quota reservation held because the outcome is not settled.
#: 已终止。第一次写入生效（见 :meth:`finish_job`），
#: 后来的写入一律忽略 —— 否则会双计额度。
TERMINAL_JOB_STATES = (JOB_SUCCEEDED, JOB_FAILED, JOB_CANCELLED)
UNSETTLED_JOB_STATES = (JOB_QUEUED, JOB_RUNNING, JOB_OUTCOME_UNKNOWN, JOB_AWAITING_RECONCILE)

# Ambiguous characters are excluded so a merchant can read the code off screen.
_USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789"

#: OWASP-recommended scrypt parameters (N=2^14, r=8, p=1 => ~16 MiB per hash).
#: n=2^15 would exceed CPython's default 32 MiB OpenSSL maxmem on some builds.
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


class ProductError(RuntimeError):
    """Base class for product gateway errors carrying a merchant-safe code."""

    code = "product_error"
    http_status = 400

    def __init__(self, message: str = "", code: str = "", http_status: int = 0):
        super().__init__(message or self.code)
        self.code = code or self.code
        self.http_status = http_status or self.http_status


class ValidationFailed(ProductError):
    code = "invalid_request"
    http_status = 400


class Unauthenticated(ProductError):
    code = "unauthenticated"
    http_status = 401


class Forbidden(ProductError):
    code = "forbidden"
    http_status = 403


class NotFound(ProductError):
    code = "not_found"
    http_status = 404


class Conflict(ProductError):
    code = "conflict"
    http_status = 409


class SubscriptionInactive(ProductError):
    code = "subscription_inactive"
    http_status = 402


class QuotaExhausted(ProductError):
    code = "quota_exhausted"
    http_status = 402


class IdempotencyConflict(Conflict):
    code = "idempotency_key_reuse"


class ModelUnavailable(Conflict):
    """所选模型当前不能使用。

    与 :class:`QuotaExhausted` 分开：额度用完可以等或换模型，模型不可用
    往往是上游故障或尚未通过验证。两者都**不**允许自动换成收费模型。
    """

    def __init__(self, message: str = "", code: str = "model_unavailable"):
        super().__init__(message, code=code)


class PlatformBudgetExhausted(Conflict):
    """平台级预算用尽，停止接收**新**的执行任务。

    已经在跑的任务照常跑完；客户本地数据不受影响。
    """

    def __init__(self, message: str = "", code: str = "platform_budget_exhausted", facts: Mapping[str, Any] | None = None):
        super().__init__(message, code=code)
        #: 新的计费事实。客户端据此重新展示"这次要花多少"，商户确认后再提交。
        self.facts = dict(facts or {})


class PricingChanged(Conflict):
    """提交时策略版本与商户确认时看到的不一致。

    不是失败，是**要求重新确认**：价格在商户看过之后变了，不能悄悄按新价扣。
    """

    def __init__(self, message: str = "", code: str = "pricing_changed", facts: Mapping[str, Any] | None = None):
        super().__init__(message, code=code)
        self.facts = dict(facts or {})


class PricingVersionRequired(Conflict):
    """收费任务提交时**没有**带计费策略版本。

    没有版本就没有"商户确认过的那个价"，按当前价格扣属于不知情收费。
    与 :class:`PricingChanged` 分开：一个是压根没确认过，一个是确认的那一版
    已经过期 —— 客户端要做的动作不同（先展示金额 / 重新展示并确认）。
    """

    def __init__(self, message: str = "", code: str = "pricing_version_required", facts: Mapping[str, Any] | None = None):
        super().__init__(message, code=code)
        self.facts = dict(facts or {})


@dataclass
class PlatformBudget:
    """平台自己的总预算与全局并发上限。

    **不伪造人民币成本**：订阅费用无法可靠按请求折算时，这里记的是请求数、
    token 和并发数 —— 真实供应商用量仍然按次记在 ``usage_ledger`` 的
    ``internal_retry`` 与结算里。知道单价时，``estimated_cost_cents`` 才有值。
    """

    #: 每天允许执行的付费任务数。0 = 不限制。
    daily_paid_jobs: int = 0
    #: 同时执行的付费任务数。0 = 不限制。
    max_paid_concurrency: int = 0
    #: 已知价格时用于估算；未知就是 None，**不编造**。
    cost_cents_per_paid_job: int | None = None
    #: 每日成本上限（分）。None = 不限。
    daily_cost_cents: int | None = None
    #: 免费任务不受平台付费预算限制。
    free_jobs_exempt: bool = True

    def can_accept(self, charged_micro_usd: int) -> bool:
        """只做静态判断；实际用量由 :meth:`PlatformBudgetLedger` 判定。"""
        return True

    def exhausted_reason(self) -> str:
        return "平台处理能力暂时用尽，已暂停接收新的分析任务。您本地的订单和消息不受影响，请稍后重试。"


class PlatformBudgetLedger:
    """平台预算的实际记账。

    只用**现有的** ``jobs`` 和 ``usage_ledger``，不新增数据库、不引消息队列。
    免费任务不占平台付费预算 —— 否则免费额度一开就会把付费通道堵死。
    """

    def __init__(self, store: "ProductStore", budget: PlatformBudget, clock: Callable[[], float] | None = None):
        self.store = store
        self.budget = budget
        self._clock = clock or (lambda: __import__("time").time())

    def _today(self) -> str:
        from datetime import datetime as _dt

        return _dt.fromtimestamp(self._clock(), tz=timezone.utc).strftime("%Y-%m-%d")

    def paid_jobs_today(self) -> int:
        row = self.store._connection.execute(
            "SELECT COUNT(*) AS count FROM usage_ledger"
            " WHERE kind='settle' AND day=? AND charged_micro_usd>0",
            (self._today(),),
        ).fetchone()
        return int(row["count"] or 0)

    def settled_cost_cents_today(self) -> int | None:
        """今天已结算的估算成本。没有可靠单价时返回 None，不编造。

        按**任务**算，不是按额度算：``cost_cents_per_paid_job`` 的单位是"每个
        付费任务"，乘上额度数会在一个任务扣多份额度时高估。
        """
        unit = self.budget.cost_cents_per_paid_job
        if unit is None:
            return None
        row = self.store._connection.execute(
            "SELECT COUNT(*) AS count FROM usage_ledger"
            " WHERE kind='settle' AND day=? AND charged_micro_usd>0",
            (self._today(),),
        ).fetchone()
        return int(row["count"] or 0) * int(unit)

    def running_paid_jobs(self) -> int:
        """当前占着执行位的付费任务数。

        未结算的付费任务把金额记在 ``jobs.reserved_micro_usd`` 上（结算之后才写
        ``charged_micro_usd``），所以两个字段都要看。只看 ``charged_micro_usd`` 的话，
        排队和执行中的任务一个都数不到 —— 全局并发上限就成了摆设。
        """
        row = self.store._connection.execute(
            "SELECT COUNT(*) AS count FROM jobs"
            " WHERE state IN ('queued','running') AND (reserved_micro_usd>0 OR charged_micro_usd>0)"
        ).fetchone()
        return int(row["count"] or 0)

    def can_accept(self, charged_micro_usd: int) -> bool:
        if self.budget.free_jobs_exempt and int(charged_micro_usd) <= 0:
            return True
        if self.budget.daily_paid_jobs and self.paid_jobs_today() >= int(self.budget.daily_paid_jobs):
            return False
        if self.budget.max_paid_concurrency and self.running_paid_jobs() >= int(self.budget.max_paid_concurrency):
            return False
        if self.budget.daily_cost_cents is not None and self.budget.cost_cents_per_paid_job is not None:
            spent = self.settled_cost_cents_today() or 0
            if spent >= int(self.budget.daily_cost_cents):
                return False
        return True

    def exhausted_reason(self) -> str:
        return self.budget.exhausted_reason()

    def status(self) -> dict[str, Any]:
        return {
            "daily_paid_jobs": self.budget.daily_paid_jobs,
            "paid_jobs_today": self.paid_jobs_today(),
            "max_paid_concurrency": self.budget.max_paid_concurrency,
            "running_paid_jobs": self.running_paid_jobs(),
            "daily_cost_cents": self.budget.daily_cost_cents,
            "cost_cents_per_paid_job": self.budget.cost_cents_per_paid_job,
            "settled_cost_cents_today": self.settled_cost_cents_today(),
            "cost_known": self.budget.cost_cents_per_paid_job is not None,
            "accepting": self.can_accept(1),
        }


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def utc_now() -> float:
    return time.time()


#: 解析失败过的时区名，只警告一次 —— 每条任务都喊一遍没人会看。
_WARNED_TIMEZONES: set[str] = set()


def resolve_timezone(name: str) -> Any:
    """把 IANA 时区名变成 tzinfo。解析不了就返回 ``None``（**并警告**）。

    Windows 没有系统时区库：没装 ``tzdata`` 时 ``zoneinfo`` 对**任何**名字
    都抛异常。此前这个异常被静默吞掉、悄悄按 UTC 记账 —— 运营配了
    ``Asia/Shanghai``，商户的"每天免费次数"就在当地早上 8 点重置，而界面上
    一点提示都没有。静默的计费错误比报错更贵。
    """
    key = str(name or "UTC").strip() or "UTC"
    if key.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(key)
    except Exception as exc:  # 没有 tzdata、名字写错、平台不支持
        if key not in _WARNED_TIMEZONES:
            _WARNED_TIMEZONES.add(key)
            LOGGER.warning(
                "timezone %r cannot be resolved (%s: %s); free quota will be counted in UTC. "
                "Install the 'tzdata' package on Windows.",
                key, type(exc).__name__, exc,
            )
        return None


def day_key(now: float | None = None, timezone_name: str = "UTC") -> str:
    """免费额度记在哪一天 —— 全系统唯一的一份实现。

    时区显式可配，默认 UTC。调用方（目录层与存储层）必须用这一个，否则两处
    口径不同，一个任务会被记到两个日子里。
    """
    stamp = datetime.fromtimestamp(now if now is not None else utc_now(), tz=timezone.utc)
    zone = resolve_timezone(timezone_name)
    if zone is not None:
        return stamp.astimezone(zone).strftime("%Y-%m-%d")
    return stamp.strftime("%Y-%m-%d")


def iso(value: float | None = None) -> str:
    return datetime.fromtimestamp(value if value is not None else utc_now(), timezone.utc).isoformat()


def token_digest(token: str) -> str:
    """Server-side storage is a digest only; plaintext tokens are never persisted."""
    if not token:
        raise ValueError("token is required")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PasswordHasher:
    """scrypt password hashing (RFC 7914) using the standard library only."""

    n = SCRYPT_N
    r = SCRYPT_R
    p = SCRYPT_P
    dklen = SCRYPT_DKLEN

    def hash(self, password: str) -> str:
        if not isinstance(password, str) or len(password) < 8:
            raise ValidationFailed("password must be at least 8 characters", code="weak_password")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=self.n, r=self.r, p=self.p, dklen=self.dklen)
        return f"scrypt${self.n}${self.r}${self.p}${_b64(salt)}${_b64(digest)}"

    def verify(self, password: str, encoded: str) -> bool:
        try:
            algorithm, n, r, p, salt_text, digest_text = str(encoded).split("$")
            if algorithm != "scrypt":
                return False
            expected = _unb64(digest_text)
            actual = hashlib.scrypt(
                password.encode("utf-8"), salt=_unb64(salt_text), n=int(n), r=int(r), p=int(p), dklen=len(expected)
            )
        except (ValueError, TypeError, MemoryError):
            return False
        return hmac.compare_digest(expected, actual)


def normalize_user_code(raw: str) -> str:
    cleaned = re.sub(r"[^0-9A-Z]", "", str(raw or "").upper())
    if len(cleaned) != 8 or any(character not in _USER_CODE_ALPHABET for character in cleaned):
        raise ValidationFailed("验证码格式不正确", code="bad_user_code")
    return f"{cleaned[:4]}-{cleaned[4:]}"


#: A throwaway hash of a random value, used to equalise timing on unknown users.
DUMMY_PASSWORD_HASH = "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    access_expires_at: float
    refresh_expires_at: float
    token_type: str = "Bearer"


def _guarded(fn):
    """让方法体在 ProductStore 的锁下执行。

    为什么要这么做：

    写路径都经过 :meth:`_transaction`，它会拿锁。但**读**路径原本往往不拿锁，
    而连接是**共享的**。另一个线程正在事务里（已经 BEGIN IMMEDIATE）
    的时候，本线程去 execute() 一下，sqlite3 模块内部状态就会被打乱 ——

        sqlite3.InterfaceError: bad parameter or other API misuse

    这不是并发测试里的工作：它是真实的多线程下的数据库访问错误。

    ``_lock`` 是 RLock，重入可以：已经在事务里的方法被再来一层的装饰器
    包裹，不会自锁。
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper

class ProductStore:
    """Gateway-side persistence for accounts, devices, subscriptions and jobs."""

    def __init__(self, path: str | Path = "product.db", clock=utc_now):
        self.path = str(path)
        self._clock = clock
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Transactions nest (a job finish settles quota, a claim releases a
        # reservation), so depth is tracked and only the outermost one commits.
        self._txn_depth = 0
        self._txn_immediate = False
        self._connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=10000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self.passwords = PasswordHasher()
        self._create_schema()

    # ------------------------------------------------------------------ schema

    @_guarded
    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS product_schema_version(version INTEGER NOT NULL);

                CREATE TABLE IF NOT EXISTS accounts(
                    id TEXT PRIMARY KEY,
                    merchant_id TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('merchant','operator')),
                    status TEXT NOT NULL CHECK(status IN ('active','disabled')),
                    must_change_password INTEGER NOT NULL DEFAULT 1,
                    failed_logins INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS web_sessions(
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS web_sessions_account ON web_sessions(account_id, expires_at);

                CREATE TABLE IF NOT EXISTS product_devices(
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    merchant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
                    platform TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS product_devices_account ON product_devices(account_id, status);

                CREATE TABLE IF NOT EXISTS device_authorizations(
                    id TEXT PRIMARY KEY,
                    device_code_hash TEXT NOT NULL UNIQUE,
                    user_code TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN ('pending','approved','denied','claimed','expired')),
                    account_id TEXT REFERENCES accounts(id),
                    device_id TEXT REFERENCES product_devices(id),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    approved_at REAL,
                    claimed_at REAL,
                    poll_count INTEGER NOT NULL DEFAULT 0,
                    slow_down_until REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS device_authorizations_user_code ON device_authorizations(user_code, status);
                CREATE INDEX IF NOT EXISTS device_authorizations_expiry ON device_authorizations(status, expires_at);

                CREATE TABLE IF NOT EXISTS access_tokens(
                    token_hash TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL REFERENCES product_devices(id) ON DELETE CASCADE,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS access_tokens_device ON access_tokens(device_id, expires_at);

                CREATE TABLE IF NOT EXISTS refresh_tokens(
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL REFERENCES product_devices(id) ON DELETE CASCADE,
                    family_id TEXT NOT NULL,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    rotated_at REAL,
                    revoked_at REAL,
                    revoked_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS refresh_tokens_family ON refresh_tokens(family_id, revoked_at);

                CREATE TABLE IF NOT EXISTS subscriptions(
                    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                    plan_code TEXT NOT NULL,
                    quota_micro_usd INTEGER NOT NULL,
                    used_micro_usd INTEGER NOT NULL DEFAULT 0,
                    reserved_micro_usd INTEGER NOT NULL DEFAULT 0,
                    max_concurrent_jobs INTEGER NOT NULL DEFAULT 2,
                    period_start REAL NOT NULL,
                    period_end REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','suspended','expired')),
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL REFERENCES product_devices(id) ON DELETE CASCADE,
                    merchant_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    request_json TEXT,
                    upstream_session_id TEXT,
                    upstream_message_id TEXT,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    reserved_micro_usd INTEGER NOT NULL DEFAULT 0,
                    charged_micro_usd INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    deadline_at REAL NOT NULL,
                    lease_until REAL,
                    lease_token TEXT,
                    updated_at REAL NOT NULL,
                    -- 请求的模型 vs 上游自报实际执行的模型。两者分开记，
                    -- 便于事后核对这一单到底跑了哪个模型。
                    model_requested TEXT NOT NULL DEFAULT '',
                    model_reported TEXT NOT NULL DEFAULT '',
                    model_confirmed INTEGER NOT NULL DEFAULT 0,
                    model_warning TEXT NOT NULL DEFAULT '',
                    request_evidence TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(account_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS jobs_account_created ON jobs(account_id, created_at);
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, deadline_at);
                CREATE INDEX IF NOT EXISTS jobs_session ON jobs(upstream_session_id);
                -- 模型目录与运营策略。凭据不进这张表，也不进 jobs。
                CREATE TABLE IF NOT EXISTS model_catalog(
                    version TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    last_success_at REAL NOT NULL,
                    fetched_ok INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS model_policy(
                    alias TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                -- 订单能力验证记录：后端、实际模型、提示词版本、检查结果。
                CREATE TABLE IF NOT EXISTS model_verification(
                    alias TEXT PRIMARY KEY,
                    backend TEXT NOT NULL,
                    provider_id TEXT NOT NULL DEFAULT '',
                    model_id TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    passed INTEGER NOT NULL DEFAULT 0,
                    checks TEXT NOT NULL DEFAULT '{}',
                    verified_at REAL NOT NULL
                );
                -- 免费额度按天计。免费任务**不**扣订阅额度，但用量照样记录。
                CREATE TABLE IF NOT EXISTS free_usage(
                    account_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    reserved INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(account_id, day)
                );
                -- 任务创建时冻结的计费与路由快照。网络重试必须用同一份。
                CREATE TABLE IF NOT EXISTS job_model_snapshot(
                    job_id TEXT PRIMARY KEY,
                    alias TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    catalog_version TEXT NOT NULL DEFAULT '',
                    pricing_class TEXT NOT NULL DEFAULT 'metered',
                    pricing_version TEXT NOT NULL DEFAULT '',
                    -- 下面三列是**免费档位的遗留**。
                    --
                    -- 免费路径已经焊死（见 ``OperatorModelPolicy.__post_init__``），
                    -- 新写入的行这里一律是 0。但**不能删列**：库里已有历史行，
                    -- 删列要么迁移、要么旧数据读不出来。这里保留只读，
                    -- 等确认线上没有未结算的历史行之后再单独做迁移。
                    daily_free_limit INTEGER NOT NULL DEFAULT 0,
                    prompt_version TEXT NOT NULL DEFAULT '',
                    plugin_version TEXT NOT NULL DEFAULT '',
                    free_settled INTEGER NOT NULL DEFAULT 0,
                    free_released INTEGER NOT NULL DEFAULT 0,
                    snapshot TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS usage_ledger(
                    id TEXT PRIMARY KEY,
                    job_id TEXT,
                    account_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('reserve','settle','release','internal_retry')),
                    micro_usd INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    -- 平台预算按天统计用；不知道单价时只按 micro_usd 计数，不折算金额。
                    day TEXT NOT NULL DEFAULT '',
                    charged_micro_usd INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS usage_ledger_job ON usage_ledger(job_id, created_at);
                CREATE INDEX IF NOT EXISTS usage_ledger_day ON usage_ledger(day, kind);

                -- 充值订单：本地对 JYI 支付订单的**镜像**，不是支付事实来源。
                -- 钱有没有真到账以 JYI 为准；这里只存"我们认为它到了"以及核对用的外部单号。
                CREATE TABLE IF NOT EXISTS recharge_orders(
                    order_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    product_id TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    credits INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    external_id TEXT NOT NULL DEFAULT '',
                    adapter TEXT NOT NULL DEFAULT '',
                    checkout_url TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS recharge_orders_account ON recharge_orders(account_id, created_at);

                -- 充值流水。``reference`` 唯一 —— 同一笔来源交易只可能入账一次。
                -- 这一条是"重复回调不加点"的唯一保证，不靠上游自觉。
                CREATE TABLE IF NOT EXISTS credit_ledger(
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    micro_usd INTEGER NOT NULL,
                    reference TEXT NOT NULL UNIQUE,
                    order_id TEXT NOT NULL DEFAULT '',
                    adapter TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS credit_ledger_account ON credit_ledger(account_id, created_at);

                CREATE TABLE IF NOT EXISTS gateway_secrets(
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS login_attempts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS login_attempts_scope ON login_attempts(scope, at);

                CREATE TABLE IF NOT EXISTS job_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    at REAL NOT NULL,
                    state TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, at);
                """
            )
            self._connection.execute(
                "INSERT INTO product_schema_version SELECT ? WHERE NOT EXISTS(SELECT 1 FROM product_schema_version)",
                (PRODUCT_SCHEMA_VERSION,),
            )
            self._connection.execute(
                "UPDATE product_schema_version SET version=? WHERE version<?",
                (PRODUCT_SCHEMA_VERSION, PRODUCT_SCHEMA_VERSION),
            )

    @property
    def schema_version(self) -> int:
        row = self._connection.execute("SELECT version FROM product_schema_version").fetchone()
        return int(row["version"]) if row else 0

    # ---------------------------------------------------------------- accounts

    @_guarded
    def create_account(
        self,
        username: str,
        password: str,
        merchant_id: str = "",
        display_name: str = "",
        role: str = "merchant",
        plan_code: str = "trial",
        quota_micro_usd: int = 0,
        max_concurrent_jobs: int = 2,
        period_days: int = 30,
        must_change_password: bool = True,
    ) -> dict[str, Any]:
        """Operator-facing provisioning. Returns the plaintext password once."""
        username = str(username or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9._@+-]{3,64}", username):
            raise ValidationFailed("username must be 3-64 characters of a-z 0-9 . _ @ + -", code="bad_username")
        if role not in {"merchant", "operator"}:
            raise ValidationFailed("role must be merchant or operator", code="bad_role")
        now = self._clock()
        account_id = "acc_" + secrets.token_hex(12)
        merchant = merchant_id or "m_" + secrets.token_hex(6)
        with self._transaction():
            if self._connection.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
                raise Conflict("用户名已存在", code="username_taken")
            self._connection.execute(
                "INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?,0,NULL,?,?)",
                (
                    account_id,
                    merchant,
                    username,
                    display_name or username,
                    self.passwords.hash(password),
                    role,
                    "active",
                    int(must_change_password),
                    now,
                    now,
                ),
            )
            self._connection.execute(
                "INSERT INTO subscriptions VALUES(?,?,?,0,0,?,?,?,?,?)",
                (account_id, plan_code, int(quota_micro_usd), int(max_concurrent_jobs), now, now + period_days * 86400, "active", now),
            )
        return {
            "account_id": account_id,
            "merchant_id": merchant,
            "username": username,
            "password": password,
            "display_name": display_name or username,
            "plan_code": plan_code,
        }

    @_guarded
    def get_account(self, account_id: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    @_guarded
    def find_account(self, username: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM accounts WHERE username=?", (str(username or "").strip().lower(),)
        ).fetchone()
        return dict(row) if row else None

    @_guarded
    def authenticate_password(self, username: str, password: str) -> dict[str, Any]:
        """Password check with rate limiting and a uniform failure message."""
        username = str(username or "").strip().lower()
        scope = f"password:{username}"
        self._rate_limit(scope, limit=10, window=300.0, code="login_rate_limited")
        account = self.find_account(username)
        if account is None:
            # Spend comparable time so a missing account is not distinguishable.
            self.passwords.verify(password, DUMMY_PASSWORD_HASH)
            raise Unauthenticated("账号或密码不正确", code="invalid_credentials")
        if account.get("locked_until") and account["locked_until"] > self._clock():
            raise Forbidden("账号暂时锁定，请稍后再试", code="account_locked")
        if account["status"] != "active":
            raise Forbidden("账号已停用", code="account_disabled")
        if not self.passwords.verify(password, account["password_hash"]):
            with self._transaction():
                self._connection.execute("UPDATE accounts SET failed_logins=failed_logins+1 WHERE id=?", (account["id"],))
            raise Unauthenticated("账号或密码不正确", code="invalid_credentials")
        if account["failed_logins"]:
            with self._transaction():
                self._connection.execute("UPDATE accounts SET failed_logins=0 WHERE id=?", (account["id"],))
        return account

    _dummy_hash = ""  # replaced below once PasswordHasher exists

    @_guarded
    def change_password(self, account_id: str, old_password: str, new_password: str) -> None:
        account = self.get_account(account_id)
        if account is None:
            raise NotFound("账号不存在")
        if not self.passwords.verify(old_password, account["password_hash"]):
            raise Unauthenticated("原密码不正确", code="invalid_credentials")
        with self._transaction():
            self._connection.execute(
                "UPDATE accounts SET password_hash=?,must_change_password=0,updated_at=? WHERE id=?",
                (self.passwords.hash(new_password), self._clock(), account_id),
            )
            self._connection.execute(
                "UPDATE refresh_tokens SET revoked_at=?,revoked_reason='password_changed' WHERE account_id=? AND revoked_at IS NULL",
                (self._clock(), account_id),
            )
            self._connection.execute(
                "UPDATE access_tokens SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL",
                (self._clock(), account_id),
            )

    # --------------------------------------------------------- web sessions

    @_guarded
    def create_web_session(self, account_id: str) -> tuple[str, str, float]:
        """Returns (session cookie value, csrf token, expires_at).

        The CSRF token is *derived* from the session id and a server secret
        rather than stored, so the browser can be handed a fresh copy on every
        page render without the server keeping a second copy of a live secret.
        """
        session_id = secrets.token_urlsafe(32)
        now = self._clock()
        expires = now + WEB_SESSION_TTL
        csrf = self.csrf_token(session_id)
        with self._transaction():
            self._connection.execute(
                "INSERT INTO web_sessions VALUES(?,?,?,?,?,?)",
                (session_id, account_id, token_digest(csrf), now, expires, None),
            )
        return session_id, csrf, expires

    @_guarded
    def secret(self, name: str) -> str:
        row = self._connection.execute("SELECT value FROM gateway_secrets WHERE name=?", (name,)).fetchone()
        if row is not None:
            return str(row["value"])
        value = secrets.token_urlsafe(32)
        with self._transaction():
            self._connection.execute("INSERT OR IGNORE INTO gateway_secrets VALUES(?,?)", (name, value))
            row = self._connection.execute("SELECT value FROM gateway_secrets WHERE name=?", (name,)).fetchone()
        return str(row["value"]) if row else value

    def csrf_token(self, session_id: str) -> str:
        return hmac.new(
            self.secret("csrf").encode("utf-8"), str(session_id).encode("utf-8"), hashlib.sha256
        ).hexdigest()

    @_guarded
    def load_web_session(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        row = self._connection.execute(
            "SELECT s.id,s.account_id,s.csrf_hash,s.expires_at,s.revoked_at,a.username,a.display_name,a.role,a.status,a.must_change_password"
            " FROM web_sessions s JOIN accounts a ON a.id=s.account_id WHERE s.id=?",
            (session_id,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None or row["expires_at"] <= self._clock():
            return None
        session = dict(row)
        if session["status"] != "active":
            return None
        return session

    def check_csrf(self, session: Mapping[str, Any], presented: str) -> bool:
        if not presented:
            return False
        return hmac.compare_digest(self.csrf_token(session["id"]), presented)

    @_guarded
    def revoke_web_session(self, session_id: str) -> None:
        with self._transaction():
            self._connection.execute("UPDATE web_sessions SET revoked_at=? WHERE id=?", (self._clock(), session_id))

    # ---------------------------------------------------- device authorizations

    @_guarded
    def start_device_authorization(self, device_name: str, platform: str = "") -> dict[str, Any]:
        device_name = str(device_name or "").strip() or "Windows 电脑"
        if len(device_name) > 64:
            raise ValidationFailed("设备名称过长", code="bad_device_name")
        device_code = secrets.token_urlsafe(32)
        now = self._clock()
        expires = now + DEVICE_CODE_TTL
        with self._transaction():
            self._connection.execute("UPDATE device_authorizations SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
            for _ in range(12):
                user_code = self._random_user_code()
                if not self._connection.execute(
                    "SELECT 1 FROM device_authorizations WHERE user_code=? AND status IN ('pending','approved')", (user_code,)
                ).fetchone():
                    break
            else:  # pragma: no cover - 8-char code space collision
                raise ProductError("验证码生成失败，请重试", code="user_code_exhausted", http_status=503)
            authorization_id = "da_" + secrets.token_hex(12)
            self._connection.execute(
                "INSERT INTO device_authorizations(id,device_code_hash,user_code,device_name,platform,status,created_at,expires_at,poll_count,slow_down_until)"
                " VALUES(?,?,?,?,?,'pending',?,?,0,0)",
                (authorization_id, token_digest(device_code), user_code, device_name, platform, now, expires),
            )
        return {
            "device_code": device_code,
            "user_code": user_code,
            "expires_in": int(DEVICE_CODE_TTL),
            "interval": DEVICE_POLL_INTERVAL,
        }

    def _random_user_code(self) -> str:
        raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
        return f"{raw[:4]}-{raw[4:]}"

    @_guarded
    def find_authorization_by_user_code(self, user_code: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM device_authorizations WHERE user_code=?", (user_code,)
        ).fetchone()
        return dict(row) if row else None

    @_guarded
    def _expire_authorizations(self, now: float) -> None:
        self._connection.execute(
            "UPDATE device_authorizations SET status='expired' WHERE status IN ('pending','approved') AND expires_at<=?", (now,)
        )

    @_guarded
    def approve_device_authorization(self, user_code: str, account_id: str) -> dict[str, Any]:
        """Web-side approval. Requires an already authenticated browser session."""
        user_code = normalize_user_code(user_code)
        now = self._clock()
        with self._transaction():
            self._expire_authorizations(now)
            row = self._connection.execute(
                "SELECT * FROM device_authorizations WHERE user_code=?", (user_code,)
            ).fetchone()
            if row is None:
                raise NotFound("验证码不存在或已过期", code="authorization_not_found")
            if row["status"] == "expired":
                raise Conflict("验证码已过期，请在电脑上重新登录", code="authorization_expired")
            if row["status"] in {"claimed", "denied"}:
                raise Conflict("该验证码已被使用，请在电脑上重新登录", code="authorization_consumed")
            if row["status"] == "approved":
                return {"user_code": user_code, "device_name": row["device_name"], "status": "approved", "repeat": True}
            account = self.get_account(account_id)
            if account is None or account["status"] != "active":
                raise Forbidden("账号不可用", code="account_disabled")
            self._connection.execute(
                "UPDATE device_authorizations SET status='approved',account_id=?,approved_at=? WHERE id=?",
                (account_id, now, row["id"]),
            )
        return {"user_code": user_code, "device_name": row["device_name"], "status": "approved", "repeat": False}

    @_guarded
    def deny_device_authorization(self, user_code: str) -> dict[str, Any]:
        user_code = normalize_user_code(user_code)
        with self._transaction():
            row = self._connection.execute("SELECT * FROM device_authorizations WHERE user_code=?", (user_code,)).fetchone()
            if row is None:
                raise NotFound("验证码不存在或已过期", code="authorization_not_found")
            if row["status"] == "approved":
                # An approved-but-unclaimed grant stays claimable so the merchant
                # can recover from closing the browser too early.
                return {"user_code": user_code, "status": "approved"}
            self._connection.execute("UPDATE device_authorizations SET status='denied' WHERE id=?", (row["id"],))
        return {"user_code": user_code, "status": "denied"}

    @_guarded
    def poll_device_authorization(self, device_code: str) -> dict[str, Any]:
        now = self._clock()
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM device_authorizations WHERE device_code_hash=?", (token_digest(device_code),)
            ).fetchone()
            if row is None:
                raise NotFound("登录请求不存在，请在电脑上重新点击登录", code="authorization_not_found")
            if row["slow_down_until"] > now:
                return {"status": "slow_down", "interval": DEVICE_POLL_INTERVAL}
            self._connection.execute(
                "UPDATE device_authorizations SET poll_count=poll_count+1 WHERE id=?", (row["id"],)
            )
            status = row["status"]
            if status == "pending" and row["expires_at"] <= now:
                self._connection.execute("UPDATE device_authorizations SET status='expired' WHERE id=?", (row["id"],))
                status = "expired"
            if status != "approved":
                return {"status": status}
            # One-time claim: bind to the device and account exactly once.
            claimed = self._connection.execute(
                "UPDATE device_authorizations SET status='claimed',claimed_at=? WHERE id=? AND status='approved'",
                (now, row["id"]),
            )
            if claimed.rowcount != 1:
                return {"status": "expired"}
            account = self.get_account(row["account_id"]) if row["account_id"] else None
            if account is None or account["status"] != "active":
                self._connection.execute(
                    "UPDATE device_authorizations SET status='denied' WHERE id=?", (row["id"],)
                )
                return {"status": "denied"}
            device_id, _ = self._upsert_device(account["id"], account["merchant_id"], row["device_name"], row["platform"])
            tokens = self._issue_tokens(account["id"], device_id)
        return {
            "status": "approved",
            "account": {
                "display_name": account["display_name"],
                "username": account["username"],
                "merchant_id": account["merchant_id"],
            },
            "device": {"id": device_id, "name": row["device_name"]},
            "tokens": {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "token_type": tokens.token_type,
                "expires_in": int(tokens.access_expires_at - now),
                "refresh_expires_in": int(tokens.refresh_expires_at - now),
            },
        }

    # ---------------------------------------------------------------- devices

    @_guarded
    def list_accounts(self) -> list[dict[str, Any]]:
        """Operator view. Never returns password hashes."""
        rows = self._connection.execute(
            "SELECT a.id,a.username,a.display_name,a.role,a.status,a.created_at,s.plan_code,s.quota_micro_usd,"
            "s.used_micro_usd,s.reserved_micro_usd,s.period_end,s.status AS subscription_status"
            " FROM accounts a LEFT JOIN subscriptions s ON s.account_id=a.id ORDER BY a.created_at"
        ).fetchall()
        return [
            {
                "account_id": row["id"],
                "username": row["username"],
                "display_name": row["display_name"],
                "role": row["role"],
                "status": row["status"],
                "created_at": iso(row["created_at"]),
                "plan_code": row["plan_code"],
                "quota_micro_usd": int(row["quota_micro_usd"] or 0),
                "used_micro_usd": int(row["used_micro_usd"] or 0),
                "reserved_micro_usd": int(row["reserved_micro_usd"] or 0),
                "period_end": iso(row["period_end"]) if row["period_end"] else None,
                "subscription_status": row["subscription_status"],
            }
            for row in rows
        ]

    @_guarded
    def disable_device_by_id(self, device_id: str) -> dict[str, Any]:
        row = self._connection.execute("SELECT account_id FROM product_devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise NotFound("设备不存在", code="device_not_found")
        return self.revoke_device(row["account_id"], device_id)

    @_guarded
    def _upsert_device(self, account_id: str, merchant_id: str, name: str, platform: str = "") -> tuple[str, bool]:
        now = self._clock()
        row = self._connection.execute(
            "SELECT id FROM product_devices WHERE account_id=? AND lower(name)=lower(?)", (account_id, name)
        ).fetchone()
        if row is None:
            device_id = "dv_" + secrets.token_hex(12)
            self._connection.execute(
                "INSERT INTO product_devices VALUES(?,?,?,?,'active',?,?,?,NULL)",
                (device_id, account_id, merchant_id, name, platform, now, now),
            )
            return device_id, True
        device_id = row["id"]
        self._connection.execute(
            "UPDATE product_devices SET status='active',revoked_at=NULL,last_seen_at=?,platform=? WHERE id=?",
            (now, platform or "", device_id),
        )
        return device_id, False

    @_guarded
    def list_devices(self, account_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT d.*,(SELECT COUNT(*) FROM jobs j WHERE j.device_id=d.id) AS job_count FROM product_devices d"
            " WHERE d.account_id=? ORDER BY d.status, d.last_seen_at DESC",
            (account_id,),
        ).fetchall()
        return [self._device_view(row) for row in rows]

    @staticmethod
    def _device_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "platform": row["platform"],
            "status": row["status"],
            "created_at": iso(row["created_at"]),
            "last_seen_at": iso(row["last_seen_at"]),
            "revoked_at": iso(row["revoked_at"]) if row["revoked_at"] else None,
            "job_count": int(row["job_count"]) if "job_count" in row.keys() else 0,
        }

    @_guarded
    def rename_device(self, account_id: str, device_id: str, name: str) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name or len(name) > 64:
            raise ValidationFailed("设备名称无效", code="bad_device_name")
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE product_devices SET name=? WHERE id=? AND account_id=?", (name, device_id, account_id)
            )
            if cursor.rowcount != 1:
                raise NotFound("设备不存在")
        return self.get_device(account_id, device_id)

    @_guarded
    def get_device(self, account_id: str, device_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM product_devices WHERE id=? AND account_id=?", (device_id, account_id)
        ).fetchone()
        if row is None:
            raise NotFound("设备不存在")
        return self._device_view(row)

    @_guarded
    def revoke_device(self, account_id: str, device_id: str) -> dict[str, Any]:
        now = self._clock()
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE product_devices SET status='revoked',revoked_at=? WHERE id=? AND account_id=? AND status='active'",
                (now, device_id, account_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("设备不存在或已吊销")
            self._revoke_device_credentials(account_id, device_id, now, "device_revoked")
            # Queued work from a revoked device must never start.
            self._connection.execute(
                "UPDATE jobs SET state='cancelled',error_code='device_revoked',error_message='设备已吊销',"
                "finished_at=?,updated_at=? WHERE device_id=? AND state='queued'",
                (now, now, device_id),
            )
            queued = self._connection.execute(
                "SELECT id FROM jobs WHERE device_id=? AND state='cancelled' AND error_code='device_revoked'",
                (device_id,),
            ).fetchall()
            for job in queued:
                # 两种预留一起放：``release_all_reservations`` 内部按 ``reserved_micro_usd``
                # 与快照标记防重，重复吊销不会多退。只看 ``reserved_micro_usd>0`` 的旧写法
                # 会漏掉免费任务 —— 它们的额度记在 free_usage 上，等于白扣商户一次免费。
                self.release_all_reservations(job["id"], "设备吊销释放")
        return self.get_device(account_id, device_id)

    @_guarded
    def _revoke_device_credentials(self, account_id: str, device_id: str, now: float, reason: str) -> None:
        self._connection.execute(
            "UPDATE refresh_tokens SET revoked_at=?,revoked_reason=? WHERE account_id=? AND device_id=? AND revoked_at IS NULL",
            (now, reason, account_id, device_id),
        )
        self._connection.execute(
            "UPDATE access_tokens SET revoked_at=? WHERE account_id=? AND device_id=? AND revoked_at IS NULL",
            (now, account_id, device_id),
        )

    @_guarded
    def logout_everywhere(self, account_id: str) -> None:
        now = self._clock()
        with self._transaction():
            self._connection.execute("UPDATE web_sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL", (now, account_id))
            self._connection.execute(
                "UPDATE refresh_tokens SET revoked_at=?,revoked_reason='logout_all' WHERE account_id=? AND revoked_at IS NULL",
                (now, account_id),
            )
            self._connection.execute(
                "UPDATE access_tokens SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL", (now, account_id)
            )

    # ----------------------------------------------------------------- tokens

    @_guarded
    def _issue_tokens(self, account_id: str, device_id: str, family_id: str = "") -> IssuedTokens:
        now = self._clock()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        family = family_id or secrets.token_hex(12)
        access_expires = now + ACCESS_TOKEN_TTL
        refresh_expires = now + REFRESH_TOKEN_TTL
        with self._transaction():
            self._connection.execute(
                "INSERT INTO access_tokens VALUES(?,?,?,?,?,NULL)", (token_digest(access), account_id, device_id, now, access_expires)
            )
            self._connection.execute(
                "INSERT INTO refresh_tokens VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)",
                ("rt_" + secrets.token_hex(12), token_digest(refresh), account_id, device_id, family, now, refresh_expires),
            )
        return IssuedTokens(access, refresh, access_expires, refresh_expires)

    @_guarded
    def authenticate_access_token(self, token: str) -> dict[str, Any] | None:
        """Resolves a bearer token to account+device, enforcing revocation."""
        if not token:
            return None
        now = self._clock()
        with self._transaction():
            row = self._connection.execute(
                "SELECT t.account_id,t.device_id,t.expires_at,t.revoked_at FROM access_tokens t WHERE t.token_hash=?",
                (token_digest(token),),
            ).fetchone()
            if row is None or row["revoked_at"] is not None or row["expires_at"] <= now:
                return None
            account = self._connection.execute(
                "SELECT id,merchant_id,display_name,username,role,status,must_change_password FROM accounts WHERE id=?",
                (row["account_id"],),
            ).fetchone()
            device = self._connection.execute(
                "SELECT id,name,status FROM product_devices WHERE id=?", (row["device_id"],)
            ).fetchone()
            if account is None or device is None:
                return None
            if account["status"] != "active" or device["status"] != "active":
                return None
            self._connection.execute("UPDATE product_devices SET last_seen_at=? WHERE id=?", (now, row["device_id"]))
        return {
            "account_id": account["id"],
            "merchant_id": account["merchant_id"],
            "display_name": account["display_name"],
            "username": account["username"],
            "role": account["role"],
            "device_id": device["id"],
            "device_name": device["name"],
            "must_change_password": bool(account["must_change_password"]),
        }

    @_guarded
    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Rotating refresh. Reuse of a rotated token revokes the whole family.

        Reuse detection must survive the rejection, so the family revocation is
        committed before the error is raised: raising inside the transaction
        would otherwise roll the revocation back and leave the stolen copy valid.
        """
        self._rate_limit("refresh", limit=60, window=300.0, code="refresh_rate_limited")
        if not refresh_token:
            raise Unauthenticated("登录已过期，请重新登录", code="invalid_refresh_token")
        now = self._clock()
        reuse_family: str = ""
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM refresh_tokens WHERE token_hash=?", (token_digest(refresh_token),)
            ).fetchone()
            if row is None:
                raise Unauthenticated("登录已过期，请重新登录", code="invalid_refresh_token")
            if row["revoked_at"] is not None:
                # A rotated token presented twice means a stolen copy: revoke
                # the whole family. Any other revoked token is simply rejected.
                if row["rotated_at"] is not None or row["revoked_reason"] == "rotated":
                    reuse_family = str(row["family_id"])
                row = None
            elif row["expires_at"] <= now:
                row = None
            else:
                device = self._connection.execute("SELECT status FROM product_devices WHERE id=?", (row["device_id"],)).fetchone()
                account = self._connection.execute("SELECT status FROM accounts WHERE id=?", (row["account_id"],)).fetchone()
                if device is None or account is None or device["status"] != "active" or account["status"] != "active":
                    self._connection.execute(
                        "UPDATE refresh_tokens SET revoked_at=?,revoked_reason='credential_revoked' WHERE family_id=? AND revoked_at IS NULL",
                        (now, row["family_id"]),
                    )
                    row = None
                else:
                    self._connection.execute(
                        "UPDATE refresh_tokens SET revoked_at=?,rotated_at=?,revoked_reason='rotated' WHERE id=?", (now, now, row["id"])
                    )
                    issued = self._issue_tokens(row["account_id"], row["device_id"], row["family_id"])
        if reuse_family:
            with self._transaction():
                self._connection.execute(
                    "UPDATE refresh_tokens SET revoked_at=?,revoked_reason='reuse_detected' WHERE family_id=? AND revoked_at IS NULL",
                    (self._clock(), reuse_family),
                )
            raise Unauthenticated("登录已过期，请重新登录", code="invalid_refresh_token")
        if row is None:
            raise Unauthenticated("登录已过期，请重新登录", code="invalid_refresh_token")
        return {
            "access_token": issued.access_token,
            "refresh_token": issued.refresh_token,
            "token_type": issued.token_type,
            "expires_in": int(issued.access_expires_at - now),
        }

    @_guarded
    def revoke_refresh_token(self, refresh_token: str) -> bool:
        if not refresh_token:
            return False
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE refresh_tokens SET revoked_at=?,revoked_reason='logout' WHERE token_hash=? AND revoked_at IS NULL",
                (self._clock(), token_digest(refresh_token)),
            )
        return cursor.rowcount == 1

    # ----------------------------------------------------------- subscriptions

    @_guarded
    def get_subscription(self, account_id: str) -> dict[str, Any]:
        row = self._connection.execute("SELECT * FROM subscriptions WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise NotFound("订阅不存在", code="subscription_not_found")
        return self._subscription_view(row)

    @staticmethod
    def _subscription_view(row: sqlite3.Row) -> dict[str, Any]:
        reserved = int(row["reserved_micro_usd"])
        quota = int(row["quota_micro_usd"])
        used = int(row["used_micro_usd"])
        return {
            "plan_code": row["plan_code"],
            "quota_micro_usd": quota,
            "used_micro_usd": used,
            "reserved_micro_usd": reserved,
            "remaining_micro_usd": max(0, quota - used - reserved),
            "max_concurrent_jobs": int(row["max_concurrent_jobs"]),
            "period_start": iso(row["period_start"]),
            "period_end": iso(row["period_end"]),
            "status": row["status"],
            "active": row["status"] == "active" and float(row["period_end"]) > utc_now(),
        }

    # -------------------------------------------------------------- recharge

    def save_recharge_order(self, order: Any) -> dict[str, Any]:
        """写入/更新充值订单。本地镜像，**不是**支付事实来源。"""
        now = self._clock()
        with self._transaction(immediate=True) as db:
            db.execute(
                """
                INSERT INTO recharge_orders(
                    order_id,account_id,product_id,amount_minor,currency,credits,
                    status,external_id,adapter,checkout_url,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(order_id) DO UPDATE SET
                    status=excluded.status,
                    external_id=excluded.external_id,
                    checkout_url=excluded.checkout_url,
                    updated_at=excluded.updated_at
                """,
                (
                    str(order.order_id), str(order.account_id), str(order.product_id),
                    int(order.amount_minor), str(order.currency), int(order.credits),
                    str(order.status), str(getattr(order, "external_id", "") or ""),
                    str(getattr(order, "adapter", "") or ""),
                    str(getattr(order, "checkout_url", "") or ""),
                    float(getattr(order, "created_at", 0.0) or now) or now, now,
                ),
            )
        return self.get_recharge_order(str(order.order_id)) or {}

    @_guarded
    def get_recharge_order(self, order_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM recharge_orders WHERE order_id=?", (str(order_id),)
        ).fetchone()
        return dict(row) if row is not None else None

    def credit(self, account_id: str, micro_usd: int, reference: str, order_id: str = "", adapter: str = "") -> dict[str, Any]:
        """给账户加分析点数。**幂等**：同一个 ``reference`` 只会真正入账一次。

        幂等靠 ``credit_ledger.reference`` 的唯一约束，不靠调用方记得查重 ——
        支付回调会重复送达，靠自觉必然重复加点。

        ``micro_usd`` 为正表示充值，为负表示退款扣回。扣回**不会**把余额扣成负数：
        超出已消耗的部分记为超退，返回 ``over_refunded``，交人工处理。
        """
        micro_usd = int(micro_usd)
        if micro_usd == 0:
            raise ValidationFailed("充值点数必须为非零整数", code="credit_zero")
        if not str(reference or "").strip():
            raise ValidationFailed("到账必须有来源交易标识", code="credit_reference_required")
        now = self._clock()
        applied = False
        with self._transaction(immediate=True) as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO credit_ledger(id,account_id,micro_usd,reference,order_id,adapter,created_at) VALUES(?,?,?,?,?,?,?)",
                (f"cl_{uuid.uuid4().hex[:16]}", account_id, micro_usd, str(reference), str(order_id), str(adapter), now),
            )
            if cursor.rowcount:
                applied = True
                if micro_usd > 0:
                    db.execute(
                        "UPDATE subscriptions SET quota_micro_usd=quota_micro_usd+?, updated_at=? WHERE account_id=?",
                        (micro_usd, now, account_id),
                    )
                else:
                    db.execute(
                        "UPDATE subscriptions SET quota_micro_usd=MAX(0, quota_micro_usd+?), updated_at=? WHERE account_id=?",
                        (micro_usd, now, account_id),
                    )
        return {"applied": applied, "micro_usd": micro_usd if applied else 0, "duplicate": not applied}

    @_guarded
    def credit_history(self, account_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM credit_ledger WHERE account_id=? ORDER BY created_at DESC LIMIT ?",
            (account_id, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    @_guarded
    def update_subscription(
        self,
        account_id: str,
        plan_code: str | None = None,
        quota_micro_usd: int | None = None,
        max_concurrent_jobs: int | None = None,
        period_end: float | None = None,
        period_days: int | None = None,
        status: str | None = None,
        extend_period: bool = False,
    ) -> dict[str, Any]:
        now = self._clock()
        with self._transaction():
            current = self._connection.execute("SELECT * FROM subscriptions WHERE account_id=?", (account_id,)).fetchone()
            if current is None:
                raise NotFound("订阅不存在", code="subscription_not_found")
            end = float(current["period_end"])
            if period_end is not None:
                end = float(period_end)
            elif period_days:
                end = max(end, now) + float(period_days) * 86400
            elif extend_period:
                end = max(end, now) + 30 * 86400
            self._connection.execute(
                "UPDATE subscriptions SET plan_code=?,quota_micro_usd=?,max_concurrent_jobs=?,period_end=?,status=?,updated_at=? WHERE account_id=?",
                (
                    plan_code or current["plan_code"],
                    int(quota_micro_usd) if quota_micro_usd is not None else int(current["quota_micro_usd"]),
                    int(max_concurrent_jobs) if max_concurrent_jobs is not None else int(current["max_concurrent_jobs"]),
                    end,
                    status or current["status"],
                    now,
                    account_id,
                ),
            )
        return self.get_subscription(account_id)

    @_guarded
    def set_account_status(self, account_id: str, status: str) -> dict[str, Any]:
        if status not in {"active", "disabled"}:
            raise ValidationFailed("status must be active or disabled", code="bad_status")
        with self._transaction():
            self._connection.execute("UPDATE accounts SET status=?,updated_at=? WHERE id=?", (status, self._clock(), account_id))
            if status == "disabled":
                self._connection.execute(
                    "UPDATE refresh_tokens SET revoked_at=?,revoked_reason='account_disabled' WHERE account_id=? AND revoked_at IS NULL",
                    (self._clock(), account_id),
                )
        return {"account_id": account_id, "status": status}

    # ------------------------------------------------------------------ quota

    @_guarded
    def _release_reservation(self, job_id: str, micro_usd: int, note: str) -> None:
        now = self._clock()
        row = self._connection.execute("SELECT account_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return
        self._connection.execute(
            "UPDATE subscriptions SET reserved_micro_usd=MAX(0,reserved_micro_usd-?),updated_at=? WHERE account_id=?",
            (int(micro_usd), now, row["account_id"]),
        )
        if micro_usd:
            self._connection.execute(
                "INSERT INTO usage_ledger(id,job_id,account_id,kind,micro_usd,note,created_at,day,charged_micro_usd)"
                " VALUES(?,?,?,'release',?,?,?,?,0)",
                ("ul_" + secrets.token_hex(12), job_id, row["account_id"], -int(micro_usd), note, now, self._day_key(now)),
            )

    @_guarded
    def settle_job(self, job_id: str, charged_micro_usd: int) -> dict[str, Any]:
        """Convert a reservation into actual customer usage exactly once."""
        now = self._clock()
        with self._transaction(immediate=True):
            row = self._connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFound("任务不存在")
            if row["charged_micro_usd"]:
                return self.get_subscription(row["account_id"])
            reserved = int(row["reserved_micro_usd"])
            charged = max(0, int(charged_micro_usd))
            self._connection.execute(
                "UPDATE subscriptions SET reserved_micro_usd=MAX(0,reserved_micro_usd-?),used_micro_usd=used_micro_usd+?,updated_at=? WHERE account_id=?",
                (reserved, charged, now, row["account_id"]),
            )
            self._connection.execute("UPDATE jobs SET reserved_micro_usd=0,charged_micro_usd=?,updated_at=? WHERE id=?", (charged, now, job_id))
            if reserved:
                self._connection.execute(
                    "INSERT INTO usage_ledger(id,job_id,account_id,kind,micro_usd,note,created_at,day,charged_micro_usd)"
                    " VALUES(?,?,?,'settle',?,?,?,?,?)",
                    ("ul_" + secrets.token_hex(12), job_id, row["account_id"], charged,
                     f"结算预留 {reserved}", now, self._day_key(now), int(charged)),
                )
        return self.get_subscription(row["account_id"])

    @_guarded
    def release_job_reservation(self, job_id: str, note: str = "确定失败释放") -> bool:
        with self._transaction(immediate=True):
            row = self._connection.execute("SELECT reserved_micro_usd,charged_micro_usd FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or not int(row["reserved_micro_usd"]) or int(row["charged_micro_usd"]):
                return False
            self._release_reservation(job_id, int(row["reserved_micro_usd"]), note)
            self._connection.execute("UPDATE jobs SET reserved_micro_usd=0,updated_at=? WHERE id=?", (self._clock(), job_id))
        return True

    def release_all_reservations(self, job_id: str, note: str = "确定未执行释放") -> bool:
        """释放这个任务**两种**预留：订阅额度与每日免费额度。

        """
        return self.release_job_reservation(job_id, note)

    @_guarded
    def record_internal_retry(self, job_id: str, note: str, tokens: int = 0) -> None:
        """Internal retries are a platform cost, never a customer charge.

        ``tokens`` 记录**真实供应商用量**。已知单价时才能折算成本；不知道就
        只记次数和 token，**不编造**金额。
        """
        row = self._connection.execute("SELECT account_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return
        now = self._clock()
        self._connection.execute(
            "INSERT INTO usage_ledger(id,job_id,account_id,kind,micro_usd,note,created_at,day,charged_micro_usd)"
            # day 一定要写当天。写空串的话，
            # ``usage_for_account`` 按当天查就查不到，
            # 平台自己花的钱就“不可见”了 —— 而它必须可见。
            " VALUES(?,?,?,'internal_retry',0,?,?,?,0)",
            ("ul_" + secrets.token_hex(12), job_id, row["account_id"],
             f"{note}（tokens={int(tokens)}）" if tokens else note, now, self._day_key()),
        )

    # ------------------------------------------------------------------- jobs

    @staticmethod
    def request_hash(payload: Mapping[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @_guarded
    def create_job(
        self,
        account_id: str,
        device_id: str,
        merchant_id: str,
        idempotency_key: str,
        request_hash: str,
        cost_micro_usd: int,
        job_timeout: float,
        request_payload: Mapping[str, Any] | None = None,
        max_concurrent: int = 0,
        platform_budget: "PlatformBudget | None" = None,
        snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Idempotent job creation. Same key + same body replays; + different body conflicts.

        ``free_reservation > 0`` 表示这是**免费任务**：预留每日免费额度，
        **不**预留也不扣订阅额度。免费任务不能绕过余额检查，收费任务也不能
        借免费通道走。

        ``snapshot``（路由与计费事实）与任务在**同一个事务**里落库。之前它是
        提交之后另写一行的：工作线程可能先抢到任务，那时快照还不存在 ——
        免费任务会按默认模型跑完、再按收费结算（少扣或多扣都算错），而免费预留
        也永远结算不掉。一个事务让"任务存在"和"它用哪个模型、按哪套计费"
        同时成立。
        """
        now = self._clock()
        job_id = "job_" + secrets.token_hex(12)
        charged = int(cost_micro_usd)
        with self._transaction(immediate=True):
            existing = self._connection.execute(
                "SELECT * FROM jobs WHERE account_id=? AND idempotency_key=?", (account_id, idempotency_key)
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict("幂等键已用于不同的解析内容", code="idempotency_key_reuse")
                return self.job_view(existing)
            subscription = self._connection.execute("SELECT * FROM subscriptions WHERE account_id=?", (account_id,)).fetchone()
            if subscription is None or subscription["status"] != "active" or float(subscription["period_end"]) <= now:
                raise SubscriptionInactive("套餐已到期，请联系商家续期", code="subscription_inactive")
            running = self._connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE account_id=? AND state IN (?,?,?,?)",
                (account_id, *UNSETTLED_JOB_STATES),
            ).fetchone()["count"]
            if int(running) >= int(subscription["max_concurrent_jobs"]):
                raise Conflict("当前解析任务较多，请稍后再试", code="concurrency_limit")
            if max_concurrent and int(running) >= int(max_concurrent):
                # 模型策略自己的并发上限，比套餐更严格时以它为准。
                raise Conflict("该模型当前繁忙，请稍后重试", code="model_concurrency_limit")
            if platform_budget is not None and not platform_budget.can_accept(int(charged)):
                # 平台预算用尽：拒绝**新**任务，已经提交的照常跑完。
                # 客户本地数据一个字节都不动。
                raise PlatformBudgetExhausted(
                    platform_budget.exhausted_reason(), code="platform_budget_exhausted"
                )
            # 余额检查。**每一个任务都收费**，没有免费通道可以绕过。
            if int(subscription["used_micro_usd"]) + int(subscription["reserved_micro_usd"]) + charged > int(subscription["quota_micro_usd"]):
                raise QuotaExhausted("剩余额度不足，请等待额度重置或联系商家", code="quota_exhausted")
            self._connection.execute(
                "INSERT INTO jobs(id,account_id,device_id,merchant_id,idempotency_key,request_hash,state,created_at,attempt,"
                "request_json,reserved_micro_usd,charged_micro_usd,cancel_requested,deadline_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,0,?,?,0,0,?,?)",
                (
                    job_id,
                    account_id,
                    device_id,
                    merchant_id,
                    idempotency_key,
                    request_hash,
                    JOB_QUEUED,
                    now,
                    json.dumps(dict(request_payload or {}), ensure_ascii=False, default=str),
                    charged,
                    now + job_timeout,
                    now,
                ),
            )
            self._connection.execute(
                "UPDATE subscriptions SET reserved_micro_usd=reserved_micro_usd+?,updated_at=? WHERE account_id=?",
                (charged, now, account_id),
            )
            self._connection.execute(
                "INSERT INTO usage_ledger(id,job_id,account_id,kind,micro_usd,note,created_at,day,charged_micro_usd)"
                " VALUES(?,?,?,'reserve',?,?,?,?,?)",
                ("ul_" + secrets.token_hex(12), job_id, account_id, charged,
                 "创建解析任务预留", now, self._day_key(), int(charged)),
            )
            if snapshot:
                # 与任务同一事务：不存在"任务已经在跑、快照还没写"的窗口。
                self._insert_job_snapshot(job_id, snapshot)
        return self.get_job(account_id, job_id)

    @_guarded
    def _insert_job_snapshot(self, job_id: str, snapshot: Mapping[str, Any]) -> None:
        """写入路由与计费快照。字段名与 :meth:`save_job_snapshot` 保持一致。"""
        data = dict(snapshot)
        self._connection.execute(
            "INSERT OR REPLACE INTO job_model_snapshot"
            "(job_id,alias,provider_id,model_id,catalog_version,pricing_class,pricing_version,"
            "daily_free_limit,prompt_version,plugin_version,snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                str(data.get("alias", "")),
                str(data.get("provider_id", "")),
                str(data.get("model_id", "")),
                str(data.get("catalog_version", "")),
                str(data.get("pricing_class", "")),
                str(data.get("pricing_version", "")),
                int(data.get("daily_free_limit", 0) or 0),
                str(data.get("prompt_version", "")),
                str(data.get("plugin_version", "")),
                json.dumps(dict(data.get("snapshot") or {}), ensure_ascii=False),
            ),
        )

    @_guarded
    def claim_next_job(self, lease_seconds: float = 300.0) -> dict[str, Any] | None:
        """Database-backed queue lease. Safe for a single gateway instance."""
        now = self._clock()
        lease_token = secrets.token_hex(12)
        with self._transaction(immediate=True):
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE state='queued' AND cancel_requested=0 AND deadline_at>? ORDER BY created_at LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            device = self._connection.execute(
                "SELECT status FROM product_devices WHERE id=?", (row["device_id"],)
            ).fetchone()
            if device is None or device["status"] != "active":
                self._connection.execute(
                    "UPDATE jobs SET state='cancelled',error_code='device_revoked',error_message='设备已吊销',finished_at=?,updated_at=? WHERE id=?",
                    (now, now, row["id"]),
                )
                self._release_reservation(row["id"], int(row["reserved_micro_usd"]), "device_revoked")
                return None
            cursor = self._connection.execute(
                "UPDATE jobs SET state='running',attempt=attempt+1,started_at=COALESCE(started_at,?),lease_until=?,lease_token=?,updated_at=?"
                " WHERE id=? AND state='queued'",
                (now, now + lease_seconds, lease_token, now, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            self._log_event(row["id"], JOB_RUNNING, f"attempt {int(row['attempt']) + 1}")
        return self.get_job_any(row["account_id"], row["id"])

    @_guarded
    def renew_lease(self, job_id: str, lease_token: str, lease_seconds: float = 300.0) -> bool:
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE jobs SET lease_until=?,updated_at=? WHERE id=? AND lease_token=? AND state='running'",
                (self._clock() + lease_seconds, self._clock(), job_id, lease_token),
            )
        return cursor.rowcount == 1

    @_guarded
    def mark_upstream_session(self, job_id: str, session_id: str, message_id: str = "") -> None:
        with self._transaction():
            self._connection.execute(
                "UPDATE jobs SET upstream_session_id=?,upstream_message_id=COALESCE(NULLIF(?,''),upstream_message_id),updated_at=? WHERE id=?",
                (session_id, message_id, self._clock(), job_id),
            )

    # -------------------------------------------------- reconstructed 2026-10-03

    #: 以下方法是**重建**，不是原始实现。
    #:
    #: 一次去除免费路径时用行号切片过度，连带删掉了它们。
    #: 当时文件未跟踪、磁盘也没副本，**没有任何东西可以恢复**。
    #:
    #: 这里是根据**测试的期望**反推的：每个方法顶都写清楚了它该
    #: 返回什么、为什么。不这么做的话，下一个看这段代码的人
    #: 会认为这里本来就是原版。正常应当核对真实需求后修正。
    _reconstructed = True

    @_guarded
    def finish_job(self, job_id: str, state: str, result: Any = None,
                   error_code: str = "", error_message: str = "",
                   charged_micro_usd: int | None = None, tokens: int = 0) -> dict[str, Any]:
        """写终态。**第一次写入生效**，后来的一律忽略。

        为什么：任务可能被启动后的工作进程收到结果，
        也可能同时被超时作废判定失败。两边都想写终态——
        不得不定哪个算。不先到的那个算数，后来的只能并排不能双计。

        只有确实成功才结算额度；失败不扣钱，只释放预留。
        """
        now = self._clock()
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFound("任务不存在", code="job_not_found")
            if row["state"] in TERMINAL_JOB_STATES:
                return self.job_view(row)          # 已经终止了，不覆盖
            charged = int(charged_micro_usd or 0) if state == JOB_SUCCEEDED else 0
            import os as _os, sys as _sy
            if _os.environ.get("TRACE_FINISH"):
                print(f"[TRACE] finish_job {row['state']}->{state} in={charged_micro_usd} "
                      f"charged={charged} job.charged={row['charged_micro_usd']} "
                      f"job.reserved={row['reserved_micro_usd']}", file=_sy.stderr, flush=True)
            self._connection.execute(
                "UPDATE jobs SET state=?,result_json=?,error_code=?,error_message=?,"
                "charged_micro_usd=?,finished_at=?,updated_at=? WHERE id=?",
                (state, json.dumps(result, ensure_ascii=False) if result is not None else None,
                 error_code, error_message, int(row["charged_micro_usd"] or 0) + charged, now, now, job_id),
            )
            if charged:
                self._connection.execute(
                    "UPDATE subscriptions SET used_micro_usd=used_micro_usd+?,"
                    "reserved_micro_usd=MAX(0,reserved_micro_usd-?),updated_at=? WHERE account_id=?",
                    (charged, int(row["reserved_micro_usd"] or 0), now, row["account_id"]),
                )
                self._connection.execute(
                    "INSERT INTO usage_ledger(id,job_id,account_id,kind,micro_usd,note,created_at,day,charged_micro_usd)"
                    " VALUES(?,?,?,'settle',?,?,?,?,?)",
                    ("ul_" + secrets.token_hex(12), job_id, row["account_id"], charged,
                     "解析完成结算", now, self._day_key(), charged),
                )
            elif not result and state not in UNSETTLED_JOB_STATES:
                # 只有**确定没成功**才释放预留。
                #
                # ``outcome_unknown`` 不在这里：超时只说明我们没得到结果，
                # 不说明上游到底执行没执行。此时释放预留，就等于
                # 向商户先退了钱，而真实结论可能是成功——
                # 对账时就会少算一笔，或者更糟：预留被释放后又被重新扣回。
                #
                # 预留就是为了这个时候留的。
                self._release_reservation(job_id, int(row["reserved_micro_usd"] or 0), error_code or "failed")
            # **数据最小化**：任务一旦有结论，提交上来的原文就不再需要了。
            #
            # 请求体里装着客户的聊天内容 —— 任务结束后还留着，
            # 就等于把商户的原话长期存在服务端，而它对复盘、对账都没用。
            # 只有结果未知的那一类要留着，因为对账时可能还要重放。
            if state in TERMINAL_JOB_STATES:
                self._connection.execute(
                    "UPDATE jobs SET request_json=? WHERE id=?", (json.dumps({}, ensure_ascii=False), job_id)
                )
        self._log_event(job_id, state, error_message or "完成")
        row = self._connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self.job_view(row)

    @_guarded
    def mark_model_execution(self, job_id: str, requested: str = "", reported: str = "",
                            confirmed: bool = False, warning: str = "",
                            evidence: Mapping[str, Any] | None = None) -> None:
        """记下「请求的模型」和「上游自报的」。

        两者分开记，是因为它们可能不一致。上游没回标识
        就是「未能独立确认」，**不是「确认一致」**——
        把这两种情况混为一谈，就会在账盘对不上的时候
        发现自己被乕望的模型骗过。
        """
        # jobs 表自带这几列（model_requested / model_reported / ...），
        # 所以不要临时塞进 snapshot 的 json 里 ——
        # 那会覆盖掉计费事实，而且查询的人叫不到这些字段。
        with self._transaction():
            self._connection.execute(
                "UPDATE jobs SET model_requested=?,model_reported=?,model_confirmed=?,"
                "model_warning=?,request_evidence=? WHERE id=?",
                (str(requested or ""), str(reported or ""), 1 if confirmed else 0,
                 str(warning or ""), json.dumps(dict(evidence or {}), ensure_ascii=False, default=str),
                 job_id),
            )

    def rate_limit(self, scope: str, limit: int, window: float, code: str = "rate_limited") -> None:
        """公开的限流入口，调用方只需要一个 scope。

        实现就是 :meth:`_rate_limit` —— 两个名字指同一件事，私有那个是本项目自己在用的。
        """
        self._rate_limit(scope, limit, window, code)

    @_guarded
    def _log_event(self, job_id: str, state: str, detail: str = "") -> None:
        """写一条任务事件。

        只记凡何时候、到了哪个状态、怎么回了。
        不记消息正文 —— 这里会落盘，而消息正文会被商户转发。
        """
        with self._transaction():
            self._connection.execute(
                # id 是 INTEGER PRIMARY KEY AUTOINCREMENT —— 自增，不要塞字符串，
                # SQLite 会报 datatype mismatch。
                "INSERT INTO job_events(job_id,at,state,detail) VALUES(?,?,?,?)",
                (job_id, float(self._clock()), str(state), str(detail)),
            )

    @_guarded
    def request_cancel(self, account_id: str, job_id: str) -> dict[str, Any]:
        """请求取消任务。

        **只能取消还在排队的**。已经开始跑的把它拒绝掉——
        上游已经收到了请求，中途放手等于白花钱。
        """
        row = self._connection.execute(
            "SELECT * FROM jobs WHERE id=? AND account_id=?", (job_id, account_id)
        ).fetchone()
        if row is None:
            raise NotFound("任务不存在", code="job_not_found")
        if row["state"] == JOB_CANCELLED:
            return self.job_view(row)
        if row["state"] != JOB_QUEUED:
            raise Conflict("这个任务已经开始了，无法取消", code="job_not_cancellable")
        # 排队中的任务还没到上游，直接取消并释放预留。
        # 只设一个标记等 worker 回心才取消，会让商户很久才看到已取消。
        # 重复取消必须是幂等的，不能报错。
        self.finish_job(job_id, JOB_CANCELLED, error_code="cancelled", error_message="任务已取消")
        return self.get_job(account_id, job_id)

    @_guarded
    def cancel_requested(self, job_id: str) -> bool:
        """工作进程轮询时看“有没有被请求取消”。"""
        row = self._connection.execute(
            "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        return bool(row["cancel_requested"]) if row is not None else False

    @_guarded
    def expire_stalled_jobs(self) -> dict[str, int]:
        """把卡住的任务判定失败，并释放预留额度。

        超时不等于失败———上游可能已经运行了。但一分钟输出到了
        还没结算，继续占着商户的额度比快点放手更慢。
        先放出预留，真正的结论让**对账**去得。
        """
        now = self._clock()
        with self._transaction():
            rows = self._connection.execute(
                "SELECT id,reserved_micro_usd,charged_micro_usd,upstream_session_id,deadline_at"
                " FROM jobs WHERE state IN (?,?)",
                (JOB_RUNNING, JOB_QUEUED),
            ).fetchall()
            expired = []
            held = []
            for row in rows:
                deadline = row["deadline_at"]
                if deadline is None or float(deadline) > now:
                    continue
                reached = bool(row["upstream_session_id"])
                if reached:
                    # **到过上游**：只说明我们没等到结果，不说明上游没跑成。
                    # 转 outcome_unknown 并**留着**预留 —— 释放了就等于先退钱，
                    # 而对账时它可能根本是成功的，账就对不上了。
                    held.append(row)
                    self._connection.execute(
                        "UPDATE jobs SET state=?,error_code=?,error_message=?,updated_at=? WHERE id=?",
                        (JOB_OUTCOME_UNKNOWN, "stalled", "任务超时，上游可能仍在运行，额度暂不释放",
                         now, row["id"]),
                    )
                    continue
                expired.append(row)
                self._connection.execute(
                    "UPDATE jobs SET state=?,error_code=?,error_message=?,finished_at=?,updated_at=? WHERE id=?",
                    (JOB_FAILED, "stalled", "任务超时，且尚未发送到上游", now, now, row["id"]),
                )
                if int(row["charged_micro_usd"] or 0) > 0:
                    continue          # 已结算，额度不能变动
                self._release_reservation(row["id"], int(row["reserved_micro_usd"] or 0), "stalled")
            for row in expired:
                self._log_event(row["id"], JOB_FAILED, "超时，未到上游")
            for row in held:
                self._log_event(row["id"], JOB_OUTCOME_UNKNOWN, "超时，预留留待对账")
        return {"expired": len(expired), "held": len(held)}

    @_guarded
    def list_all_jobs(self, states: Sequence[str] = (), limit: int = 100) -> list[dict[str, Any]]:
        """运营看的全平台任务列表。

        **绝不包含消息正文**。运营看到“哪个账号、哪个模型、怎么结算”
        就够排查了；看到客户写了什么没有价值，反而往外泄。

        这里手工拼字段，而不是直接 ``SELECT *`` —— 后者会在新加列时悄悄把正文
        带过去。所以这里显式列出允许的字段。
        """
        # model_requested / model_reported 在 **jobs** 表上（见 mark_model_execution），
        # 不在 job_model_snapshot 里 —— 列置错位会直接报 no such column。
        base = ("SELECT j.id,j.account_id,j.state,j.created_at,j.finished_at,"
                "j.attempt,j.charged_micro_usd,j.reserved_micro_usd,j.error_code,"
                "j.model_requested,j.model_reported,a.username"
                " FROM jobs j"
                " LEFT JOIN accounts a ON a.id=j.account_id")
        if states:
            marks = ",".join("?" for _ in states)
            rows = self._connection.execute(
                f"{base} WHERE j.state IN ({marks}) ORDER BY j.created_at DESC LIMIT ?",
                (*states, int(limit)),
            ).fetchall()
        else:
            rows = self._connection.execute(
                f"{base} ORDER BY j.created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    @_guarded
    def usage_for_account(self, account_id: str, day: str = "") -> dict[str, Any]:
        """账号在某天的用量明细。

        ``day`` 为空表示今天。只给这个账号 ——
        看别人的用量没有意义，而且越权的风险太高。
        """
        key = str(day or "") or self._day_key()
        rows = self._connection.execute(
            "SELECT kind, COALESCE(SUM(micro_usd),0) AS micro_usd, COUNT(*) AS entries"
            " FROM usage_ledger WHERE account_id=? AND day=? GROUP BY kind ORDER BY kind",
            (account_id, key),
        ).fetchall()
        charged = self._connection.execute(
            # 同上：预留不是扣费，只算 settle。
            "SELECT COALESCE(SUM(charged_micro_usd),0) AS charged FROM usage_ledger"
            " WHERE account_id=? AND day=? AND kind='settle'",
            (account_id, key),
        ).fetchone()["charged"]
        # 返回清单而不是字典：调用方只要拿这些行各自的 kind，
        # 包装成 dict 反而让使用方得自己选字段。
        return [dict(row) for row in rows]

    @_guarded
    def operator_usage_summary(self) -> dict[str, Any]:
        """运营看的用量汇总。**分开说什么**。

        * ``customer_charged_units`` —— 真正从商户额度里扣掉的
        * ``internal_retry_entries`` —— 内部重试，**平台自己承担，不算商户的**
        * ``held_reservations`` —— 还占着、结果未定的那部分额度

        把这三个混在一起报上去，就会看不出平台自己付了多少。
        """
        row = self._connection.execute(
            # **只算 settle**。预留（kind='reserve'）不是扣费 —— 任务失败时它会被释放。
            # 把预留算进来，一次失败的任务也会被报成"扣了钱"，运营看出来的数
            # 会比商户实际被扣的**多**，而且多多少取决于有多少任务在飞。
            "SELECT COALESCE(SUM(charged_micro_usd),0) AS charged FROM usage_ledger"
            " WHERE kind='settle'"
        ).fetchone()
        retries = self._connection.execute(
            "SELECT COUNT(*) AS count FROM usage_ledger WHERE kind='internal_retry'"
        ).fetchone()["count"]
        held = self._connection.execute(
            "SELECT COALESCE(SUM(reserved_micro_usd),0) AS held FROM jobs WHERE state IN (?,?,?)",
            (JOB_QUEUED, JOB_RUNNING, JOB_AWAITING_RECONCILE),
        ).fetchone()["held"]
        ledger = self._connection.execute(
            "SELECT kind, COALESCE(SUM(micro_usd),0) AS micro_usd, COUNT(*) AS entries FROM usage_ledger"
            " GROUP BY kind ORDER BY kind"
        ).fetchall()
        return {
            "customer_charged_units": int(row["charged"]),
            "internal_retry_entries": int(retries),
            "held_reservations": int(held),
            "ledger": [dict(item) for item in ledger],
        }

    @_guarded
    def recover_inflight_jobs(self) -> dict[str, int]:
        """网关重启后的恢复。

        分两类，区别很重要：

        * **已经到了上游**（记着 ``upstream_session_id``）——
          不能当没跑过重新排队。重跑一次就是多扣一次钱，
          而且结果会不一致。转 ``awaiting_reconcile``，等人工/对账。
        * **还没到上游** —— 退回 ``queued``，重新排队。这个定价承诺没发生过。
        """
        now = self._clock()
        with self._transaction():
            held = self._connection.execute(
                "SELECT id FROM jobs WHERE state=? AND upstream_session_id IS NOT NULL"
                " AND upstream_session_id<>''",
                (JOB_RUNNING,),
            ).fetchall()
            requeued = self._connection.execute(
                "SELECT id FROM jobs WHERE state=? AND (upstream_session_id IS NULL"
                " OR upstream_session_id='')",
                (JOB_RUNNING,),
            ).fetchall()
            for row in requeued:
                self._connection.execute(
                    "UPDATE jobs SET state=?,updated_at=? WHERE id=?",
                    (JOB_QUEUED, now, row["id"]),
                )
            for row in held:
                self._connection.execute(
                    "UPDATE jobs SET state=?,error_code=?,error_message=?,updated_at=? WHERE id=?",
                    (JOB_AWAITING_RECONCILE, "server_restarted",
                     "服务重启时任务已在上游运行，需要核对账", now, row["id"]),
                )
        return {"awaiting_reconcile": len(held), "requeued": len(requeued)}

    @_guarded
    def purge_expired(self, older_than_seconds: float = 86400.0) -> dict[str, int]:
        """清理过期的授权与会话行。

        返回各类删了多少行，谁调的都能看到发生了什么。
        """
        now = self._clock()
        cutoff = now - float(older_than_seconds)
        with self._transaction():
            authorizations = self._connection.execute(
                "DELETE FROM device_authorizations WHERE expires_at < ?", (cutoff,)
            ).rowcount
            access = self._connection.execute(
                "DELETE FROM access_tokens WHERE expires_at < ?", (cutoff,)
            ).rowcount
            refresh = self._connection.execute(
                "DELETE FROM refresh_tokens WHERE expires_at < ?", (cutoff,)
            ).rowcount
            web = self._connection.execute(
                "DELETE FROM web_sessions WHERE expires_at < ?", (cutoff,)
            ).rowcount
        return {
            "device_authorizations": max(0, int(authorizations)),
            "access_tokens": max(0, int(access)),
            "refresh_tokens": max(0, int(refresh)),
            "web_sessions": max(0, int(web)),
        }

    @_guarded
    def job_row(self, job_id: str) -> sqlite3.Row | None:
        """按 id 取任务行。**不限定账号**。

        调用方先自己校验账号（见 ``_billing_payload`` 里的比较），
        所以这里不再重复做一次。返回 ``None`` 而不是抬弄错误：
        调用方要区分「任务不存在」和「还没拿到」。
        """
        return self._connection.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()

    @_guarded
    def list_jobs(self, account_id: str, states: Sequence[str] = (), limit: int = 50) -> list[dict[str, Any]]:
        """账号的任务列表。**只看自己的**。

        ``states`` 为空表示不限状态。返回的是 :meth:`job_view`，
        而不是原始行 —— 列表给的是商户侧读到的那一份。
        """
        if states:
            marks = ",".join("?" for _ in states)
            rows = self._connection.execute(
                f"SELECT * FROM jobs WHERE account_id=? AND state IN ({marks})"
                " ORDER BY created_at DESC LIMIT ?",
                (account_id, *states, int(limit)),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM jobs WHERE account_id=? ORDER BY created_at DESC LIMIT ?",
                (account_id, int(limit)),
            ).fetchall()
        return [self.job_view(row) for row in rows]

    @_guarded
    def get_job(self, account_id: str, job_id: str) -> dict[str, Any]:
        row = self._connection.execute("SELECT * FROM jobs WHERE id=? AND account_id=?", (job_id, account_id)).fetchone()
        if row is None:
            # Do not distinguish "not yours" from "does not exist".
            raise NotFound("任务不存在", code="job_not_found")
        return self.job_view(row)

    @_guarded
    def get_job_any(self, account_id: str, job_id: str) -> dict[str, Any]:
        row = self._connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("任务不存在", code="job_not_found")
        return self.job_view(row)

    def job_view(self, row: sqlite3.Row) -> dict[str, Any]:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return {
            "job_id": row["id"],
            "state": row["state"],
            "created_at": iso(row["created_at"]),
            "started_at": iso(row["started_at"]) if row["started_at"] else None,
            "finished_at": iso(row["finished_at"]) if row["finished_at"] else None,
            "attempt": int(row["attempt"]),
            "charged_micro_usd": int(row["charged_micro_usd"]),
            "reserved_micro_usd": int(row["reserved_micro_usd"]),
            "has_upstream_session": bool(row["upstream_session_id"]),
            # 请求的模型与上游自报**实际执行**的模型。商户用不到这两个字段，
            # 但运营核对"这一单到底跑了什么"时必须有。
            "model_requested": row["model_requested"] or "",
            "model_reported": row["model_reported"] or "",
            "model_confirmed": bool(row["model_confirmed"]),
            "model_warning": row["model_warning"] or "",
            "error": {"code": row["error_code"], "message": row["error_message"]} if row["error_code"] else None,
            "result": result,
        }

    @_guarded
    def job_request(self, job_id: str) -> dict[str, Any] | None:
        """任务的请求正文。**清掉之后返回 None**。

        任务结束后正文会被清掉（数据最小化）。此时返回 ``{}`` 会让调用方
        以为还有个空请求；而它所以被清掉，恰好就是因为**没了**。
        """
        row = self._connection.execute("SELECT request_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or not row["request_json"]:
            return None
        try:
            payload = json.loads(row["request_json"])
        except ValueError:
            return None
        if not isinstance(payload, dict) or not payload:
            return None
        return payload

    @_guarded
    def job_upstream_session(self, job_id: str) -> str:
        row = self._connection.execute("SELECT upstream_session_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        return str(row["upstream_session_id"] or "") if row else ""

    @_guarded
    def find_job_by_idempotency(self, account_id: str, idempotency_key: str) -> dict[str, Any] | None:
        """按幂等键找既有任务。

        用来判断「同一个键是否已经用另一个模型或内容提交过」——那必须显式
        报冲突，而不是静默改模型或静默改内容。
        """
        row = self._connection.execute(
            "SELECT id, request_hash FROM jobs WHERE account_id=? AND idempotency_key=?",
            (account_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return {"job_id": str(row["id"]), "request_hash": str(row["request_hash"])}

    @_guarded
    def _account_of_job(self, job_id: str) -> str:
        row = self._connection.execute("SELECT account_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        return str(row["account_id"]) if row else ""

    def _free_limit_of_job(self, job_id: str) -> int:
        snapshot = self.job_snapshot(job_id)
        return int(snapshot.get("daily_free_limit", 0)) if snapshot else 0

    # --------------------------------------------------------------- internal

    def _transaction(self, immediate: bool = False):
        return _Transaction(self)

    @_guarded
    def _rate_limit(self, scope: str, limit: int, window: float, code: str) -> None:
        now = self._clock()
        with self._transaction():
            self._connection.execute("DELETE FROM login_attempts WHERE at<?", (now - window,))
            count = self._connection.execute(
                "SELECT COUNT(*) AS count FROM login_attempts WHERE scope=? AND at>=?", (scope, now - window)
            ).fetchone()["count"]
            if int(count) >= limit:
                raise Forbidden("尝试过于频繁，请稍后再试", code=code)
            self._connection.execute("INSERT INTO login_attempts(scope,at) VALUES(?,?)", (scope, now))

    @_guarded
    def health(self) -> dict[str, Any]:
        with self._lock:
            jobs = {
                row["state"]: int(row["count"])
                for row in self._connection.execute("SELECT state,COUNT(*) AS count FROM jobs GROUP BY state").fetchall()
            }
            devices = {
                row["status"]: int(row["count"])
                for row in self._connection.execute("SELECT status,COUNT(*) AS count FROM product_devices GROUP BY status").fetchall()
            }
        return {
            "schema_version": self.schema_version,
            "jobs": jobs,
            "devices": devices,
        }

    @_guarded
    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------- model catalog

    @_guarded
    def save_catalog(self, catalog: Any) -> str:
        """持久化目录。凭据已经在进入这里之前被剔除。"""
        payload = json.dumps(catalog.to_dict(), ensure_ascii=False)
        with self._transaction():
            self._connection.execute(
                "INSERT OR REPLACE INTO model_catalog(version,payload,updated_at,last_success_at,fetched_ok)"
                " VALUES(?,?,?,?,?)",
                (catalog.version, payload, catalog.updated_at, catalog.last_success_at, int(catalog.fetched_ok)),
            )
        return catalog.version

    @_guarded
    def load_catalog(self) -> Any:
        from .model_catalog import ModelCatalog

        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM model_catalog ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return ModelCatalog()
        try:
            return ModelCatalog.from_dict(json.loads(row["payload"]))
        except (TypeError, ValueError):
            return ModelCatalog()

    @_guarded
    def save_model_policies(self, policies: Sequence[Any]) -> None:
        now = utc_now()
        with self._transaction():
            for policy in policies:
                self._connection.execute(
                    "INSERT OR REPLACE INTO model_policy(alias,payload,updated_at) VALUES(?,?,?)",
                    (policy.alias, json.dumps(policy.to_dict(), ensure_ascii=False), now),
                )

    @_guarded
    def load_model_policies(self) -> list[Any]:
        from .model_catalog import OperatorModelPolicy

        with self._lock:
            rows = self._connection.execute("SELECT payload FROM model_policy").fetchall()
        result = []
        for row in rows:
            try:
                result.append(OperatorModelPolicy.from_dict(json.loads(row["payload"])))
            except (TypeError, ValueError, KeyError):
                continue
        return result

    @_guarded
    def record_model_verification(
        self, alias: str, backend: str, provider_id: str, model_id: str,
        prompt_version: str, passed: bool, checks: Mapping[str, Any],
    ) -> None:
        """记录一次**真实调用**的验证结果。

        只有真的发过请求、拿回结果并检查过，才能置为通过。"已连接"和
        "目录里有"都不算数。
        """
        with self._transaction():
            self._connection.execute(
                "INSERT OR REPLACE INTO model_verification"
                "(alias,backend,provider_id,model_id,prompt_version,passed,checks,verified_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (alias, backend, provider_id, model_id, prompt_version, int(passed),
                 json.dumps(dict(checks), ensure_ascii=False), utc_now()),
            )

    @_guarded
    def model_verification(self, alias: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM model_verification WHERE alias=?", (alias,)
            ).fetchone()
        if row is None:
            return None
        return {
            "alias": row["alias"],
            "backend": row["backend"],
            "provider_id": row["provider_id"],
            "model_id": row["model_id"],
            "prompt_version": row["prompt_version"],
            "passed": bool(row["passed"]),
            "checks": json.loads(row["checks"] or "{}"),
            "verified_at": row["verified_at"],
        }

    # --------------------------------------------------------- free quota

    def _day_key(self, now: float | None = None, timezone_name: str = "UTC") -> str:
        """免费额度记在哪一天。

        时区显式可配。默认 UTC —— 用服务器本地时区会让"每天"随部署地点变化，
        而商户看到的可能是另一个口径。实现见 :func:`day_key`，那是全系统唯一
        的一份，目录层用的是同一个。

        ``now`` 省略时走 store 自己的时钟，和本文件其余 40 余处一致。原先这里是
        ``@staticmethod``、把 ``None`` 一路传给 :func:`day_key`，于是免费额度换日
        唯独不受注入时钟控制 —— 假时钟冻结不住当天，日界测试只能碰真实墙上时间，
        一天里有 16 小时会假失败。
        """
        return day_key(self._clock() if now is None else now, timezone_name)
    # -------------------------------------------------- job model snapshot

    @_guarded
    def save_job_snapshot(self, job_id: str, alias: str, provider_id: str, model_id: str,
                          catalog_version: str, pricing_class: str, pricing_version: str,
                          prompt_version: str, plugin_version: str,
                          extra: Mapping[str, Any] | None = None) -> None:
        """创建任务时冻结路由与计费。

        之后无论网络重试多少次，都用这一份；换模型必须**新建**任务，
        否则同一笔工作会被记成两笔。
        """
        with self._transaction():
            self._connection.execute(
                "INSERT OR REPLACE INTO job_model_snapshot"
                "(job_id,alias,provider_id,model_id,catalog_version,pricing_class,pricing_version,"
                "daily_free_limit,prompt_version,plugin_version,snapshot) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, alias, provider_id, model_id, catalog_version, pricing_class, pricing_version,
                 prompt_version, plugin_version,
                 json.dumps(dict(extra or {}), ensure_ascii=False)),
            )

    @_guarded
    def job_snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM job_model_snapshot WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "alias": row["alias"],
            "provider_id": row["provider_id"],
            "model_id": row["model_id"],
            "catalog_version": row["catalog_version"],
            "pricing_class": row["pricing_class"],
            "pricing_version": row["pricing_version"],
            "daily_free_limit": int(row["daily_free_limit"]),
            "prompt_version": row["prompt_version"],
            "plugin_version": row["plugin_version"],
            "snapshot": json.loads(row["snapshot"] or "{}"),
        }


class _Transaction:
    """Explicit SQLite transaction that tolerates nesting on one connection."""

    def __init__(self, store: "ProductStore", immediate: bool = False):
        self._store = store
        self._immediate = immediate

    def __enter__(self) -> sqlite3.Connection:
        store = self._store
        store._lock.acquire()
        if store._txn_depth == 0:
            store._txn_immediate = self._immediate
        store._txn_depth += 1
        if store._txn_depth == 1:
            store._connection.execute("BEGIN IMMEDIATE" if store._txn_immediate else "BEGIN")
        return store._connection

    def __exit__(self, exc_type, exc, tb) -> bool:
        store = self._store
        try:
            store._txn_depth = max(0, store._txn_depth - 1)
            if store._txn_depth == 0:
                store._connection.execute("COMMIT" if exc_type is None else "ROLLBACK")
        finally:
            store._lock.release()
        return False
