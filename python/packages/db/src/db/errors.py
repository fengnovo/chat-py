"""Repository error types — mirrors the error classes in packages/db."""

from __future__ import annotations


class RepositoryNotFoundError(Exception):
    """A referenced row does not exist (mirrors TS RepositoryNotFoundError)."""

    def __init__(self, resource: str) -> None:
        self.resource = resource
        super().__init__(f"{resource} not found")


class RepositoryConflictError(Exception):
    """A uniqueness or state conflict occurred (mirrors TS RepositoryConflictError)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ForbiddenKnowledgeError(Exception):
    """Knowledge base access denied (mirrors TS ForbiddenKnowledgeError)."""

    status_code = 403

    def __init__(self, message: str = "Knowledge base access denied") -> None:
        super().__init__(message)
