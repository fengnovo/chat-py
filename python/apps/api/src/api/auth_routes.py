"""Auth routes — mirrors apps/api/src/auth-routes.ts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from .auth import SESSION_COOKIE_NAME, SESSION_TTL_SECONDS, sign_session_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class RegisterInput(BaseModel):
    username: str = Field(pattern=r"^[a-zA-Z0-9_.-]{3,64}$")
    display_name: str = Field(alias="displayName", min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=200)

    model_config = {"populate_by_name": True}


class ChangePasswordInput(BaseModel):
    current_password: str = Field(alias="currentPassword", min_length=1, max_length=200)
    new_password: str = Field(alias="newPassword", min_length=8, max_length=200)

    @field_validator("new_password")
    @classmethod
    def _must_differ(cls, v: str, info: Any) -> str:
        if hasattr(info, "data") and info.data.get("current_password") == v:
            raise ValueError("new_password_must_differ")
        return v

    model_config = {"populate_by_name": True}


def _set_session_cookie(response: Response, config: Any, token: str) -> None:
    secure = config.NODE_ENV == "production"
    domain = None if secure else "localhost"
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        path="/",
        secure=secure,
        max_age=SESSION_TTL_SECONDS,
        domain=domain,
    )


@router.post("/login")
async def login(
    input: LoginInput,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    from db import verify_password
    repository = request.app.state.repository
    config = request.app.state.config
    user = await repository.find_user_for_login(input.username)
    password_ok = await verify_password(input.password, user.get("password_hash") if user else None)
    if not user or not password_ok:
        raise HTTPException(status_code=401, detail={"error": "invalid_credentials"})
    token = await sign_session_token(config, user["id"], user["tenant_id"], user["role"])
    _set_session_cookie(response, config, token)
    return {
        "user": {
            "id": user["id"],
            "displayName": user["display_name"],
            "role": user["role"],
            "tenantId": user["tenant_id"],
        },
    }


@router.post("/register", status_code=201)
async def register(
    input: RegisterInput,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    from db import hash_password
    repository = request.app.state.repository
    config = request.app.state.config
    if config.AUTH_MODE != "password" or not config.signup_enabled:
        raise HTTPException(status_code=403, detail={"error": "signup_disabled"})
    created = await repository.create_tenant_user(config.signup_tenant_id, {
        "username": input.username,
        "display_name": input.display_name,
        "password_hash": await hash_password(input.password),
        "role": "member",
    })
    token = await sign_session_token(config, created["id"], config.signup_tenant_id, "member")
    _set_session_cookie(response, config, token)
    return {
        "user": {
            "id": created["id"],
            "displayName": created["display_name"],
            "role": "member",
            "tenantId": config.signup_tenant_id,
        },
    }


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/change-password")
async def change_password(
    input: ChangePasswordInput,
    request: Request,
) -> dict[str, bool]:
    from db import hash_password, verify_password
    config = request.app.state.config
    if config.AUTH_MODE != "password":
        raise HTTPException(status_code=403, detail={"error": "password_change_unavailable"})
    auth = request.state.auth
    repository = request.app.state.repository
    password_hash = await repository.get_user_password_hash(auth.tenant_id, auth.user_id)
    if not password_hash:
        raise HTTPException(status_code=403, detail={"error": "password_change_unavailable"})
    if not await verify_password(input.current_password, password_hash):
        raise HTTPException(status_code=401, detail={"error": "invalid_current_password"})
    updated = await repository.update_tenant_user(auth.tenant_id, auth.user_id, {
        "password_hash": await hash_password(input.new_password),
    })
    if not updated:
        raise HTTPException(status_code=404, detail={"error": "user_not_found"})
    return {"ok": True}


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    auth = request.state.auth
    repository = request.app.state.repository
    config = request.app.state.config
    display_name = await repository.get_user_display_name(auth.tenant_id, auth.user_id)
    avatar_url = await repository.get_user_avatar_url(auth.tenant_id, auth.user_id)
    password_hash = await repository.get_user_password_hash(auth.tenant_id, auth.user_id)
    return {
        "user": {
            "id": str(auth.user_id),
            "displayName": display_name or "Agent user",
            "role": auth.roles[0] if auth.roles else "member",
            "tenantId": str(auth.tenant_id),
            "authMode": config.AUTH_MODE,
            "hasPassword": password_hash is not None,
            "avatarUrl": avatar_url,
        },
    }
