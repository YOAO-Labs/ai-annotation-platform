from __future__ import annotations

import io
import logging
import re
import time
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from app.config import settings

if TYPE_CHECKING:
    from app.db.models.task import Task

logger = logging.getLogger(__name__)

# presigned URL 的 Expires 对齐到 10 分钟网格, 使同一对象在一个窗口内签出的 URL 逐字节相同。
# 不对齐时 Expires 逐秒递增, 签名随之改变: MinIO 不下发 Cache-Control, 浏览器只能按
# 完整 URL (含签名) 做缓存 key, 于是每次 task 列表 refetch 都会让全部缩略图/原图缓存失效并重下。
# 代价是实际有效期在 [expires_in, expires_in + _PRESIGN_ALIGN_WINDOW] 之间浮动。
_PRESIGN_ALIGN_WINDOW = 600
TRUSTED_NUSCENES_PREFIX = "__aap_trusted_nuscenes__"


def _aligned_expires_in(expires_in: int) -> int:
    """把相对有效期换算成能让绝对 Expires 落在窗口边界上的相对值。

    botocore 取 ``Expires = int(time.time()) + ExpiresIn``, 它内部那次取时刻若恰好跨过秒
    边界, 签出的 Expires 会比预期大 1 秒。这类抖动只让当次 URL 落单 (多下一次), 下个窗口
    自愈, 不值得为它补一轮重签。
    """
    now = int(time.time())
    deadline = ((now // _PRESIGN_ALIGN_WINDOW) + 1) * _PRESIGN_ALIGN_WINDOW + expires_in
    return deadline - now


class StorageService:
    def __init__(self) -> None:
        scheme = "https" if settings.minio_use_ssl else "http"
        self.client = boto3.client(
            "s3",
            endpoint_url=f"{scheme}://{settings.minio_endpoint}",
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
        )
        self.bucket = settings.minio_bucket
        self.datasets_bucket = settings.minio_datasets_bucket
        self.bug_reports_bucket = settings.minio_bug_reports_bucket
        self.media_cache_bucket = settings.minio_media_cache_bucket
        self.audit_archive_bucket = settings.minio_audit_archive_bucket
        self.import_bucket = settings.minio_import_bucket
        self.export_bucket = settings.minio_export_bucket
        self.avatars_bucket = settings.minio_avatars_bucket

    def ensure_bucket(self, bucket: str | None = None) -> None:
        b = bucket or self.bucket
        try:
            self.client.head_bucket(Bucket=b)
        except ClientError:
            self.client.create_bucket(Bucket=b)

    def ensure_all_buckets(self) -> None:
        self.ensure_bucket(self.bucket)
        self.ensure_bucket(self.datasets_bucket)
        self.ensure_bucket(self.bug_reports_bucket)
        self.ensure_bucket(self.media_cache_bucket)
        self.ensure_bucket(self.audit_archive_bucket)
        self.ensure_bucket(self.import_bucket)
        self.ensure_bucket(self.export_bucket)
        self.ensure_bucket(self.avatars_bucket)
        self._ensure_lifecycle()

    def _ensure_lifecycle(self) -> None:
        """v0.6.6 · 评论附件 90 天过期。
        B-4 · bug 截图迁到独立桶 ``bug-reports``,在该桶上挂 180 天 lifecycle。
        v0.10.17 · 派生媒体缓存独立桶 ``media-cache`` 整桶 30 天过期。
        若 MinIO 不支持（旧版本）静默忽略。
        """
        anno_rules = [
            {
                "ID": "comment-attachments-90d",
                "Status": "Enabled",
                "Filter": {"Prefix": "comment-attachments/"},
                "Expiration": {"Days": 90},
            },
        ]
        try:
            self.client.put_bucket_lifecycle_configuration(
                Bucket=self.bucket,
                LifecycleConfiguration={"Rules": anno_rules},
            )
        except ClientError as exc:
            logger.warning("Failed to set bucket lifecycle on %s: %s", self.bucket, exc)

        bug_rules = [
            {
                "ID": "bug-screenshots-180d",
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "Expiration": {"Days": 180},
            },
        ]
        try:
            self.client.put_bucket_lifecycle_configuration(
                Bucket=self.bug_reports_bucket,
                LifecycleConfiguration={"Rules": bug_rules},
            )
        except ClientError as exc:
            logger.warning(
                "Failed to set bucket lifecycle on %s: %s", self.bug_reports_bucket, exc
            )

        # 只过期「可按需重新抽取」的帧图 / 分块缓存(videos/<id>/frames|chunks)。
        # playback/(非 h264 视频的浏览器播放代理) 与 thumbnails/(海报) 是 DB 元数据
        # 持久引用、入库时一次性生成、不会惰性重建,必须随原视频同寿命——绝不能进过期规则。
        # 历史教训:旧规则 Prefix="" 把整桶都按 30 天过期,playback 代理 30 天后被删而
        # DB 仍指向它,<video> 取预签名 URL 命中 404 → 浏览器报 MEDIA_ELEMENT_ERROR: Format error。
        media_cache_rules = [
            {
                "ID": "media-cache-frames-chunks-30d",
                "Status": "Enabled",
                "Filter": {"Prefix": "videos/"},
                "Expiration": {"Days": 30},
            },
        ]
        try:
            self.client.put_bucket_lifecycle_configuration(
                Bucket=self.media_cache_bucket,
                LifecycleConfiguration={"Rules": media_cache_rules},
            )
        except ClientError as exc:
            logger.warning(
                "Failed to set bucket lifecycle on %s: %s", self.media_cache_bucket, exc
            )

        # v0.10.27 · 导入/导出产物桶整桶 7 天过期(短生命周期产物)。
        for short_lived_bucket, rule_id in (
            (self.import_bucket, "import-artifacts-7d"),
            (self.export_bucket, "export-artifacts-7d"),
        ):
            try:
                self.client.put_bucket_lifecycle_configuration(
                    Bucket=short_lived_bucket,
                    LifecycleConfiguration={
                        "Rules": [
                            {
                                "ID": rule_id,
                                "Status": "Enabled",
                                "Filter": {"Prefix": ""},
                                "Expiration": {"Days": 7},
                            },
                        ]
                    },
                )
            except ClientError as exc:
                logger.warning(
                    "Failed to set bucket lifecycle on %s: %s", short_lived_bucket, exc
                )

        # audit-archive 桶不挂 lifecycle:合规要求永久保留。运维可单独开 object lock。
        # avatars 桶同样不挂 lifecycle:头像是不可重生的持久身份数据,由 users.avatar_ref
        # 持久引用;一旦过期就会出现"DB 指向已删对象"的破图(与上面 playback 同类故障)。
        # 修改头像时由 API 显式删除旧对象,不依赖 lifecycle。

    def _public_url(self, url: str) -> str:
        if settings.minio_public_url:
            scheme = "https" if settings.minio_use_ssl else "http"
            internal = f"{scheme}://{settings.minio_endpoint}"
            url = url.replace(internal, settings.minio_public_url.rstrip("/"), 1)
        return url

    def rewrite_host_for_ml_backend(self, url: str) -> str:
        """把 presigned URL 的 host 重写为 ``ml_backend_storage_host`` (容器可达地址)。

        平台 api 跑在 host 进程而 ML backend 在 docker 网内时, presigned URL 里的 host
        (宿主可达) 在 backend 容器内解析不到; 重写成容器可达地址。v0.18.4 从
        ``ml_backends._resolve_task_url`` 抽出共用 (task URL 与 ROI crop URL 共享), 行为逐字一致。
        """
        if settings.ml_backend_storage_host:
            # generate_download_url 已换成浏览器地址。DEV 同源 /minio 无
            # host，必须先还原内部绝对 URL，再改成 Docker 网关地址。
            if settings.minio_public_url:
                scheme = "https" if settings.minio_use_ssl else "http"
                internal = f"{scheme}://{settings.minio_endpoint}"
                public = settings.minio_public_url.rstrip("/")
                if url.startswith(public + "/"):
                    url = internal + url[len(public) :]
            return re.sub(
                r"://[^/]+", f"://{settings.ml_backend_storage_host}", url, count=1
            )
        return url

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
        bucket: str | None = None,
    ) -> None:
        """小对象直写入口(如头像 WebP)。大文件请用 ``upload_file`` 走分段上传。"""
        extra: dict = {"ContentType": content_type}
        if cache_control:
            extra["CacheControl"] = cache_control
        self.client.put_object(
            Bucket=bucket or self.bucket,
            Key=key,
            Body=data,
            **extra,
        )

    def upload_crop_bytes(
        self, jpeg_bytes: bytes, key: str, *, expires_in: int = 3600
    ) -> str:
        """v0.18.4 · 上传 ROI crop JPEG 字节到 import 桶, 返回下游 backend 可拉取的 presigned URL。

        import 桶挂 7 天 lifecycle 自动清理临时 crop。返回 URL 经 ml_backend host 重写,
        与 task URL 投递路径一致——对所有走 ``httpx.get`` 的下游 backend (gsam2/sam3 不支持
        ``data:`` URI) 通用。
        """
        self.client.put_object(
            Bucket=self.import_bucket,
            Key=key,
            Body=jpeg_bytes,
            ContentType="image/jpeg",
        )
        url = self.generate_download_url(
            key, expires_in=expires_in, bucket=self.import_bucket
        )
        return self.rewrite_host_for_ml_backend(url)

    def generate_upload_url(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        expires_in: int = 900,
        bucket: str | None = None,
    ) -> str:
        url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket or self.bucket,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
        )
        return self._public_url(url)

    def generate_download_url(
        self,
        key: str,
        expires_in: int = 3600,
        bucket: str | None = None,
        download_name: str | None = None,
        align: bool = True,
    ) -> str:
        # v0.10.43 · download_name 经 ResponseContentDisposition 给浏览器一个友好文件名。
        params: dict = {"Bucket": bucket or self.bucket, "Key": key}
        if download_name:
            params["ResponseContentDisposition"] = (
                f'attachment; filename="{download_name}"'
            )
        # align=True (默认) 把 Expires 对齐到 10 分钟网格以复用浏览器缓存 (缩略图/原图);
        # 非缓存类、要求严格短有效期的调用者 (如评论附件私链) 传 align=False 走精确 expires_in。
        url = self.client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=_aligned_expires_in(expires_in) if align else expires_in,
        )
        return self._public_url(url)

    def upload_file(
        self,
        local_path: str,
        key: str,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        """从本地文件路径流式上传（boto3 managed multipart，不把整文件读进内存）。

        v0.12.1 · 导出落盘式 ZIP 的上传入口：worker 把 ZIP 写到 tempfile 后用本方法
        分段上传，内存与产物大小解耦（对比旧 put_object(Body=bytes) 全量驻留 RAM）。
        """
        extra_args = {"ContentType": content_type}
        if cache_control:
            extra_args["CacheControl"] = cache_control
        self.client.upload_file(
            local_path,
            bucket or self.bucket,
            key,
            ExtraArgs=extra_args,
        )

    def verify_upload(self, key: str, bucket: str | None = None) -> dict | None:
        try:
            return self.client.head_object(Bucket=bucket or self.bucket, Key=key)
        except ClientError:
            return None

    def delete_object(self, key: str, bucket: str | None = None) -> None:
        self.client.delete_object(Bucket=bucket or self.bucket, Key=key)

    def delete_prefix(self, prefix: str, bucket: str | None = None) -> int:
        """Delete one exact derived-asset prefix in bounded S3 batches."""
        b = bucket or self.bucket
        normalized = prefix.rstrip("/") + "/"
        deleted = 0
        continuation_token = None
        while True:
            kwargs: dict = {
                "Bucket": b,
                "Prefix": normalized,
                "MaxKeys": 1000,
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = self.client.list_objects_v2(**kwargs)
            objects = [{"Key": obj["Key"]} for obj in response.get("Contents", [])]
            if objects:
                self.client.delete_objects(
                    Bucket=b,
                    Delete={"Objects": objects, "Quiet": True},
                )
                deleted += len(objects)
            if not response.get("IsTruncated"):
                return deleted
            continuation_token = response["NextContinuationToken"]

    def list_objects(self, prefix: str, bucket: str | None = None) -> list[dict]:
        b = bucket or self.bucket
        result: list[dict] = []
        continuation_token = None
        while True:
            kwargs: dict = {"Bucket": b, "Prefix": prefix}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            resp = self.client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                result.append(
                    {
                        "key": obj["Key"],
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"],
                        "etag": (obj.get("ETag") or "").strip('"'),
                    }
                )
            if resp.get("IsTruncated"):
                continuation_token = resp["NextContinuationToken"]
            else:
                break
        return result

    def head_object_etag(self, key: str, bucket: str | None = None) -> str | None:
        meta = self.verify_upload(key, bucket=bucket)
        if not meta:
            return None
        etag = meta.get("ETag") or ""
        return etag.strip('"') or None

    def summarize_bucket(self, bucket: str | None = None) -> dict:
        """统计桶内对象数量与总字节数；状态字段独立返回 (ok|error)。"""
        b = bucket or self.bucket
        try:
            objs = self.list_objects("", bucket=b)
        except ClientError as e:
            return {
                "name": b,
                "status": "error",
                "object_count": 0,
                "total_size_bytes": 0,
                "error": str(e),
            }
        total = sum(o["size"] for o in objs if not o["key"].endswith("/"))
        count = sum(1 for o in objs if not o["key"].endswith("/"))
        return {
            "name": b,
            "status": "ok",
            "object_count": count,
            "total_size_bytes": int(total),
        }

    def list_all_buckets(self) -> list[str]:
        return [
            self.bucket,
            self.datasets_bucket,
            self.bug_reports_bucket,
            self.media_cache_bucket,
            self.audit_archive_bucket,
            self.import_bucket,
            self.export_bucket,
            self.avatars_bucket,
        ]

    # v0.10.17 · 派生媒体缓存按 key 前缀路由到 media-cache 桶。
    # 老数据若仍在 datasets 桶,运维可用 mc mirror 一次性搬迁,期间无下行兼容
    # (派生缓存可重生)。前缀清单与 workers/media.py / video_frame_service.py 写侧保持一致。
    MEDIA_CACHE_PREFIXES: tuple[str, ...] = (
        "thumbnails/",
        "videos/",
        "playback/",
        "image-pyramids/",
    )

    def bucket_for_cache_key(self, key: str, default: str | None = None) -> str:
        """根据派生缓存 key 前缀选桶。默认回退到 default(通常 datasets_bucket)。"""
        if key and key.startswith(self.MEDIA_CACHE_PREFIXES):
            return self.media_cache_bucket
        return default or self.datasets_bucket

    def create_folder(self, folder_name: str, bucket: str | None = None) -> None:
        b = bucket or self.bucket
        self.client.put_object(Bucket=b, Key=f"{folder_name}/", Body=b"")

    @staticmethod
    def read_image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
        """从已下载字节直接解析图像尺寸（zip 内文件已在内存）。"""
        try:
            from PIL import Image  # noqa: PLC0415
        except ImportError:
            return None
        try:
            with Image.open(io.BytesIO(data)) as img:
                return StorageService._logical_image_dimensions(img)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _logical_image_dimensions(img) -> tuple[int, int]:
        """Return browser/libvips autorotated dimensions without decoding pixels."""
        width, height = int(img.width), int(img.height)
        try:
            orientation = int(img.getexif().get(274, 1))
        except Exception:  # noqa: BLE001 - malformed EXIF is treated as orientation 1
            orientation = 1
        if orientation in {5, 6, 7, 8}:
            return height, width
        return width, height

    def read_image_dimensions(
        self,
        key: str,
        bucket: str | None = None,
        head_bytes: int = 256 * 1024,
    ) -> tuple[int, int] | None:
        """读取对象前若干字节交给 Pillow 解析尺寸。无法解析返回 None；不抛。

        Pillow 大多格式（JPEG / PNG / WEBP / GIF）只需读到文件头即可拿到 size，
        故只 Range-fetch 头部 head_bytes（默认 256KB）来避开整张大图的下载与内存。
        """
        try:
            from PIL import Image  # noqa: PLC0415 - 延迟导入，未安装时仅尺寸功能失效
        except ImportError:
            logger.warning("Pillow 未安装，跳过尺寸读取 key=%s", key)
            return None

        b = bucket or self.bucket
        try:
            resp = self.client.get_object(
                Bucket=b, Key=key, Range=f"bytes=0-{head_bytes - 1}"
            )
            data = resp["Body"].read()
        except ClientError as exc:
            logger.warning("读取对象 head 失败 key=%s err=%s", key, exc)
            return None

        try:
            with Image.open(io.BytesIO(data)) as img:
                return self._logical_image_dimensions(img)
        except Exception as exc:  # noqa: BLE001 - 任意 PIL / 损坏文件错误
            logger.info("Pillow 解析失败 key=%s err=%s", key, exc)
            return None


storage_service = StorageService()


def resolve_task_url(task: "Task") -> str:
    """把 task.file_path (MinIO 对象 key) 转成 ML backend 可访问的 presigned URL。

    SAM backend 协议要求 file_path 是 http(s):// URL 或本地路径; tasks 表里存的是 key,
    必须先签发 presigned URL。当平台 api 跑在 host 进程而 ML backend 在 docker 网内时,
    再把 host 替换为 ``settings.ml_backend_storage_host`` (容器可达地址)。

    v0.23.0 · 从 ``app.api.v1.ml_backends._resolve_task_url`` 下沉到 service 层,
    让 router、worker 与 video tracker runner 共用同一 helper, 消除 service → API
    反向依赖。行为与原 router 内私有函数逐字一致。
    """
    bucket = (
        storage_service.datasets_bucket
        if task.dataset_item_id
        else storage_service.bucket
    )
    url = storage_service.generate_download_url(task.file_path, bucket=bucket)
    return storage_service.rewrite_host_for_ml_backend(url)
