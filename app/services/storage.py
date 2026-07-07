from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator
from urllib.parse import quote

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

# Reasonable streaming chunk size used by all backends.
_DEFAULT_STREAM_CHUNK = 1024 * 1024  # 1 MiB


class StorageError(Exception):
    pass


@dataclass
class StoredObject:
    key: str
    url: str


class BaseStorage:
    def save_bytes(self, *, key: str, data: bytes, content_type: str | None = None) -> StoredObject:
        raise NotImplementedError

    def open_bytes(self, *, key: str) -> tuple[bytes, str | None]:
        raise NotImplementedError

    def save_stream(
        self,
        *,
        key: str,
        stream: BinaryIO,
        content_type: str | None = None,
        chunk_size: int = _DEFAULT_STREAM_CHUNK,
    ) -> StoredObject:
        """Save object from a binary stream.

        Default implementation drains ``stream`` into memory once and delegates
        to ``save_bytes``. Backends with native multipart support should
        override this so the file is uploaded without buffering the whole blob
        in Python.
        """
        data = stream.read()
        return self.save_bytes(key=key, data=data, content_type=content_type)

    def open_stream(
        self,
        *,
        key: str,
        chunk_size: int = _DEFAULT_STREAM_CHUNK,
    ) -> tuple[Iterable[bytes], str | None, int | None]:
        """Open object as a stream of chunks.

        Returns ``(chunk_iter, content_type, total_size)``. ``total_size`` may
        be ``None`` when the backend cannot know the size up front.
        """
        raise NotImplementedError

    def delete_prefix(self, *, prefix: str) -> int:
        raise NotImplementedError

    def ensure_ready(self) -> None:
        return None

    def public_url(self, *, key: str) -> str:
        raise NotImplementedError


