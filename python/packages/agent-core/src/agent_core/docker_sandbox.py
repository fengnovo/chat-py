"""普通本地 Docker 容器沙箱。

对应 TS 的 DockerSandboxBackend：每次 execute 启动一个短生命周期容器，
通过受限 bind mount 复用本轮工作区。容器无网络、只读根文件系统、
丢弃全部 capability，且不能访问宿主 Docker socket。

切换到 deepagents 0.7 后，本类直接继承 `deepagents.backends.sandbox.BaseSandbox`。
Docker 沙箱用 asyncio.create_subprocess_exec 起容器，是 async 路径：
- sync `execute` / `upload_files` / `download_files` raise RuntimeError（仅满足 ABC 契约）
- override async `aexecute` / `aupload_files` / `adownload_files`，承载真实 async 逻辑
- BaseSandbox 的 `als/aread/awrite/aedit/agrep/aglob` 默认实现会调上述 async 方法
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from .types import FileOperationError

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "chat-agent-sandbox:latest"
DEFAULT_TIMEOUT_MS = 180_000
DEFAULT_MAX_OUTPUT_BYTES = 100_000
MAX_COMMAND_LENGTH = 30_000
MAX_OUTPUT_FILES = 20
MAX_OUTPUT_TREE_ENTRIES = 2_000
MAX_OUTPUT_TREE_DEPTH = 20
MAX_OUTPUT_TOTAL_BYTES = 250 * 1024 * 1024
MAX_PREVIEW_COUNT = 5
MAX_PREVIEW_FILES = 2_000
MAX_PREVIEW_BYTES = 100 * 1024 * 1024
CONTAINER_USER = "65532:65532"
IMAGE_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$")

# 沙箱内不可被 shell 命令修改的关键路径前缀（容器内视角）。
# 用于对破坏性命令做前置告警（纵深防御；真正的拦截由只读 bind mount 保证）。
PROTECTED_CONTAINER_PATHS: tuple[str, ...] = (
    "/mnt/user-data",
    "/mnt/user-data/workspace",
    "/mnt/user-data/outputs",
    "/mnt/user-data/previews",
    "/skills",
    "/mnt/skills",
    "/large_tool_results",
)


def _detect_dangerous_command(command: str) -> str | None:
    """检测 shell 命令是否包含针对关键路径的破坏性操作（rm / mv / chmod / chown）。

    仅用于审计日志，不阻断执行——内核级只读挂载才是真正的安全边界。
    """
    # 按管道/分号/逻辑运算符拆分命令片段，逐段检测
    for segment in re.split(r"[|;&]", command):
        trimmed = segment.strip()
        if not trimmed:
            continue
        tokens = trimmed.split()
        cmd = tokens[0]
        # 支持 sudo 前缀（虽然容器内无 sudo，但防御应覆盖）
        effective_cmd = tokens[1] if cmd == "sudo" and len(tokens) > 1 else cmd
        if effective_cmd not in ("rm", "mv", "chmod", "chown"):
            continue
        for token in tokens:
            for protected in PROTECTED_CONTAINER_PATHS:
                if token == protected or token.startswith(f"{protected}/"):
                    return f"{effective_cmd} {token}"
    return None


# 默认沙箱会话根；位于仓库 data 目录下，已被 .gitignore 忽略。
# 本文件位于 python/packages/agent-core/src/agent_core/，parents[5] 即仓库根。
_REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_SESSIONS_ROOT = str(_REPO_ROOT / "data" / "sandboxes")

# 沙箱可用的工作区根目录，也是模型看到的容器内工作目录。
DOCKER_SANDBOX_WORKSPACE = "/mnt/user-data/workspace"


class SandboxSkillFile(BaseModel):
    relative_path: str = Field(alias="relativePath")
    content: bytes | str

    model_config = ConfigDict(populate_by_name=True)


class SandboxSkill(BaseModel):
    """需要注入沙箱 /skills 目录的技能包。"""

    name: str
    files: list[SandboxSkillFile]


class DockerSandboxOptions(BaseModel):
    session_id: str | None = Field(default=None, alias="sessionId")
    root_directory: str | None = Field(default=None, alias="rootDirectory")
    image: str | None = None
    command_timeout_ms: int | None = Field(default=None, alias="commandTimeoutMs")
    max_output_bytes: int | None = Field(default=None, alias="maxOutputBytes")

    model_config = ConfigDict(populate_by_name=True)


class SandboxOutputFile(BaseModel):
    """Docker 沙箱中由 Agent 写入 outputs 目录的候选产物。"""

    absolute_path: str = Field(alias="absolutePath")
    relative_path: str = Field(alias="relativePath")
    name: str
    size: int

    model_config = ConfigDict(populate_by_name=True)


class SandboxPreviewDirectory(BaseModel):
    """previews 下一个包含 index.html 的可发布静态网站目录。"""

    absolute_path: str = Field(alias="absolutePath")
    name: str
    file_count: int = Field(alias="fileCount")
    size: int

    model_config = ConfigDict(populate_by_name=True)


def _is_within(root: str, candidate: str) -> bool:
    """判断规范化后的宿主路径是否仍位于本轮受信根目录内。"""
    path_from_root = os.path.relpath(candidate, root)
    return path_from_root == "." or (
        not path_from_root.startswith(f"..{os.sep}") and path_from_root != ".."
    )


class _PathError(Exception):
    """携带类 errno 代码的路径错误，映射到 FileOperationError。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _to_file_error(error: BaseException) -> FileOperationError:
    code = getattr(error, "code", None)
    if code in ("ENOENT", "EISDIR", "EACCES", "EPERM", "EINVAL"):
        if code == "ENOENT":
            return "file_not_found"
        if code == "EISDIR":
            return "is_directory"
        if code in ("EACCES", "EPERM"):
            return "permission_denied"
        return "invalid_path"
    if isinstance(error, FileNotFoundError):
        return "file_not_found"
    if isinstance(error, IsADirectoryError):
        return "is_directory"
    if isinstance(error, PermissionError):
        return "permission_denied"
    return "invalid_path"


