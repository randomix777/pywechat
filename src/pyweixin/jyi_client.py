"""JYI production browser authorization and device credential adapter.

JYI model requests use an OpenAI-compatible gateway. Its OAuth credentials
are not the rotating access/refresh tokens used by the original product server.
Business task APIs must be adapted separately before enabling analysis.
"""
from __future__ import annotations

from typing import Any, Mapping

from .client_login import (
    CredentialStore, GatewayError, MerchantClient, Session,
    STATUS_LOGGED_IN, STATUS_WAITING_BROWSER, _session_to_dict,
)
from .oauth_browser import BrowserLoginFlow, OAuthConfig, OAuthError

JYI_GATEWAY = "https://gateway.jyisolutions.com"
JYI_AUTH_ORIGIN = "https://ai.jyisolutions.com"
BUSINESS_NOTICE = "已连接 JYI；文字分析已接入，图片/音频/视频暂未接入。"

#: 默认文字模型（网关目录里的 ID）。换默认改这里或设环境变量，
#: 不用发版 —— 但线上改之前先在小群里实测意图识别，别直接切。
DEFAULT_TEXT_MODEL = "jyi-default"


def is_jyi_gateway(url: str) -> bool:
    return url.rstrip("/") in (JYI_GATEWAY, JYI_GATEWAY + "/v1")


def production_oauth_config() -> OAuthConfig:
    return OAuthConfig(
        authorize_base=JYI_AUTH_ORIGIN,
        token_base=JYI_AUTH_ORIGIN,
        client_id="pyweixin-desktop",
        authorize_path="/connect/authorize",
        token_path="/oauth/token",
        scope="config:read",
        use_pkce=True,
    )


