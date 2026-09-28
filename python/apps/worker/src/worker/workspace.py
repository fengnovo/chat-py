"""沙箱工作区准备与 Agent 资源上传 — 镜像 apps/worker/src/workspace.ts。

提供 prepare_workspace、upload_agent_resources 以及路径安全工具函数。
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from deepagents.backends.protocol import SandboxBackendProtocol
from pydantic import BaseModel, Field


class _ProjectManifest(BaseModel):
    version: int = 1
    files: list[_ProjectFile] = Field(min_length=1, max_length=1_000)


class _ProjectFile(BaseModel):
    path: str = Field(min_length=1, max_length=1_000)
    content_base64: str = Field(alias="contentBase64", min_length=1)


# deepagents 0.7 的 SandboxBackendProtocol：sync execute/upload_files 仅满足 ABC 契约，
# Docker/E2B 实现里真实逻辑都在 async aexecute/aupload_files 上。
RemoteWorkspaceSandbox = SandboxBackendProtocol


class AgentResources:
    """已上传的 Agent 资源路径集合。"""

    def __init__(self, *, skills: list[str], memory: list[str]) -> None:
        self.skills = skills
        self.memory = memory


def safe_relative_path(relative_path: str) -> str:
    """拒绝绝对路径与 .. 穿越；允许带子目录的文件名。"""
    normalized = relative_path.replace("\\", "/")
    if (
        normalized.startswith("/")
        or "\x00" in normalized
        or any(seg == ".." or seg == "" for seg in normalized.split("/"))
    ):
        raise ValueError(f"Uploaded file path is unsafe: {relative_path}")
    return normalized


def remote_workspace_path(configured_path: str) -> str:
    if not configured_path.startswith("/") or "\x00" in configured_path:
        raise ValueError("Sandbox workspace path must be absolute")
    return str(PurePosixPath("/") / PurePosixPath(configured_path))


async def _checked_execute(sandbox: RemoteWorkspaceSandbox, command: str) -> str:
    result = await sandbox.aexecute(command)
    exit_code = result.exit_code
    if exit_code not in (0, None):
        raise RuntimeError(result.output.strip() or f"Command failed: {command}")
    return result.output


def _shell_quote(value: str) -> str:
    return f"'{value.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"


# 镜像内预装的离线 React/Vite 依赖，沙箱无网络时唯一的依赖来源。
_OFFLINE_WEB_RUNTIME = "/opt/chat-web-runtime/node_modules"


async def _link_offline_web_runtime(
    sandbox: RemoteWorkspaceSandbox,
    workspace: str,
) -> None:
    """把离线 Web 运行时接到工作区，让子目录里的项目能向上解析到依赖。

    必须建真实目录再逐包软链：整体软链会让 vite 无法写 node_modules/.vite-temp，
    构建会以 ENOENT 失败（这正是 Agent 之前反复重写文件的原因之一）。
    已存在则跳过，避免覆盖 Agent 自己准备的依赖。
    """
    if not workspace.startswith("/"):
        return
    node_modules = f"{workspace}/node_modules"
    script = (
        f"if [ -d {_shell_quote(_OFFLINE_WEB_RUNTIME)} ] && [ ! -e {_shell_quote(node_modules)} ]; then\n"
        f"  mkdir -p {_shell_quote(node_modules)}/.bin &&\n"
        f"  for p in {_shell_quote(_OFFLINE_WEB_RUNTIME)}/*; do [ -e \"$p\" ] || continue; "
        f"ln -sfn \"$p\" {_shell_quote(node_modules)}/\"$(basename \"$p\")\"; done &&\n"
        f"  for b in {_shell_quote(_OFFLINE_WEB_RUNTIME)}/.bin/*; do [ -e \"$b\" ] || continue; "
        f"ln -sfn \"$b\" {_shell_quote(node_modules)}/.bin/\"$(basename \"$b\")\"; done\n"
        f"fi"
    )
    await sandbox.aexecute(script)


async def prepare_workspace(
    sandbox: RemoteWorkspaceSandbox,
    workspace: str,
    source: dict[str, Any] | None,
    load_upload: Any,
    initialize_source: bool,
) -> str:
    """准备沙箱工作区：创建目录、链接离线依赖、恢复项目源码。"""
    await _checked_execute(sandbox, f"mkdir -p -- {_shell_quote(workspace)}")
    await _link_offline_web_runtime(sandbox, workspace)
    if not initialize_source or not source or source.get("type") == "empty":
        return workspace

    listing = await _checked_execute(
        sandbox,
        f"if [ -z \"$(ls -A -- {_shell_quote(workspace)})\" ]; then printf empty; else printf populated; fi",
    )
    if listing.strip() != "empty":
        return workspace

    if source.get("type") == "git":
        url = source["url"]
        ref = source.get("ref")
        branch = f" --branch {_shell_quote(ref)}" if ref else ""
        await _checked_execute(
            sandbox,
            f"git -c credential.helper= -c core.askPass= clone --depth 1 --single-branch{branch} "
            f"-- {_shell_quote(url)} {_shell_quote(f'{workspace}/.restore')}"
            f" && cp -a -- {_shell_quote(f'{workspace}/.restore/.')} {_shell_quote(workspace)}"
            f" && rm -rf -- {_shell_quote(f'{workspace}/.restore')}",
        )
        return workspace

    # type == "upload"
    object_key = source["objectKey"]
    snapshot_bytes = await load_upload(object_key)
    manifest = _ProjectManifest.model_validate(json.loads(snapshot_bytes.decode("utf-8")))
    files: list[tuple[str, bytes]] = []
    for file in manifest.files:
        target = PurePosixPath(workspace) / safe_relative_path(file.path)
        files.append(
            (str(target), __import__("base64").b64decode(file.content_base64))
        )
    directories = list({str(PurePosixPath(p).parent) for p, _ in files})
    if directories:
        await _checked_execute(
            sandbox, f"mkdir -p -- {' '.join(_shell_quote(d) for d in directories)}"
        )
    results = await sandbox.aupload_files(files)
    for result in results:
        if result.error:
            raise RuntimeError(
                f"Upload restore failed for {result.path}: {result.error}"
            )
    return workspace


async def upload_agent_resources(
    sandbox: RemoteWorkspaceSandbox,
    workspace: str,
    memory_host_file: str | None,
    skills_host_dir: str | None,
) -> AgentResources:
    """把宿主机的 DeepAgents memory/skills 文件上传到沙箱。

    与 CLI 的 uploadAgentResources 逻辑一致：memory 上传到 .deepagents/AGENTS.md，
    skills 上传到 .deepagents/skills 子目录下的 SKILL.md。
    """
    files: list[tuple[str, bytes]] = []
    memory_path = str(PurePosixPath(workspace) / ".deepagents" / "AGENTS.md")
    if memory_host_file and os.path.exists(memory_host_file):
        files.append((memory_path, Path(memory_host_file).read_bytes()))

    skill_root = str(PurePosixPath(workspace) / ".deepagents" / "skills")
    if skills_host_dir and os.path.exists(skills_host_dir):
        for entry in os.listdir(skills_host_dir):
            skill_dir = Path(skills_host_dir) / entry
            if not skill_dir.is_dir():
                continue
            source = skill_dir / "SKILL.md"
            if not source.exists():
                continue
            files.append(
                (str(PurePosixPath(skill_root) / entry / "SKILL.md"), source.read_bytes())
            )

    if files:
        directories = list({str(PurePosixPath(p).parent) for p, _ in files})
        await _checked_execute(
            sandbox, f"mkdir -p -- {' '.join(_shell_quote(d) for d in directories)}"
        )
        uploaded = await sandbox.aupload_files(files)
        for result in uploaded:
            if result.error:
                raise RuntimeError(
                    f"无法上传 Agent 配置：{result.path} ({result.error})"
                )

    return AgentResources(
        skills=[skill_root]
        if any(p.startswith(f"{skill_root}/") for p, _ in files)
        else [],
        memory=[memory_path]
        if any(p == memory_path for p, _ in files)
        else [],
    )