def _append_bounded(
    current: list[bytes],
    chunk: bytes,
    state: dict[str, Any],
    limit: int,
) -> None:
    if state["bytes"] >= limit:
        state["truncated"] = True
        return
    remaining = limit - state["bytes"]
    accepted = chunk[:remaining]
    current.append(accepted)
    state["bytes"] += len(accepted)
    if len(accepted) < len(chunk):
        state["truncated"] = True


class DockerSandboxBackend(BaseSandbox):
    """DeepAgent 看到的工作区根是 /mnt/user-data；它与仓库源码目录没有任何映射关系。"""

    # deepagents.BaseSandbox 把 `id` 声明为 abstract property；这里用类级属性
    # 覆盖以解除抽象（运行时由 __init__ 写入实例值）。
    id: str = ""

    def __init__(self, options: DockerSandboxOptions) -> None:
        # sessionId 只用于生成服务端目录和容器名称，不允许成为任意宿主路径。
        normalized_session_id = re.sub(
            r"[^A-Za-z0-9_-]", "-", options.session_id or uuid.uuid4().hex
        )[:120]
        session_id = normalized_session_id or uuid.uuid4().hex
        sessions_root = os.path.realpath(options.root_directory or DEFAULT_SESSIONS_ROOT)

        self.id = f"docker-{session_id}"
        self.root_dir = "/mnt/user-data"
        self._session_root = os.path.join(sessions_root, session_id)
        # 只有 user-data 会作为可写 bind mount 暴露给容器。仓库、.env 和进程 cwd
        # 从未出现在 docker run 的挂载参数中。
        self._user_data_root = os.path.join(self._session_root, "user-data")
        self._workspace_root = os.path.join(self._user_data_root, "workspace")
        self._outputs_root = os.path.join(self._user_data_root, "outputs")
        self._previews_root = os.path.join(self._user_data_root, "previews")
        self._large_results_root = os.path.join(self._user_data_root, "large-tool-results")
        self._skills_root = os.path.join(self._session_root, "skills")
        self._image = (options.image or "").strip() or DEFAULT_IMAGE
        if not IMAGE_REFERENCE_PATTERN.match(self._image):
            raise ValueError(f"Docker 沙箱镜像名称无效：{self._image}")
        self._command_timeout_ms = min(
            max(options.command_timeout_ms or DEFAULT_TIMEOUT_MS, 1_000), 600_000
        )
        self._max_output_bytes = min(
            max(options.max_output_bytes or DEFAULT_MAX_OUTPUT_BYTES, 1_024), 1_000_000
        )
        self._closed = False

    @classmethod
    async def create(
        cls,
        options: DockerSandboxOptions,
        skills: list[SandboxSkill] | None = None,
    ) -> "DockerSandboxBackend":
        backend = cls(options)
        try:
            await backend._initialize(skills or [])
            return backend
        except Exception:
            try:
                await backend.destroy()
            except Exception:
                pass
            raise

    async def _initialize(self, skills: list[SandboxSkill]) -> None:
        # 目录按"工作源码 / 下载产物 / 静态预览 / 大工具结果"分区，方便发布器
        # 只扫描明确出口，而不是遍历整个模型工作区。
        def _mkdirs() -> None:
            for path, mode in (
                (self._workspace_root, 0o777),
                (self._outputs_root, 0o777),
                (self._previews_root, 0o777),
                (self._large_results_root, 0o777),
                (self._skills_root, 0o755),
            ):
                os.makedirs(path, mode=mode, exist_ok=True)
            # Docker Desktop 会保留 bind mount 的权限；工作区允许容器内的非特权用户写入。
            for path in (
                self._user_data_root,
                self._workspace_root,
                self._outputs_root,
                self._previews_root,
                self._large_results_root,
            ):
                os.chmod(path, 0o777)

            for skill in skills:
                skill_root = os.path.join(self._skills_root, skill.name)
                os.makedirs(skill_root, mode=0o755, exist_ok=True)
                for file in skill.files:
                    target = os.path.realpath(os.path.join(skill_root, file.relative_path))
                    if not _is_within(skill_root, target):
                        raise ValueError(f"Skill 文件路径越界：{file.relative_path}")
                    os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
                    data = (
                        file.content.encode("utf-8")
                        if isinstance(file.content, str)
                        else bytes(file.content)
                    )
                    with open(target, "wb") as handle:
                        handle.write(data)
                    os.chmod(target, 0o444)

        await asyncio.to_thread(_mkdirs)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """sync execute 不可用：Docker 路径走 asyncio.create_subprocess_exec，请用 aexecute。"""
        raise RuntimeError(
            "DockerSandboxBackend.execute (sync) 不可用：Docker 路径仅支持 async，请用 aexecute"
        )

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if self._closed:
            return ExecuteResponse(output="Docker 沙箱已经关闭。", exit_code=1, truncated=False)
        if not command.strip():
            return ExecuteResponse(output="命令不能为空。", exit_code=2, truncated=False)
        if len(command) > MAX_COMMAND_LENGTH:
            return ExecuteResponse(
                output=f"命令长度超过 {MAX_COMMAND_LENGTH} 个字符。",
                exit_code=2,
                truncated=False,
            )

        # 纵深防御：检测针对关键路径的破坏性命令并记录告警。
        # 真正的拦截由 /mnt/user-data 的只读 bind mount 保证，这里仅用于审计日志。
        dangerous_hit = _detect_dangerous_command(command)
        if dangerous_hit:
            logger.warning(
                "[DockerSandbox] 检测到破坏性命令（沙箱 %s）: %s ← %s",
                self.id,
                dangerous_hit,
                command[:200],
            )

        container_name = f"{self.id}-{uuid.uuid4().hex[:8]}".lower()[:63]
        # 每个 execute 创建一个新容器；所有容器只通过本轮 user-data 共享状态。
        # 这样不会保留后台进程，同时仍允许"写源码 → 构建 → 收集产物"的多步任务。
        args = [
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--ulimit",
            "fsize=104857600:104857600",
            "--memory",
            "768m",
            "--cpus",
            "1.5",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m",
            "--user",
            CONTAINER_USER,
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--workdir",
            DOCKER_SANDBOX_WORKSPACE,
            # /mnt/user-data 父目录只读：禁止 mv/rm 等对目录结构本身的修改。
            # 各子目录通过独立的可写 bind mount 覆盖，文件读写不受影响。
            "--mount",
            f"type=bind,src={self._user_data_root},dst=/mnt/user-data,ro",
            "--mount",
            f"type=bind,src={self._workspace_root},dst=/mnt/user-data/workspace",
            "--mount",
            f"type=bind,src={self._outputs_root},dst=/mnt/user-data/outputs",
            "--mount",
            f"type=bind,src={self._previews_root},dst=/mnt/user-data/previews",
            "--mount",
            f"type=bind,src={self._skills_root},dst=/skills,readonly",
            "--mount",
            f"type=bind,src={self._skills_root},dst=/mnt/skills/public,readonly",
            "--mount",
            f"type=bind,src={self._large_results_root},dst=/large_tool_results",
            self._image,
            "/bin/bash",
            "-lc",
            command,
        ]

        env = {
            key: value
            for key, value in {
                "PATH": os.environ.get("PATH"),
                "DOCKER_HOST": os.environ.get("DOCKER_HOST"),
                "DOCKER_CONTEXT": os.environ.get("DOCKER_CONTEXT"),
            }.items()
            if value is not None
        }

        output: list[bytes] = []
        output_state: dict[str, Any] = {"bytes": 0, "truncated": False}
        timed_out = False
        spawn_error: BaseException | None = None

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            # docker 可执行文件缺失等 spawn 失败：与 TS 的 error 事件对齐，exitCode=127。
            spawn_error = error
            proc = None

        if proc is not None:

            async def _drain(stream: asyncio.StreamReader | None) -> None:
                if stream is None:
                    return
                while True:
                    chunk = await stream.read(65_536)
                    if not chunk:
                        break
                    _append_bounded(output, chunk, output_state, self._max_output_bytes)

            readers = [
                asyncio.ensure_future(_drain(proc.stdout)),
                asyncio.ensure_future(_drain(proc.stderr)),
            ]
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._command_timeout_ms / 1000)
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                await proc.wait()

                async def _force_remove() -> None:
                    cleanup = await asyncio.create_subprocess_exec(
                        "docker",
                        "rm",
                        "--force",
                        container_name,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await cleanup.wait()

                asyncio.ensure_future(_force_remove())
            await asyncio.gather(*readers, return_exceptions=True)
            exit_code = proc.returncode
        else:
            exit_code = 127

        text = b"".join(output).decode("utf-8", errors="replace")
        if spawn_error is not None:
            text += ("\n" if text else "") + str(spawn_error)
        if timed_out:
            text += ("\n" if text else "") + f"命令执行超过 {self._command_timeout_ms}ms，已终止容器。"
        return ExecuteResponse(
            output=text or ("命令执行成功。" if exit_code == 0 else "命令执行失败。"),
            exit_code=124 if timed_out else exit_code,
            truncated=bool(output_state["truncated"]),
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """sync upload_files 不可用：Docker 路径仅支持 async，请用 aupload_files。"""
        raise RuntimeError(
            "DockerSandboxBackend.upload_files (sync) 不可用：Docker 路径仅支持 async，请用 aupload_files"
        )

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        # BaseSandbox 的 write/edit 最终会走这个适配器。这里写的是服务端创建的
        # 会话暂存目录，不是调用进程 cwd，更不会接受任意宿主绝对路径。
        results: list[FileUploadResponse] = []
        for file_path, content in files:
            try:
                target = await self._resolve_host_path(file_path, True)

                def _write() -> None:
                    os.makedirs(os.path.dirname(target), mode=0o777, exist_ok=True)
                    with open(target, "wb") as handle:
                        handle.write(bytes(content))
                    os.chmod(target, 0o666)

                await asyncio.to_thread(_write)
                results.append(FileUploadResponse(path=file_path, error=None))
            except Exception as error:
                results.append(FileUploadResponse(path=file_path, error=_to_file_error(error)))
        return results

    async def assert_writable_path(self, file_path: str) -> None:
        """在进入 BaseSandbox 工具前校验受限写入路径。"""
        await self._resolve_host_path(file_path, True)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """sync download_files 不可用：Docker 路径仅支持 async，请用 adownload_files。"""
        raise RuntimeError(
            "DockerSandboxBackend.download_files (sync) 不可用：Docker 路径仅支持 async，请用 adownload_files"
        )

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        # 二进制读取和 edit 的读阶段会走这里；符号链接必须在读取前拒绝，
        # 避免容器先创建链接再诱导宿主适配器读取链接目标。
        results: list[FileDownloadResponse] = []
        for file_path in paths:
            try:
                target = await self._resolve_host_path(file_path, False)

                def _read() -> bytes:
                    info = os.lstat(target)
                    if stat.S_ISDIR(info.st_mode):
                        raise _PathError("is a directory", "EISDIR")
                    if stat.S_ISLNK(info.st_mode):
                        raise _PathError("symbolic link not allowed", "EINVAL")
                    with open(target, "rb") as handle:
                        return handle.read()

                content = await asyncio.to_thread(_read)
                results.append(
                    FileDownloadResponse(path=file_path, content=content, error=None)
                )
            except Exception as error:
                results.append(
                    FileDownloadResponse(
                        path=file_path,
                        content=None,
                        error=_to_file_error(error),
                    )
                )
        return results

    async def list_output_files(self) -> list[SandboxOutputFile]:
        """在模型结束后扫描唯一允许的下载出口 outputs。

        这里只返回候选元数据，真正的持久复制和 URL 签发由调用方发布器完成。
        """

        def _scan() -> list[SandboxOutputFile]:
            results: list[SandboxOutputFile] = []
            entry_count = 0
            total_bytes = 0

            def visit(directory: str, depth: int) -> None:
                nonlocal entry_count, total_bytes
                if depth > MAX_OUTPUT_TREE_DEPTH:
                    raise ValueError(f"产物目录层级不能超过 {MAX_OUTPUT_TREE_DEPTH} 层")
                with os.scandir(directory) as entries:
                    for entry in entries:
                        entry_count += 1
                        if entry_count > MAX_OUTPUT_TREE_ENTRIES:
                            raise ValueError(
                                f"产物目录包含超过 {MAX_OUTPUT_TREE_ENTRIES} 个条目"
                            )
                        absolute_path = os.path.join(directory, entry.name)
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            visit(absolute_path, depth + 1)
                        elif entry.is_file(follow_symlinks=False):
                            info = os.lstat(absolute_path)
                            total_bytes += info.st_size
                            if len(results) >= MAX_OUTPUT_FILES:
                                raise ValueError(f"每轮最多发布 {MAX_OUTPUT_FILES} 个产物")
                            if total_bytes > MAX_OUTPUT_TOTAL_BYTES:
                                raise ValueError("本轮产物总大小不能超过 250 MB")
                            relative_path = os.path.relpath(absolute_path, self._outputs_root)
                            results.append(
                                SandboxOutputFile(
                                    absolutePath=absolute_path,
                                    relativePath=relative_path,
                                    name=os.path.basename(relative_path),
                                    size=info.st_size,
                                )
                            )

            visit(self._outputs_root, 0)
            return sorted(results, key=lambda item: item.relative_path)

        return await asyncio.to_thread(_scan)

    async def list_preview_directories(self) -> list[SandboxPreviewDirectory]:
        """只把 previews 的一级子目录作为网站；每个目录必须包含普通 index.html。

        发布前完整遍历并拒绝符号链接、过深目录和超限站点。
        """

        def _scan() -> list[SandboxPreviewDirectory]:
            previews: list[SandboxPreviewDirectory] = []
            with os.scandir(self._previews_root) as candidates:
                candidate_list = list(candidates)
            for candidate in candidate_list:
                if candidate.is_symlink() or not candidate.is_dir(follow_symlinks=False):
                    continue
                if len(previews) >= MAX_PREVIEW_COUNT:
                    raise ValueError(f"每轮最多发布 {MAX_PREVIEW_COUNT} 个网站预览")
                preview_root = os.path.join(self._previews_root, candidate.name)
                index_path = os.path.join(preview_root, "index.html")
                try:
                    index_info = os.lstat(index_path)
                except FileNotFoundError:
                    index_info = None
                if (
                    index_info is None
                    or not stat.S_ISREG(index_info.st_mode)
                    or stat.S_ISLNK(index_info.st_mode)
                ):
                    raise ValueError(f"网站预览 {candidate.name} 缺少普通 index.html")

                file_count = 0
                size = 0

                def visit(directory: str, depth: int) -> None:
                    nonlocal file_count, size
                    if depth > MAX_OUTPUT_TREE_DEPTH:
                        raise ValueError(
                            f"网站预览 {candidate.name} 目录层级不能超过 {MAX_OUTPUT_TREE_DEPTH} 层"
                        )
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            absolute_path = os.path.join(directory, entry.name)
                            if entry.is_symlink():
                                raise ValueError(f"网站预览不能包含符号链接：{entry.name}")
                            if entry.is_dir(follow_symlinks=False):
                                visit(absolute_path, depth + 1)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            file_count += 1
                            if file_count > MAX_PREVIEW_FILES:
                                raise ValueError(
                                    f"网站预览 {candidate.name} 不能超过 {MAX_PREVIEW_FILES} 个文件"
                                )
                            size += os.lstat(absolute_path).st_size
                            if size > MAX_PREVIEW_BYTES:
                                raise ValueError(
                                    f"网站预览 {candidate.name} 总大小不能超过 100 MB"
                                )

                visit(preview_root, 0)
                previews.append(
                    SandboxPreviewDirectory(
                        absolutePath=preview_root,
                        name=candidate.name,
                        fileCount=file_count,
                        size=size,
                    )
                )
            return sorted(previews, key=lambda item: item.name)

        return await asyncio.to_thread(_scan)

    async def close(self) -> None:
        """释放本轮沙箱句柄。容器本身是 --rm 短生命周期，无需额外清理；
        工作区目录保留，供同一会话的后续消息继续使用。"""
        self._closed = True

    async def destroy(self) -> None:
        """关闭沙箱并删除本轮工作区，用于取消或失败的会话。"""
        self._closed = True
        await asyncio.to_thread(shutil.rmtree, self._session_root, True)

    async def _resolve_host_path(self, sandbox_path: str, writable: bool) -> str:
        # DeepAgent 偶尔会给 upload/download 传相对路径。它们始终相对于容器的
        # 默认工作目录解析，绝不能相对于进程的 os.getcwd() 解析。
        normalized = posixpath.normpath(
            sandbox_path
            if sandbox_path.startswith("/")
            else f"{DOCKER_SANDBOX_WORKSPACE}/{sandbox_path}"
        )
        root: str
        suffix: str

        if normalized == "/mnt/user-data" or normalized.startswith("/mnt/user-data/"):
            root = self._user_data_root
            suffix = normalized[len("/mnt/user-data"):]
        elif normalized == "/large_tool_results" or normalized.startswith(
            "/large_tool_results/"
        ):
            root = self._large_results_root
            suffix = normalized[len("/large_tool_results"):]
        elif normalized == "/skills" or normalized.startswith("/skills/"):
            if writable:
                raise _PathError("Skill 目录只读", "EACCES")
            root = self._skills_root
            suffix = normalized[len("/skills"):]
        elif normalized == "/mnt/skills/public" or normalized.startswith(
            "/mnt/skills/public/"
        ):
            if writable:
                raise _PathError("Skill 目录只读", "EACCES")
            root = self._skills_root
            suffix = normalized[len("/mnt/skills/public"):]
        else:
            raise _PathError(f"不允许访问路径：{sandbox_path}", "EINVAL")

        target = os.path.normpath(os.path.join(root, f".{suffix}"))
        if not _is_within(root, target):
            raise _PathError(f"路径越界：{sandbox_path}", "EINVAL")

        # 防止容器创建的符号链接在宿主侧 upload/download 时逃逸沙箱根目录。
        def _check_links() -> None:
            cursor = root
            parts = [part for part in os.path.relpath(target, root).split(os.sep) if part]
            for part in parts:
                cursor = os.path.join(cursor, part)
                try:
                    info = os.lstat(cursor)
                except FileNotFoundError:
                    if writable:
                        break
                    raise
                if stat.S_ISLNK(info.st_mode) or (
                    writable and stat.S_ISREG(info.st_mode) and info.st_nlink > 1
                ):
                    raise _PathError("不允许通过符号链接或硬链接别名写入文件", "EINVAL")

        await asyncio.to_thread(_check_links)
        return target
