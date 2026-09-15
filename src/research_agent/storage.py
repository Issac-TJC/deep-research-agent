import asyncio
import hashlib

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from research_agent.settings import Settings


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ObjectStore:
    def __init__(self, settings: Settings):
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name="us-east-1",
            config=Config(signature_version="s3v4", retries={"max_attempts": 2}),
        )

    async def setup(self):
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"404", "NoSuchBucket"}:
                raise
            await asyncio.to_thread(self.client.create_bucket, Bucket=self.bucket)

    async def put(self, tenant: str, data: bytes, kind: str, content_type: str) -> str:
        key = f"{tenant}/{kind}/{sha256(data)}"
        await asyncio.to_thread(
            self.client.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
        )
        return key

    async def get(self, tenant: str, key: str) -> bytes:
        if not key.startswith(tenant + "/") or ".." in key:
            raise PermissionError("object outside tenant namespace")

        def read():
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            try:
                return response["Body"].read()
            finally:
                response["Body"].close()

        return await asyncio.to_thread(read)

    async def delete(self, tenant: str, key: str) -> None:
        if not key.startswith(tenant + "/") or ".." in key:
            raise PermissionError("object outside tenant namespace")
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)
