"""Admin routes — mirrors apps/api/src/admin-routes.ts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .auth import AuthenticationError, ForbiddenError, require_admin
from .routes import get_auth

router = APIRouter(prefix="/api/admin", tags=["admin"])


class CreateUserInput(BaseModel):
    username: str = Field(pattern=r"^[a-zA-Z0-9_.-]{3,64}$")
    display_name: str = Field(alias="displayName", min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=200)
    role: str = Field(pattern=r"^(admin|owner|member)$")

    model_config = {"populate_by_name": True}


class UpdateUserInput(BaseModel):
    role: str | None = Field(default=None, pattern=r"^(admin|owner|member)$")
    display_name: str | None = Field(default=None, alias="displayName", min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=8, max_length=200)

    model_config = {"populate_by_name": True}


class KnowledgeBaseGrantsInput(BaseModel):
    knowledge_base_ids: list[str] = Field(alias="knowledgeBaseIds")

    model_config = {"populate_by_name": True}


@router.get("/users")
async def list_users(request: Request, auth=Depends(get_auth)) -> dict[str, Any]:
    require_admin(auth)
    repository = request.app.state.repository
    return {"data": await repository.list_tenant_users(auth.tenant_id)}


@router.post("/users", status_code=201)
async def create_user(
    input: CreateUserInput,
    request: Request,
    auth=Depends(get_auth),
) -> Any:
    from db import hash_password
    require_admin(auth)
    repository = request.app.state.repository
    user = await repository.create_tenant_user(auth.tenant_id, {
        "username": input.username,
        "display_name": input.display_name,
        "password_hash": await hash_password(input.password),
        "role": input.role,
    })
    return user


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    input: UpdateUserInput,
    request: Request,
    auth=Depends(get_auth),
) -> Any:
    from db import hash_password
    require_admin(auth)
    repository = request.app.state.repository
    update: dict[str, Any] = {}
    if input.role:
        update["role"] = input.role
    if input.display_name:
        update["display_name"] = input.display_name
    if input.password:
        update["password_hash"] = await hash_password(input.password)
    if not update:
        raise HTTPException(status_code=400, detail={"error": "empty_patch"})
    user = await repository.update_tenant_user(auth.tenant_id, user_id, update)
    if not user:
        raise HTTPException(status_code=404, detail={"error": "user_not_found"})
    return user


@router.get("/users/{user_id}/knowledge-bases")
async def list_user_kb_grants(
    user_id: str,
    request: Request,
    auth=Depends(get_auth),
) -> dict[str, Any]:
    require_admin(auth)
    repository = request.app.state.repository
    return {"data": await repository.list_knowledge_base_grants(auth.tenant_id, user_id)}


@router.put("/users/{user_id}/knowledge-bases")
async def update_user_kb_grants(
    user_id: str,
    input: KnowledgeBaseGrantsInput,
    request: Request,
    auth=Depends(get_auth),
) -> dict[str, Any]:
    require_admin(auth)
    repository = request.app.state.repository
    try:
        granted = await repository.replace_knowledge_base_grants(
            auth.tenant_id, user_id, input.knowledge_base_ids, str(auth.user_id),
        )
        return {"data": granted}
    except Exception as e:
        if hasattr(e, "resource"):
            raise HTTPException(status_code=404, detail={"error": f"{e.resource}_not_found"})
        raise


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    request: Request,
    auth=Depends(get_auth),
) -> None:
    require_admin(auth)
    if user_id == str(auth.user_id):
        raise HTTPException(status_code=409, detail={"error": "cannot_delete_self"})
    repository = request.app.state.repository
    artifacts = request.app.state.artifacts
    try:
        result = await repository.delete_user(auth.tenant_id, user_id)
    except Exception as e:
        if getattr(e, "code", None):
            raise HTTPException(status_code=409, detail={"error": e.code})
        raise
    if not result:
        raise HTTPException(status_code=404, detail={"error": "user_not_found"})
    if result and "deleted_artifact_keys" in result:
        import asyncio
        await asyncio.gather(*[artifacts.delete_object(k) for k in result["deleted_artifact_keys"]], return_exceptions=True)