class LocalStorage(BaseStorage):
    def __init__(self, base_dir: Path, url_prefix: str = "/media"):
        self.base_dir = base_dir
        self.url_prefix = url_prefix.rstrip("/") or "/media"

    def _path_for_key(self, key: str) -> Path:
        normalized = key.lstrip("/")
        path = (self.base_dir / normalized).resolve()
        try:
            path.relative_to(self.base_dir.resolve())
        except ValueError as exc:
            raise StorageError("Invalid storage key") from exc
        return path

    def save_bytes(self, *, key: str, data: bytes, content_type: str | None = None) -> StoredObject:
        path = self._path_for_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoredObject(key=key, url=self.public_url(key=key))

    def save_stream(
        self,
        *,
        key: str,
        stream: BinaryIO,
        content_type: str | None = None,
        chunk_size: int = _DEFAULT_STREAM_CHUNK,
    ) -> StoredObject:
        path = self._path_for_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Stream straight to disk so memory stays bounded regardless of size.
        with path.open("wb") as out:
            while True:
                buf = stream.read(chunk_size)
                if not buf:
                    break
                out.write(buf)
        return StoredObject(key=key, url=self.public_url(key=key))

    def open_bytes(self, *, key: str) -> tuple[bytes, str | None]:
        path = self._path_for_key(key)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(key)
        content_type, _ = mimetypes.guess_type(path.name)
        return path.read_bytes(), content_type

    def open_stream(
        self,
        *,
        key: str,
        chunk_size: int = _DEFAULT_STREAM_CHUNK,
    ) -> tuple[Iterable[bytes], str | None, int | None]:
        path = self._path_for_key(key)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(key)
        content_type, _ = mimetypes.guess_type(path.name)

        def _iter() -> Iterator[bytes]:
            with path.open("rb") as src:
                while True:
                    buf = src.read(chunk_size)
                    if not buf:
                        break
                    yield buf

        return _iter(), content_type, path.stat().st_size

    def delete_prefix(self, *, prefix: str) -> int:
        root = self._path_for_key(prefix)
        if root.is_file():
            root.unlink(missing_ok=True)
            return 1
        if not root.exists():
            return 0
        count = 0
        for child in sorted(root.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink(missing_ok=True)
                count += 1
            elif child.is_dir():
                child.rmdir()
        root.rmdir()
        return count

    def public_url(self, *, key: str) -> str:
        return f"{self.url_prefix}/{quote(key, safe='/')}"


class S3Storage(BaseStorage):
    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        region: str | None = None,
        public_base_url: str | None = None,
    ):
        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region or None,
            config=Config(signature_version="s3v4"),
        )
        # Transfer manager powers streaming uploads without buffering the full
        # object in memory. Imported lazily so base clients that never upload
        # large files do not pay the cost on startup.
        from boto3.s3.transfer import TransferManager

        self._transfer_manager = TransferManager(client=self.client)

    def save_bytes(self, *, key: str, data: bytes, content_type: str | None = None) -> StoredObject:
        extra_args = {}
        if content_type:
            extra_args["ContentType"] = content_type
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra_args)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"Failed to store object {key}: {exc}") from exc
        return StoredObject(key=key, url=self.public_url(key=key))

    def save_stream(
        self,
        *,
        key: str,
        stream: BinaryIO,
        content_type: str | None = None,
        chunk_size: int = _DEFAULT_STREAM_CHUNK,
    ) -> StoredObject:
        # Use the boto3 multipart upload manager so we never buffer the full
        # object in Python memory. For tiny objects the API collapses this into
        # a single put_object under the hood.
        extra_args = {}
        if content_type:
            extra_args["ContentType"] = content_type
        try:
            uploader = self._transfer_manager.upload(
                self.bucket,
                key,
                stream,
                extra_args=extra_args,
            )
            uploader.result()
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"Failed to store object {key}: {exc}") from exc
        return StoredObject(key=key, url=self.public_url(key=key))

    def open_bytes(self, *, key: str) -> tuple[bytes, str | None]:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except self.client.exceptions.NoSuchKey as exc:
            raise FileNotFoundError(key) from exc
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"Failed to read object {key}: {exc}") from exc
        content_type = response.get("ContentType")
        body = response["Body"].read()
        return body, content_type

    def open_stream(
        self,
        *,
        key: str,
        chunk_size: int = _DEFAULT_STREAM_CHUNK,
    ) -> tuple[Iterable[bytes], str | None, int | None]:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except self.client.exceptions.NoSuchKey as exc:
            raise FileNotFoundError(key) from exc
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"Failed to read object {key}: {exc}") from exc
        content_type = response.get("ContentType")
        body = response["Body"]
        size = response.get("ContentLength")

        def _iter() -> Iterator[bytes]:
            try:
                while True:
                    buf = body.read(chunk_size)
                    if not buf:
                        break
                    yield buf
            finally:
                body.close()

        return _iter(), content_type, size

    def delete_prefix(self, *, prefix: str) -> int:
        deleted = 0
        continuation_token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = self.client.list_objects_v2(**kwargs)
            contents = response.get("Contents", [])
            if contents:
                self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": item["Key"]} for item in contents], "Quiet": True},
                )
                deleted += len(contents)
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
        return deleted

    def ensure_ready(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.client.create_bucket(Bucket=self.bucket)

    def public_url(self, *, key: str) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{quote(key, safe='/')}"
        return f"/media/{quote(key, safe='/')}"


def guess_content_type(filename: str) -> str:
    content_type, _ = mimetypes.guess_type(filename)
    return content_type or "application/octet-stream"


_storage_instance: BaseStorage | None = None


def get_storage() -> BaseStorage:
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance

    backend = settings.storage_backend.lower().strip()
    if backend == "local":
        _storage_instance = LocalStorage(base_dir=settings.local_media_root_path, url_prefix="/media")
        return _storage_instance
    if backend in {"s3", "seaweedfs"}:
        required = {
            "storage_s3_endpoint_url": settings.storage_s3_endpoint_url,
            "storage_s3_access_key": settings.storage_s3_access_key,
            "storage_s3_secret_key": settings.storage_s3_secret_key,
            "storage_s3_bucket": settings.storage_s3_bucket,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise StorageError(f"Missing storage settings: {', '.join(missing)}")
        _storage_instance = S3Storage(
            endpoint_url=settings.storage_s3_endpoint_url,
            access_key_id=settings.storage_s3_access_key,
            secret_access_key=settings.storage_s3_secret_key,
            bucket=settings.storage_s3_bucket,
            region=settings.storage_s3_region,
            public_base_url=settings.storage_public_base_url,
        )
        return _storage_instance
    raise StorageError(f"Unsupported storage backend: {settings.storage_backend}")


def media_object_key(deck_id: str, filename: str) -> str:
    return f"{deck_id}/{filename}"


def deck_media_prefix(deck_id: str) -> str:
    return f"{deck_id}/"
