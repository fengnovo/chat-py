from __future__ import annotations

import re
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import BaseModel


class ArtifactStoreConfig(BaseModel):
    endpoint: str
    public_endpoint: str | None = None
    region: str
    bucket: str
    access_key: str
    secret_key: str


class ArtifactUpload(BaseModel):
    upload_url: str
    headers: dict[str, str]
    expires_at: str  # ISO datetime


class CompletedPart(BaseModel):
    number: int
    etag: str


class PresignedPartUpload(BaseModel):
    number: int
    upload_url: str
    expires_at: str


class ArtifactVerificationError(Exception):
    pass


_MIN_IMAGE_HEAD_BYTES = 12

_IMAGE_MAGIC: dict[str, object] = {
    "image/png": lambda b: (
        b[0] == 0x89
        and b[1] == 0x50
        and b[2] == 0x4E
        and b[3] == 0x47
        and b[4] == 0x0D
        and b[5] == 0x0A
        and b[6] == 0x1A
        and b[7] == 0x0A
    ),
    "image/jpeg": lambda b: b[0] == 0xFF and b[1] == 0xD8 and b[2] == 0xFF,
    "image/jpg": lambda b: b[0] == 0xFF and b[1] == 0xD8 and b[2] == 0xFF,
    "image/gif": lambda b: (
        b[0] == 0x47
        and b[1] == 0x49
        and b[2] == 0x46
        and b[3] == 0x38
        and (b[4] == 0x39 or b[4] == 0x37)
        and b[5] == 0x61
    ),
    "image/webp": lambda b: (
        b[0] == 0x52
        and b[1] == 0x49
        and b[2] == 0x46
        and b[3] == 0x46
        and b[8] == 0x57
        and b[9] == 0x45
        and b[10] == 0x42
        and b[11] == 0x50
    ),
}


def _is_missing_bucket(error: Exception) -> bool:
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code", "")
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if code in ("NotFound", "NoSuchBucket") or status == 404:
            return True
    return False


def _s3_config() -> Config:
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
    )