def _model_integer(row: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        try:
            value = int(row.get(key) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 < value <= 10_000_000:
            return value
    return 0


class JYIClient(MerchantClient):
    def __init__(self, base_url: str = JYI_GATEWAY, session: Session | None = None,
                 timeout: float = 15.0, config: OAuthConfig | None = None):
        self.config = config or production_oauth_config()
        gateway = base_url.rstrip("/").removesuffix("/v1")
        # A credential from another service must never be sent to this gateway.
        if session is not None and (
            session.provider != "jyi"
            or session.gateway_url != gateway
            or session.auth_origin != self.config.token_base.rstrip("/")
        ):
            session = None
        super().__init__(gateway, session=session, timeout=timeout)
        self.auth_client = MerchantClient(self.config.token_base, timeout=timeout)

    def login(self, credentials: CredentialStore, device_name: str, progress) -> Session:
        def exchange(**values: Any) -> Session:
            result = self.auth_client._request("POST", self.config.token_path, {
                "grant_type": "authorization_code",
                "client_id": self.config.client_id,
                **values,
            }, authenticated=False)
            if result.get("key_status") == "account_suspended":
                raise OAuthError("JYI 账号已停用，请联系客服。")
            gateway = str(result.get("base_url", "")).rstrip("/").removesuffix("/v1")
            if gateway != self.base_url:
                raise OAuthError("登录服务返回的网关地址与配置不一致，请联系客服。")
            if (result.get("key_status") != "active"
                    or not str(result.get("api_key", "")).startswith("jyi_sk_")
                    or not result.get("device_token") or not result.get("connection_id")):
                raise OAuthError("JYI 未返回有效的连接凭据，请重新登录。")
            session = Session(
                access_token=result["api_key"], refresh_token=result["device_token"],
                device_id=result["connection_id"],
                merchant_name=str(result.get("account_email") or "JYI 账号"),
                device_name=device_name, provider="jyi", gateway_url=gateway,
                auth_origin=self.config.token_base.rstrip("/"),
                subscription=self._subscription(result),
            )
            credentials.save({"session": _session_to_dict(session)})
            self.session = session
            progress(STATUS_LOGGED_IN, "已连接 JYI 生产网关")
            return session

        flow = BrowserLoginFlow(
            config=self.config, exchange=exchange,
            on_progress=lambda payload: progress(STATUS_WAITING_BROWSER, payload["detail"]),
        )
        try:
            return flow.run()
        except GatewayError as exc:
            raise OAuthError("JYI 登录服务暂不可用，请重新登录。") from exc

    def _credential(self) -> dict[str, Any]:
        if self.session is None:
            raise GatewayError("unauthenticated", "请先登录 JYI 账号")
        return self.auth_client._request("POST", "/oauth/credential", {
            "connection_id": self.session.device_id,
            "device_token": self.session.refresh_token,
        }, authenticated=False)

    def refresh(self) -> dict[str, Any]:
        # JYI keys do not expire like product access tokens. Never rotate a key
        # implicitly: it invalidates the previous credential.
        return self._credential()

    def account(self) -> dict[str, Any]:
        connection = self._credential()
        subscription = self._subscription(connection)
        if self.session is not None:
            self.session.subscription = subscription
        return {"connection": connection, "subscription": subscription,
                "message": "" if subscription.get("balance_known") else "额度未知，联网后自动刷新",
                "purchase_hint": str(connection.get("topup_url") or "请在 JYI 网站查看余额和管理连接设备。"),
                "account_email": str(connection.get("account_email") or ""),
                "account_status": str(connection.get("account_status") or "unknown"),
                "allowance_syncing": bool(connection.get("allowance_syncing"))}

    @staticmethod
    def _subscription(connection: Mapping[str, Any]) -> dict[str, Any]:
        if not connection.get("balance_known"):
            return {"balance_known": False}
        subscription: dict[str, Any] = {}
        try:
            total = int(connection["quota_micro_usd"])
            used = int(connection["used_micro_usd"])
            reserved = int(connection["reserved_micro_usd"])
            remaining = int(connection["remaining_micro_usd"])
        except (KeyError, TypeError, ValueError):
            return {"balance_known": False}
        subscription.update({
            "balance_known": True,
            "unlimited": False,
            "quota_micro_usd": total,
            "used_micro_usd": used,
            "reserved_micro_usd": reserved,
            "remaining_micro_usd": max(0, remaining),
            "spendable_credits": int(connection.get("spendable_credits", 0) or 0),
            # No subscription tier is implied by the JYI account. Leave this
            # blank unless the service explicitly returns a real plan name.
            "plan_code": str(connection.get("plan_code", "") or ""),
            "period_end": str(connection.get("period_end", "") or ""),
            "active": bool(connection.get("active", True)),
            "allowance_syncing": bool(connection.get("allowance_syncing")),
            "account_email": str(connection.get("account_email") or ""),
            "account_status": str(connection.get("account_status") or "unknown"),
            "topup_url": str(connection.get("topup_url") or ""),
        })
        return subscription

    def logout(self) -> None:
        # /oauth/revoke requires a browser cookie and CSRF, not a device token.
        # Local sign-out clears DPAPI storage in MerchantApplication.detach.
        self.session = None

    def list_models(self) -> dict[str, Any]:
        body = self._request("GET", "/v1/models")
        models = []
        for row in body.get("data", []):
            if not isinstance(row, Mapping) or not row.get("id"):
                continue
            model: dict[str, Any] = {
                "alias": str(row["id"]),
                "display_name": str(row.get("name") or row["id"]),
            }
            # Preserve only public capability metadata. OpenAI-compatible
            # model lists vary in field names, so accept common variants and
            # leave missing limits unknown instead of inventing defaults.
            context = _model_integer(row, "context_window", "context_length", "max_context_tokens")
            output = _model_integer(row, "max_output_tokens", "max_completion_tokens", "max_tokens")
            if context:
                model["context_window"] = context
            if output:
                model["max_output_tokens"] = output
            modalities = row.get("input_modalities", row.get("modalities"))
            if isinstance(modalities, (list, tuple)):
                model["input_modalities"] = [str(value).lower() for value in modalities
                                              if str(value).lower() in {"text", "image", "audio", "video"}]
            models.append(model)
        return {"catalog": {"models": models, "free_available": False,
                            "notice": BUSINESS_NOTICE}}

    def text_model(self) -> str:
        """本次文字任务用的模型。环境变量优先，方便切模型实测。"""
        import os as _os

        return (_os.environ.get("PYWEIXIN_TEXT_MODEL", "") or "").strip() \
            or DEFAULT_TEXT_MODEL

    def chat_completions(self, model: str, messages: list[dict[str, Any]],
                         timeout: float = 120.0, **kwargs: Any) -> dict[str, Any]:
        """调 JYI 的 OpenAI 兼容 chat 接口。返回原始 body。

        钥匙用 JYI 登录态（session.access_token），不读任何本地密钥 ——
        LLM 走云的规定在这里落地。调用方自己组提示词、自己校验输出，
        网关只管执行（透传上游）。
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
        }
        payload.update(kwargs)
        # OpenCode Go requires a stable opaque session identifier. Derive it
        # from the conversation input so retries of the same request retain
        # the same routing/cache identity without exposing message contents.
        import hashlib
        import json
        import uuid

        session_material = json.dumps(
            {"model": model, "messages": messages},
            ensure_ascii=False, sort_keys=True, default=str,
            separators=(",", ":"),
        ).encode("utf-8")
        session_digest = hashlib.sha256(session_material).digest()[:16]
        conversation_id = str(uuid.UUID(bytes=session_digest))
        client = MerchantClient(self.base_url, session=self.session,
                                timeout=timeout)
        return client._request(
            "POST", "/v1/chat/completions", payload,
            extra_headers={"x-opencode-session": conversation_id},
        )

    def chat_completions_stream(self, model: str, messages: list[dict[str, Any]],
                                timeout: float = 120.0, on_chunk: Any = None,
                                **kwargs: Any) -> dict[str, Any]:
        """Stream one completion while retaining the normal chat response shape."""
        import hashlib
        import json
        import uuid

        payload: dict[str, Any] = {"model": model, "messages": messages,
                                   "temperature": 0, "stream": True}
        payload.update(kwargs)
        material = json.dumps({"model": model, "messages": messages},
                              ensure_ascii=False, sort_keys=True, default=str,
                              separators=(",", ":")).encode("utf-8")
        conversation_id = str(uuid.UUID(bytes=hashlib.sha256(material).digest()[:16]))
        client = MerchantClient(self.base_url, session=self.session, timeout=timeout)
        return client._request_stream(
            "POST", "/v1/chat/completions", payload,
            extra_headers={"x-opencode-session": conversation_id}, on_chunk=on_chunk)

    def refresh_models(self) -> dict[str, Any]:
        return self.list_models()

    def submit_parse(self, request: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        raise GatewayError("business_api_unavailable", BUSINESS_NOTICE)

    def submit_summary(self, request: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        raise GatewayError("business_api_unavailable", BUSINESS_NOTICE)


def create_merchant_client(base_url: str, session: Session | None = None) -> MerchantClient:
    if is_jyi_gateway(base_url):
        return JYIClient(base_url, session=session)
    # Do not send a JYI key to a development/product gateway either.
    if session is not None and session.provider == "jyi":
        session = None
    return MerchantClient(base_url, session=session)
