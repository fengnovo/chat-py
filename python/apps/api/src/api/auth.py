"""Authentication — mirrors apps/api/src/auth.ts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from contracts import AuthContext
from jose import JWTError, jwt

from .config import ApiConfig

SESSION_COOKIE_NAME = "agent_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


class AuthenticationError(Exception):
    def __init__(self, message: str = "Authentication required") -> None:
        super().__init__(message)
        self.status_code = 401


class ForbiddenError(Exception):
    def __init__(self, message: str = "Forbidden") -> None:
        super().__init__(message)
        self.status_code = 403


def require_admin(auth: AuthContext) -> None:
    if "admin" not in auth.roles:
        raise ForbiddenError("Admin role required")


def _extract_token(
    authorization: str | None = None,
    cookie: str | None = None,
) -> str | None:
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:]
    if cookie:
        for pair in cookie.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, _, value = pair.partition("=")
                if name.strip() == SESSION_COOKIE_NAME:
                    return value.strip()
    return None


async def create_authenticator(
    config: ApiConfig,
    load_membership: "Callable[[str, str], Coroutine[Any, Any, str | None]] | None" = None,
) -> "Callable[..., Coroutine[Any, Any, AuthContext]]":
    """Returns an async authenticate function."""
    if config.AUTH_MODE == "dev":
        async def authenticate_dev(**kwargs: str | None) -> AuthContext:
            return AuthContext(
                userId=uuid.UUID(config.DEV_USER_ID),
                tenantId=uuid.UUID(config.DEV_TENANT_ID),
                roles=["owner"],
            )
        return authenticate_dev

    if config.AUTH_MODE == "password":
        secret = config.AUTH_JWT_SECRET
        if not secret:
            raise ValueError("AUTH_JWT_SECRET is required when AUTH_MODE=password")

        async def authenticate_password(
            authorization: str | None = None,
            cookie: str | None = None,
        ) -> AuthContext:
            token = _extract_token(authorization, cookie)
            if not token:
                raise AuthenticationError()
            try:
                payload = jwt.decode(token, secret, algorithms=["HS256"])
            except JWTError:
                raise AuthenticationError() from None

            subject = payload.get("sub")
            tenant_id = payload.get("tenant_id")
            if not isinstance(subject, str) or not isinstance(tenant_id, str):
                raise AuthenticationError("Token sub and tenant_id must be internal UUID identifiers")
            try:
                user_uuid = uuid.UUID(subject)
                tenant_uuid = uuid.UUID(tenant_id)
            except ValueError:
                raise AuthenticationError("Token sub and tenant_id must be internal UUID identifiers") from None

            role = await load_membership(tenant_id, subject) if load_membership else None
            if not role:
                raise AuthenticationError("Membership not found")
            return AuthContext(userId=user_uuid, tenantId=tenant_uuid, roles=[role])

        return authenticate_password

    # OIDC mode
    issuer = config.OIDC_ISSUER
    audience = config.OIDC_AUDIENCE
    jwks_url = config.OIDC_JWKS_URL
    if not issuer or not audience or not jwks_url:
        raise ValueError("OIDC configuration is incomplete")

    import httpx

    async def authenticate_oidc(
        authorization: str | None = None,
        cookie: str | None = None,
    ) -> AuthContext:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError()
        token = authorization[7:]
        # Fetch JWKS
        async with httpx.AsyncClient() as client:
            jwks_response = await client.get(jwks_url)
            jwks_response.raise_for_status()
            jwks = jwks_response.json()
        try:
            payload = jwt.decode(
                token,
                jwks,
                algorithms=["RS256", "ES256"],
                issuer=issuer,
                audience=audience,
            )
        except JWTError as e:
            raise AuthenticationError() from e

        subject = payload.get("sub")
        tenant_id = payload.get("tenant_id")
        if not isinstance(subject, str) or not isinstance(tenant_id, str):
            raise AuthenticationError("Token sub and tenant_id must be internal UUID identifiers")
        try:
            user_uuid = uuid.UUID(subject)
            tenant_uuid = uuid.UUID(tenant_id)
        except ValueError:
            raise AuthenticationError("Token sub and tenant_id must be internal UUID identifiers") from None

        allowed_roles = {"owner", "admin", "member"}
        roles_raw = payload.get("roles", [])
        roles = [r for r in roles_raw if isinstance(r, str) and r in allowed_roles] if isinstance(roles_raw, list) else []
        return AuthContext(
            userId=user_uuid,
            tenantId=tenant_uuid,
            roles=roles if roles else ["member"],
        )

    return authenticate_oidc


async def sign_session_token(config: ApiConfig, user_id: str, tenant_id: str, role: str) -> str:
    secret = config.AUTH_JWT_SECRET
    if not secret:
        raise ValueError("AUTH_JWT_SECRET is required when AUTH_MODE=password")
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "roles": [role],
        "iat": now,
        "exp": now + timedelta(seconds=SESSION_TTL_SECONDS),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


# Type annotations for callable
from collections.abc import Callable, Coroutine  # noqa: E402
from typing import Any  # noqa: E402
