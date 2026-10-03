"""Error taxonomy shared by every business agent run.

The distinction that matters most in production is between *"we do not know
what happened"* and *"we know it failed, stop asking"*.  A business ambiguity
is a successful run with a reviewable outcome -- retrying it as if it were a
network failure burns merchant quota and hides the real problem.
"""
from __future__ import annotations


class ErrorCategory:
    """Stable, serialisable error categories.

    These strings cross process and protocol boundaries, so they are part of the
    public contract and must not be renamed without a schema version bump.
    """

    #: Host or plugin configuration is wrong. Never retried, never charged.
    CONFIG = "config_error"
    #: The request itself is invalid. Never retried.
    INPUT = "input_error"
    #: The backend could not be reached at all. Safe to retry.
    NETWORK = "network_error"
    #: The backend explicitly asked us to slow down. Retryable with backoff.
    RATE_LIMITED = "rate_limited"
    #: Time ran out. The outcome may or may not be known.
    TIMEOUT = "timeout"
    #: A human or the host cancelled the run.
    CANCELLED = "cancelled"
    #: The prompt was submitted but no answer came back. **Never blind-retry.**
    OUTCOME_UNKNOWN = "outcome_unknown"
    #: The backend answered but the payload failed structural validation.
    OUTPUT_INVALID = "output_invalid"
    #: The model could not resolve the business question. A normal, expected
    #: outcome that routes to human review -- not an infrastructure failure.
    BUSINESS_AMBIGUITY = "business_ambiguity"

    ALL = (
        CONFIG,
        INPUT,
        NETWORK,
        RATE_LIMITED,
        TIMEOUT,
        CANCELLED,
        OUTCOME_UNKNOWN,
        OUTPUT_INVALID,
        BUSINESS_AMBIGUITY,
    )


#: Categories where re-running the same request can produce a different answer.
#: ``OUTCOME_UNKNOWN`` is deliberately absent: the prompt already reached the
#: backend, so a retry may double-charge the merchant.
RETRYABLE_CATEGORIES = frozenset({ErrorCategory.NETWORK, ErrorCategory.RATE_LIMITED})

#: Categories that leave the upstream execution state undecided.
UNSETTLED_CATEGORIES = frozenset({ErrorCategory.TIMEOUT, ErrorCategory.OUTCOME_UNKNOWN})


class AgentError(RuntimeError):
    """A run failure that carries its category and merchant-safe wording.

    ``detail`` is for server-side logs and must never be shown to a merchant;
    ``merchant_message`` is the only text that is allowed to reach a UI.
    """

    def __init__(self, category: str, detail: str = "", merchant_message: str = ""):
        if category not in ErrorCategory.ALL:
            raise ValueError(f"unknown error category: {category}")
        super().__init__(detail or category)
        self.category = category
        self.detail = detail
        self.merchant_message = merchant_message or _DEFAULT_MERCHANT_MESSAGES[category]

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES

    @property
    def outcome_unknown(self) -> bool:
        return self.category in UNSETTLED_CATEGORIES

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "detail": self.detail,
            "merchant_message": self.merchant_message,
            "retryable": self.retryable,
        }


class ConfigError(AgentError):
    def __init__(self, detail: str = "", merchant_message: str = ""):
        super().__init__(ErrorCategory.CONFIG, detail, merchant_message)


class InputError(AgentError):
    def __init__(self, detail: str = "", merchant_message: str = ""):
        super().__init__(ErrorCategory.INPUT, detail, merchant_message)


class NetworkError(AgentError):
    def __init__(self, detail: str = "", merchant_message: str = ""):
        super().__init__(ErrorCategory.NETWORK, detail, merchant_message)


class RateLimited(AgentError):
    def __init__(self, detail: str = "", retry_after: float = 0.0, merchant_message: str = ""):
        super().__init__(ErrorCategory.RATE_LIMITED, detail, merchant_message)
        self.retry_after = float(retry_after)


class TimeoutError_(AgentError):
    def __init__(self, detail: str = "", merchant_message: str = ""):
        super().__init__(ErrorCategory.TIMEOUT, detail, merchant_message)


class Cancelled(AgentError):
    def __init__(self, detail: str = "", merchant_message: str = ""):
        super().__init__(ErrorCategory.CANCELLED, detail, merchant_message)


class OutcomeUnknown(AgentError):
    def __init__(self, detail: str = "", merchant_message: str = ""):
        super().__init__(ErrorCategory.OUTCOME_UNKNOWN, detail, merchant_message)


class OutputInvalid(AgentError):
    def __init__(self, detail: str = "", merchant_message: str = ""):
        super().__init__(ErrorCategory.OUTPUT_INVALID, detail, merchant_message)


class BusinessAmbiguity(AgentError):
    def __init__(self, detail: str = "", merchant_message: str = ""):
        super().__init__(ErrorCategory.BUSINESS_AMBIGUITY, detail, merchant_message)


_DEFAULT_MERCHANT_MESSAGES = {
    ErrorCategory.CONFIG: "服务配置不完整，请联系客服",
    ErrorCategory.INPUT: "提交的内容无法处理，请检查后重试",
    ErrorCategory.NETWORK: "服务暂时不可用，请稍后重试",
    ErrorCategory.RATE_LIMITED: "请求太频繁，请稍后重试",
    ErrorCategory.TIMEOUT: "处理超时，本次结果未知，可在软件中重试",
    ErrorCategory.CANCELLED: "已取消",
    ErrorCategory.OUTCOME_UNKNOWN: "结果待确认，请在软件中重试",
    ErrorCategory.OUTPUT_INVALID: "处理结果格式异常，请重试或转人工处理",
    ErrorCategory.BUSINESS_AMBIGUITY: "需要人工确认",
}