def artifact_object_key(tenant_id: str, run_id: str, artifact_id: str, name: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", unicodedata.normalize("NFKC", name))
    safe_name = re.sub(r"^[-.]+|[-.]+$", "", safe_name)
    safe_name = safe_name[:120] or "artifact"
    return f"tenants/{tenant_id}/runs/{run_id}/{artifact_id}/{safe_name}"


def project_snapshot_object_key(tenant_id: str, project_id: str) -> str:
    return f"tenants/{tenant_id}/projects/{project_id}/snapshot-v1.json"


def chat_attachment_object_key(
    tenant_id: str, user_id: str, attachment_id: str, filename: str
) -> str:
    safe_name = re.sub(
        r"[^a-zA-Z0-9._一-龥-]+", "-", unicodedata.normalize("NFKC", filename)
    )
    safe_name = re.sub(r"^[-.]+|[-.]+$", "", safe_name)
    safe_name = safe_name[:120] or "attachment"
    return f"tenants/{tenant_id}/users/{user_id}/chat-attachments/{attachment_id}/{safe_name}"


def assert_image_magic(data: bytes, declared_mime: str) -> None:
    if len(data) < _MIN_IMAGE_HEAD_BYTES:
        raise ArtifactVerificationError(
            f"Image payload too small to validate ({len(data)} bytes, need at least {_MIN_IMAGE_HEAD_BYTES})"
        )
    check = _IMAGE_MAGIC.get(declared_mime.lower())
    if check is None:
        raise ArtifactVerificationError(f"Unsupported image MIME: {declared_mime}")
    if not check(data):
        raise ArtifactVerificationError(
            f"Uploaded bytes do not match declared MIME {declared_mime}"
        )


class S3ArtifactStore:
    def __init__(self, config: ArtifactStoreConfig):
        self._config = config
        self._internal_session = aioboto3.Session(
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
        )
        self._signing_session = aioboto3.Session(
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
        )

    @asynccontextmanager
    async def _internal_client(self):
        async with self._internal_session.client(
            "s3",
            endpoint_url=self._config.endpoint,
            config=_s3_config(),
        ) as client:
            yield client

    @asynccontextmanager
    async def _signing_client(self):
        endpoint = self._config.public_endpoint or self._config.endpoint
        async with self._signing_session.client(
            "s3",
            endpoint_url=endpoint,
            config=_s3_config(),
        ) as client:
            yield client

    async def ensure_bucket(self) -> None:
        async with self._internal_client() as client:
            try:
                await client.head_bucket(Bucket=self._config.bucket)
            except ClientError as error:
                if not _is_missing_bucket(error):
                    raise
                await client.create_bucket(Bucket=self._config.bucket)

    async def ping(self) -> None:
        async with self._internal_client() as client:
            await client.head_bucket(Bucket=self._config.bucket)

    async def create_upload(
        self,
        object_key: str,
        content_type: str,
        sha256: str,
        expires_in: int = 900,
    ) -> ArtifactUpload:
        async with self._signing_client() as client:
            upload_url = client.generate_presigned_url(
                ClientMethod="put_object",
                Params={
                    "Bucket": self._config.bucket,
                    "Key": object_key,
                    "ContentType": content_type,
                    "Metadata": {"sha256": sha256},
                },
                ExpiresIn=expires_in,
            )
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat().replace("+00:00", "Z")
        return ArtifactUpload(
            upload_url=upload_url,
            headers={
                "content-type": content_type,
                "x-amz-meta-sha256": sha256,
            },
            expires_at=expires_at,
        )

    async def create_multipart_upload(
        self,
        object_key: str,
        content_type: str,
        sha256: str,
        content_encoding: str | None = None,
    ) -> dict[str, str]:
        async with self._internal_client() as client:
            result = await client.create_multipart_upload(
                Bucket=self._config.bucket,
                Key=object_key,
                ContentType=content_type,
                ContentEncoding=content_encoding,
                Metadata={"sha256": sha256},
            )
        upload_id = result.get("UploadId")
        if not upload_id:
            raise RuntimeError("Multipart upload id was not issued")
        return {"upload_id": upload_id}

    async def presign_part_upload(
        self,
        object_key: str,
        upload_id: str,
        part_number: int,
        expires_in: int = 600,
    ) -> PresignedPartUpload:
        async with self._signing_client() as client:
            upload_url = client.generate_presigned_url(
                ClientMethod="upload_part",
                Params={
                    "Bucket": self._config.bucket,
                    "Key": object_key,
                    "UploadId": upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=expires_in,
            )
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat().replace("+00:00", "Z")
        return PresignedPartUpload(
            number=part_number,
            upload_url=upload_url,
            expires_at=expires_at,
        )

    async def complete_multipart_upload(
        self,
        object_key: str,
        upload_id: str,
        parts: list[CompletedPart],
    ) -> None:
        sorted_parts = sorted(parts, key=lambda p: p.number)
        async with self._internal_client() as client:
            await client.complete_multipart_upload(
                Bucket=self._config.bucket,
                Key=object_key,
                UploadId=upload_id,
                MultipartUpload={
                    "Parts": [
                        {"ETag": part.etag, "PartNumber": part.number}
                        for part in sorted_parts
                    ]
                },
            )

    async def abort_multipart_upload(
        self, object_key: str, upload_id: str
    ) -> None:
        async with self._internal_client() as client:
            try:
                await client.abort_multipart_upload(
                    Bucket=self._config.bucket,
                    Key=object_key,
                    UploadId=upload_id,
                )
            except ClientError as error:
                if not _is_missing_bucket(error):
                    raise

    async def verify_object(
        self, object_key: str, size_bytes: int, sha256: str
    ) -> None:
        async with self._internal_client() as client:
            try:
                obj = await client.head_object(
                    Bucket=self._config.bucket, Key=object_key
                )
            except ClientError as error:
                if _is_missing_bucket(error):
                    raise ArtifactVerificationError(
                        "Artifact object was not uploaded"
                    )
                raise

        if obj.get("ContentLength") != size_bytes:
            raise ArtifactVerificationError("Artifact size does not match")
        if obj.get("Metadata", {}).get("sha256") != sha256:
            raise ArtifactVerificationError(
                "Artifact checksum metadata does not match"
            )

    async def put_object(
        self, object_key: str, body: bytes, content_type: str
    ) -> None:
        async with self._internal_client() as client:
            await client.put_object(
                Bucket=self._config.bucket,
                Key=object_key,
                Body=body,
                ContentType=content_type,
            )

    async def get_object_bytes(self, object_key: str) -> bytes:
        async with self._internal_client() as client:
            obj = await client.get_object(
                Bucket=self._config.bucket, Key=object_key
            )
        body = obj.get("Body")
        if body is None:
            raise RuntimeError("Object body is empty")
        return await body.read()

    async def get_object_head(self, object_key: str, length: int) -> bytes:
        if not isinstance(length, int) or length < 1:
            raise ValueError("Invalid head length")
        async with self._internal_client() as client:
            obj = await client.get_object(
                Bucket=self._config.bucket,
                Key=object_key,
                Range=f"bytes=0-{length - 1}",
            )
        body = obj.get("Body")
        if body is None:
            raise RuntimeError("Object body is empty")
        return await body.read()

    async def create_download_url(
        self, object_key: str, expires_in: int = 300
    ) -> str:
        async with self._signing_client() as client:
            return client.generate_presigned_url(
                ClientMethod="get_object",
                Params={
                    "Bucket": self._config.bucket,
                    "Key": object_key,
                },
                ExpiresIn=expires_in,
            )

    async def delete_object(self, object_key: str) -> None:
        async with self._internal_client() as client:
            try:
                await client.delete_object(
                    Bucket=self._config.bucket, Key=object_key
                )
            except ClientError as error:
                if not _is_missing_bucket(error):
                    raise

    def destroy(self) -> None:
        # aioboto3 clients are managed via async context managers per operation;
        # no persistent resources need explicit cleanup.
        pass
