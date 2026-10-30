import logging
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

from utils.constants import PLATFORM_CONFIG

load_dotenv()

logger = logging.getLogger(__name__)


def redact_presigned_url_for_log(url: str) -> str:
    """
    Strip query strings from HTTP(S) URLs before logging.
    Presigned S3 URLs embed temporary credentials in the query; they must not appear in logs.
    """
    if not url or not isinstance(url, str):
        return url
    if url.startswith(("http://", "https://")) and "?" in url:
        return url.split("?", 1)[0] + "?<redacted>"
    return url


def get_recordings_bucket_and_prefix() -> Tuple[Optional[str], str]:
    """
    Resolve recordings bucket and prefix (env overrides PLATFORM_CONFIG).
    Prefix is stripped of leading/trailing slashes for key construction.
    """
    bucket = os.getenv("S3_RECORDINGS_BUCKET") or PLATFORM_CONFIG.get("S3_RECORDINGS_BUCKET")
    raw_prefix = os.getenv("S3_RECORDINGS_PREFIX") or PLATFORM_CONFIG.get("S3_RECORDINGS_PREFIX", "Recordings") or ""
    prefix = raw_prefix.strip("/")
    return bucket, prefix


def _get_s3_client():
    """
    S3 client for the recordings bucket only (uploads, presigned GET, streaming download).

    When S3_RECORDINGS_AWS_ACCESS_KEY_ID and S3_RECORDINGS_AWS_SECRET_ACCESS_KEY are set,
    those credentials are used (corp_ai_workloads_stg or any account you configure).
    Otherwise falls back to the default credential chain (e.g. AWS_* or instance role).

    Does not affect Secrets Manager, Bedrock, or other AWS clients elsewhere.
    """
    region = (
        os.getenv("S3_RECORDINGS_AWS_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    access_key = os.getenv("S3_RECORDINGS_AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("S3_RECORDINGS_AWS_SECRET_ACCESS_KEY")
    session_token = os.getenv("S3_RECORDINGS_AWS_SESSION_TOKEN")

    if access_key and secret_key:
        session = boto3.session.Session(region_name=region)
        logger.debug("S3 recordings client using S3_RECORDINGS_AWS_* credentials")
        return session.client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token or None,
        )

    logger.info(
        "S3 recordings: S3_RECORDINGS_AWS_* not fully set; using default credential chain for recordings bucket"
    )
    session = boto3.session.Session(region_name=region)
    return session.client("s3")


def _build_http_url(bucket: str, key: str) -> str:
    # Use virtual-hosted–style URL
    return f"https://{bucket}.s3.amazonaws.com/{key}"


def upload_file_to_s3_and_get_urls(
    local_zip_path: Path,
    bucket: Optional[str] = None,
    key_prefix: Optional[str] = None,
    create_presigned: bool = True,
    expires_seconds: Optional[int] = None,
) -> Dict[str, str]:
    """
    Upload a local zip file to S3 and return URLs.

    Returns dict with keys: 's3_url', 'http_url', and optionally 'presigned_url'.
    """
    if not isinstance(local_zip_path, Path):
        local_zip_path = Path(local_zip_path)

    if not local_zip_path.exists():
        raise FileNotFoundError(f"Local zip not found: {local_zip_path}")

    default_bucket, default_prefix = get_recordings_bucket_and_prefix()
    bucket_name = bucket or default_bucket
    if not bucket_name:
        raise ValueError("S3_RECORDINGS_BUCKET not configured (env or PLATFORM_CONFIG) and no bucket provided")

    if key_prefix is not None:
        prefix = key_prefix.strip("/")
    else:
        prefix = default_prefix
    key = f"{prefix}/{local_zip_path.name}" if prefix else local_zip_path.name

    s3_client = _get_s3_client()

    logger.info(f"Uploading recording to S3: bucket={bucket_name}, key={key}")
    try:
        s3_client.upload_file(str(local_zip_path), bucket_name, key)
    except (BotoCoreError, ClientError) as e:
        logger.error(f"Failed to upload to S3: {e}")
        raise

    http_url = _build_http_url(bucket_name, key)
    s3_url = f"s3://{bucket_name}/{key}"

    result = {
        "s3_url": s3_url,
        "http_url": http_url,
    }

    if create_presigned:
        exp = (
            expires_seconds
            if expires_seconds is not None
            else int(PLATFORM_CONFIG.get("S3_PRESIGNED_EXPIRES_SECONDS", 7200))
        )
        try:
            presigned = s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket_name, "Key": key},
                ExpiresIn=exp,
            )
            result["presigned_url"] = presigned
        except (BotoCoreError, ClientError) as e:
            # Not fatal; still return http_url/s3_url
            logger.warning(f"Could not create presigned URL: {e}")

    return result

