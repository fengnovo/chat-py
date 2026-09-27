"""E2B 沙箱实现（e2b Python SDK，AsyncSandbox）。

对应 TS 的 E2BSandbox：Sandbox.create / commands.run / files.write。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .types import (
    AbortSignal,
    BaseSandbox,
    ExecuteResponse,
    FileDownloadResponse,
    FileOperationError,
    FileUploadResponse,
)

try:  # pragma: no cover - 取决于 e2b SDK 安装
    from e2b import AsyncSandbox as _AsyncSandbox
    from e2b import CommandExitError as _CommandExitError
except Exception:  # pragma: no cover
    _AsyncSandbox = None  # type: ignore[assignment]

    class _CommandExitError(Exception):  # type: ignore[no-redef]
        stdout: str = ""
        stderr: str = ""
        exit_code: int = 1


MAX_OUTPUT_BYTES = 200_000

# Default endpoint of a local E2B-compatible Docker sandbox service.
DEFAULT_LOCAL_E2B_ENDPOINT = "http://localhost:10087"


class E2BSandboxOptions(BaseModel):
    api_key: str = Field(alias="apiKey")
    api_url: str | None = Field(default=None, alias="apiUrl")
    sandbox_url: str | None = Field(default=None, alias="sandboxUrl")
    template: str | None = None
    timeout_ms: int | None = Field(default=None, alias="timeoutMs")
    signal: AbortSignal | None = None

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)


def resolve_e2b_endpoints(
    runtime: str | None,
    api_url: str | None,
    sandbox_url: str | None,
) -> dict[str, str]:
    """Resolves the E2B endpoints from environment-style options."""
    fallback = DEFAULT_LOCAL_E2B_ENDPOINT if (runtime or "").strip() == "local-e2b" else None
    resolved_api_url = (api_url or "").strip() or fallback
    resolved_sandbox_url = (sandbox_url or "").strip() or fallback
    result: dict[str, str] = {}
    if resolved_api_url:
        result["api_url"] = resolved_api_url
    if resolved_sandbox_url:
        result["sandbox_url"] = resolved_sandbox_url
    return result


def _classify_file_error(message: str) -> FileOperationError:
    normalized = message.lower()
    if "not found" in normalized or "no such file" in normalized:
        return "file_not_found"
    if "permission" in normalized or "denied" in normalized:
        return "permission_denied"
    if "is a directory" in normalized or "isdir" in normalized:
        return "is_directory"
    return "invalid_path"


def _create_kwargs(options: E2BSandboxOptions) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"api_key": options.api_key}
    if options.api_url:
        kwargs["api_url"] = options.api_url
    if options.sandbox_url:
        kwargs["sandbox_url"] = options.sandbox_url
    # Python SDK 的 timeout 单位是秒；TS 的 timeoutMs 单位是毫秒。
    kwargs["timeout"] = (options.timeout_ms if options.timeout_ms is not None else 600_000) / 1000
    return kwargs


class E2BSandbox(BaseSandbox):
    def __init__(self, sandbox: Any, signal: AbortSignal | None = None) -> None:
        self._sandbox = sandbox
        self._signal = signal
        self._running = True

    @classmethod
    async def create(cls, options: E2BSandboxOptions) -> "E2BSandbox":
        if _AsyncSandbox is None:
            raise RuntimeError("e2b SDK 未安装")
        sandbox = await _AsyncSandbox.create(
            options.template or "base",
            **_create_kwargs(options),
        )
        return cls(sandbox, options.signal)

    @classmethod
    async def connect(cls, sandbox_id: str, options: E2BSandboxOptions) -> "E2BSandbox":
        if _AsyncSandbox is None:
            raise RuntimeError("e2b SDK 未安装")
        timeout_ms = options.timeout_ms if options.timeout_ms is not None else 600_000
        sandbox = await _AsyncSandbox.connect(sandbox_id, **_create_kwargs(options))
        await sandbox.set_timeout(timeout_ms / 1000)
        return cls(sandbox, options.signal)

    @property
    def id(self) -> str:
        return self._sandbox.sandbox_id

    def get_host(self, port: int) -> str:
        return self._sandbox.get_host(port)

    async def set_timeout(self, timeout_ms: int) -> None:
        await self._sandbox.set_timeout(timeout_ms / 1000)

    async def execute(self, command: str) -> ExecuteResponse:
        background = bool(re.search(r"\bnohup\b|&\s*$", command))
        if background:
            await self._sandbox.commands.run(command, background=True, timeout=0)
            return {
                "output": f"[background started] {command}",
                "exitCode": 0,
                "truncated": False,
            }

        if self._signal is not None:
            self._signal.throw_if_aborted()
        handle = await self._sandbox.commands.run(command, background=True, timeout=180)

        abort_event = asyncio.Event()

        def _on_abort() -> None:
            abort_event.set()

        if self._signal is not None:
            self._signal.add_event_listener(_on_abort)
        wait_task = asyncio.ensure_future(handle.wait())
        abort_task = asyncio.ensure_future(abort_event.wait())
        try:
            if self._signal is not None and self._signal.aborted:
                try:
                    await handle.kill()
                except Exception:
                    pass
                raise self._signal.reason
            done, _pending = await asyncio.wait(
                {wait_task, abort_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if abort_task in done and not wait_task.done():
                wait_task.cancel()
                try:
                    await handle.kill()
                except Exception:
                    pass
                if self._signal is not None:
                    raise self._signal.reason
                raise RuntimeError("Aborted")
            result = await wait_task
            return self._execute_response(
                result.stdout or "", result.stderr or "", result.exit_code or 0
            )
        except _CommandExitError as error:
            if self._signal is not None and self._signal.aborted:
                raise self._signal.reason
            return self._execute_response(
                getattr(error, "stdout", "") or "",
                getattr(error, "stderr", "") or "",
                getattr(error, "exit_code", 1) or 1,
            )
        finally:
            if self._signal is not None:
                self._signal.remove_event_listener(_on_abort)
            for task in (wait_task, abort_task):
                if not task.done():
                    task.cancel()

    def _execute_response(self, stdout: str, stderr: str, exit_code: int) -> ExecuteResponse:
        combined = f"{stdout}\n{stderr}" if stderr else stdout
        encoded = combined.encode("utf-8")
        if len(encoded) <= MAX_OUTPUT_BYTES:
            return {"output": combined, "exitCode": exit_code, "truncated": False}
        return {
            "output": encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore"),
            "exitCode": exit_code,
            "truncated": True,
        }

    async def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results: list[FileUploadResponse] = []
        for file_path, content in files:
            try:
                await self._sandbox.files.write(file_path, bytes(content))
                results.append({"path": file_path, "error": None})
            except Exception as error:
                results.append(
                    {
                        "path": file_path,
                        "error": _classify_file_error(
                            str(error) if isinstance(error, BaseException) else str(error)
                        ),
                    }
                )
        return results

    async def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        results: list[FileDownloadResponse] = []
        for file_path in paths:
            try:
                content = await self._sandbox.files.read(file_path, format="bytes")
                results.append({"path": file_path, "content": bytes(content), "error": None})
            except Exception as error:
                results.append(
                    {
                        "path": file_path,
                        "content": None,
                        "error": _classify_file_error(
                            str(error) if isinstance(error, BaseException) else str(error)
                        ),
                    }
                )
        return results

    async def pause(self) -> None:
        if not self._running:
            return
        await self._sandbox.pause()
        self._running = False

    async def kill(self) -> None:
        if not self._running:
            return
        try:
            await self._sandbox.kill()
        finally:
            self._running = False
