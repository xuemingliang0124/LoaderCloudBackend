"""MinIO 对象存储封装：脚本/参数文件/JTL/HTML 报告统一归档。

key 规范（skill 文档第 4.6 条）：
- scripts/{script_id}/{version}/xxx.jmx
- runs/{run_no}/{agent_id}/xxx.jtl | report.zip

presigned URL 使用 minio_public_endpoint 生成（Agent 侧可达地址），
未配置时回退 minio_endpoint，仅适用于 Agent 与 MinIO 同网络的部署。
"""

import io
from datetime import timedelta

from loguru import logger
from miniopy_async import Minio

from app.core.config import get_settings

_minio: Minio | None = None
_presign: Minio | None = None


def get_minio() -> Minio:
    global _minio
    if _minio is None:
        settings = get_settings()
        _minio = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
    return _minio


def get_presign_client() -> Minio:
    """presign 专用客户端：endpoint 必须是 Agent 侧可达地址，否则 Agent 拿到内网 URL 无法下载。"""
    global _presign
    if _presign is None:
        settings = get_settings()
        _presign = Minio(
            settings.minio_public_endpoint or settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
    return _presign


async def ensure_bucket() -> None:
    settings = get_settings()
    client = get_minio()
    if not await client.bucket_exists(settings.minio_bucket):
        await client.make_bucket(settings.minio_bucket)
        logger.info(f"已创建 bucket: {settings.minio_bucket}")


async def upload_bytes(object_key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    await get_minio().put_object(
        get_settings().minio_bucket, object_key, io.BytesIO(data), length=len(data), content_type=content_type
    )


async def presigned_get(object_key: str, expires_hours: int = 6) -> str:
    """生成下载预签名 URL（默认 6h，覆盖任务排队等待期）。"""
    return await get_presign_client().presigned_get_object(
        get_settings().minio_bucket, object_key, expires=timedelta(hours=expires_hours)
    )


async def presigned_put(object_key: str, expires_hours: int = 6) -> str:
    """生成上传预签名 URL（默认 6h）。注意：签发时未含 Content-Type，PUT 时不可额外携带该头。"""
    return await get_presign_client().presigned_put_object(
        get_settings().minio_bucket, object_key, expires=timedelta(hours=expires_hours)
    )
