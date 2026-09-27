"""OAuth routes — mirrors apps/api/src/oauth-routes.ts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .auth import SESSION_COOKIE_NAME, SESSION_TTL_SECONDS, sign_session_token
from .routes import get_auth

router = APIRouter(prefix="/api/auth/oauth", tags=["oauth"])


async def _sign_state(provider: str, nonce: str, secret: str) -> str:
    payload = json.dumps({"provider": provider, "nonce": nonce, "iat": int(time.time() * 1000)})
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{b64}.{sig}"


async def _verify_state(state: str, expected_provider: str, secret: str) -> bool:
    dot = state.rfind(".")
    if dot < 0:
        return False
    payload_b64 = state[:dot]
    sig = state[dot + 1:]
    padding = 4 - len(payload_b64) % 4
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + "=" * padding).decode()
    except Exception:
        return False
    expected_sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return False
    parsed = json.loads(payload)
    if parsed.get("provider") != expected_provider:
        return False
    if time.time() * 1000 - parsed.get("iat", 0) > 10 * 60 * 1000:
        return False
    return True


async def _exchange_github_code(code: str, config: Any) -> str:
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            json={
                "client_id": config.oauth.github.client_id,
                "client_secret": config.oauth.github.client_secret,
                "code": code,
            },
        )
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise ValueError("github_token_exchange_failed")
    return token


async def _fetch_github_profile(access_token: str) -> dict[str, Any]:
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "Keen-AI-Platform",
                "Accept": "application/vnd.github+json",
            },
        )
    if resp.status_code != 200:
        raise ValueError("github_profile_fetch_failed")
    user = resp.json()
    return {
        "subject": str(user["id"]),
        "email": user.get("email"),
        "display_name": user.get("name") or user.get("login"),
        "avatar_url": user.get("avatar_url"),
    }


async def _exchange_google_code(code: str, config: Any) -> str:
    import httpx
    callback_url = config.oauth.callback_url or f"{config.WEB_ORIGIN}/api/auth/oauth/google/callback"
    proxies = {}
    if proxy := (config.HTTPS_PROXY or config.HTTP_PROXY):
        proxies = {"https://": proxy, "http://": proxy}
    async with httpx.AsyncClient(proxies=proxies) as client:  # type: ignore[arg-type]
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": config.oauth.google.client_id,
                "client_secret": config.oauth.google.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": callback_url,
            },
        )
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise ValueError("google_token_exchange_failed")
    return token


async def _fetch_google_profile(access_token: str, config: Any) -> dict[str, Any]:
    import httpx
    proxies = {}
    if proxy := (config.HTTPS_PROXY or config.HTTP_PROXY):
        proxies = {"https://": proxy, "http://": proxy}
    async with httpx.AsyncClient(proxies=proxies) as client:  # type: ignore[arg-type]
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        raise ValueError("google_profile_fetch_failed")
    user = resp.json()
    return {
        "subject": user["id"],
        "email": user.get("email"),
        "display_name": user.get("name"),
        "avatar_url": user.get("picture"),
    }


def _build_authorization_url(config: Any, provider: str, state: str) -> str:
    callback_url = config.oauth.callback_url or f"{config.WEB_ORIGIN}/api/auth/oauth/{provider}/callback"
    if provider == "github":
        params = {
            "client_id": config.oauth.github.client_id,
            "scope": "read:user user:email",
            "state": state,
        }
        return f"https://github.com/login/oauth/authorize?{ '&'.join(f'{k}={v}' for k,v in params.items()) }"
    params = {
        "client_id": config.oauth.google.client_id,
        "redirect_uri": callback_url,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{ '&'.join(f'{k}={v}' for k,v in params.items()) }"


@router.get("/providers")
async def oauth_providers(request: Request) -> dict[str, list[str]]:
    config = request.app.state.config
    providers: list[str] = []
    if config.oauth.github:
        providers.append("github")
    if config.oauth.google:
        providers.append("google")
    return {"data": providers}


@router.get("/{provider}")
async def oauth_authorize(provider: str, request: Request) -> RedirectResponse:
    config = request.app.state.config
    if provider not in ("github", "google"):
        raise HTTPException(status_code=400, detail={"error": "unsupported_provider"})
    provider_config = getattr(config.oauth, provider, None)
    if not provider_config:
        raise HTTPException(status_code=400, detail={"error": "provider_not_configured"})
    state_secret = config.oauth.state_secret
    if not state_secret:
        raise HTTPException(status_code=500, detail={"error": "oauth_state_secret_not_configured"})
    nonce = str(uuid.uuid4())
    state = await _sign_state(provider, nonce, state_secret)
    url = _build_authorization_url(config, provider, state)
    return RedirectResponse(url=url)


@router.get("/{provider}/callback")
async def oauth_callback(provider: str, request: Request) -> RedirectResponse:
    config = request.app.state.config
    repository = request.app.state.repository
    if provider not in ("github", "google"):
        return RedirectResponse(url=f"{config.WEB_ORIGIN}/login?error=unsupported_provider")
    provider_config = getattr(config.oauth, provider, None)
    if not provider_config:
        return RedirectResponse(url=f"{config.WEB_ORIGIN}/login?error=provider_not_configured")
    state_secret = config.oauth.state_secret
    if not state_secret:
        return RedirectResponse(url=f"{config.WEB_ORIGIN}/login?error=oauth_state_secret_not_configured")

    error = request.query_params.get("error")
    if error:
        return RedirectResponse(url=f"{config.WEB_ORIGIN}/login?error=oauth_denied")
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return RedirectResponse(url=f"{config.WEB_ORIGIN}/login?error=oauth_missing_params")

    if not await _verify_state(state, provider, state_secret):
        return RedirectResponse(url=f"{config.WEB_ORIGIN}/login?error=oauth_state_invalid")

    try:
        if provider == "github":
            access_token = await _exchange_github_code(code, config)
            profile = await _fetch_github_profile(access_token)
        else:
            access_token = await _exchange_google_code(code, config)
            profile = await _fetch_google_profile(access_token, config)

        oauth_user = await repository.find_oauth_account(provider, profile["subject"])
        if not oauth_user:
            tenant_id = config.signup_tenant_id
            created = await repository.create_oauth_user(tenant_id, {
                "provider": provider,
                "subject": profile["subject"],
                "email": profile["email"],
                "display_name": profile["display_name"],
                "avatar_url": profile["avatar_url"],
                "role": "member",
            })
            oauth_user = {
                "user_id": created["id"],
                "display_name": created["display_name"],
                "tenant_id": tenant_id,
                "role": created["role"],
            }
        elif profile.get("avatar_url"):
            await repository.update_user_avatar_url(oauth_user["user_id"], profile["avatar_url"])

        token = await sign_session_token(config, oauth_user["user_id"], oauth_user["tenant_id"], oauth_user["role"])
        from fastapi.responses import Response
        response = RedirectResponse(url=f"{config.WEB_ORIGIN}/")
        secure = config.NODE_ENV == "production"
        domain = None if secure else "localhost"
        response.set_cookie(
            SESSION_COOKIE_NAME, token,
            httponly=True, samesite="lax", path="/",
            secure=secure, max_age=SESSION_TTL_SECONDS, domain=domain,
        )
        return response
    except Exception as e:
        logger = request.app.state.logger
        err = str(e)
        logger.error("OAuth callback failed: %s", err, exc_info=True)
        return RedirectResponse(
            url=f"{config.WEB_ORIGIN}/login?error=oauth_failed&detail={err}"
        )
