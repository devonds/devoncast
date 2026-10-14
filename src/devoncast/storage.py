"""Cloudflare R2 over the S3 API. All keys are relative to the configured secret prefix."""

from __future__ import annotations

from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import Config

SHORT_CACHE = "public, max-age=60"
LONG_CACHE = "public, max-age=31536000, immutable"


class Store:
    def __init__(self, cfg: Config):
        r2 = cfg.r2
        if not (r2.get("access_key_id") and r2.get("secret_access_key") and r2.get("account_id")):
            raise SystemExit("R2 credentials missing: set access_key_id, secret_access_key and account_id in config.toml")
        self.bucket = r2["bucket"]
        self.prefix = r2["prefix"].strip("/")
        self.public_root = cfg.public_root
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{r2['account_id']}.r2.cloudflarestorage.com",
            aws_access_key_id=r2["access_key_id"],
            aws_secret_access_key=r2["secret_access_key"],
            region_name="auto",
            config=BotoConfig(retries={"max_attempts": 5, "mode": "standard"}),
        )

    def key(self, name: str) -> str:
        return f"{self.prefix}/{name}"

    def url(self, name: str) -> str:
        return f"{self.public_root}/{name}"

    def put_file(self, name: str, path: Path, content_type: str, cache: str = LONG_CACHE) -> str:
        self.s3.upload_file(
            str(path), self.bucket, self.key(name),
            ExtraArgs={"ContentType": content_type, "CacheControl": cache},
        )
        return self.url(name)

    def put_bytes(self, name: str, data: bytes, content_type: str, cache: str = SHORT_CACHE) -> str:
        self.s3.put_object(Bucket=self.bucket, Key=self.key(name), Body=data, ContentType=content_type, CacheControl=cache)
        return self.url(name)

    def get_bytes(self, name: str) -> bytes | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self.key(name))["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def delete(self, name: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=self.key(name))
