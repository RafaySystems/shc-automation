"""
lib/aws/s3_presign.py

Turns a plain S3 object URL into a short-lived presigned GET URL, so the
controller nodes (which have no AWS credentials) can download a package from
a private bucket. Signing happens on the test runner (Jenkins agent or a
laptop) with the AWS credentials it already has, right before each download.

    url = presign_s3_url(pkg.url)          # sign just before the download
    print(redact_presigned(output))        # never log the signature

Behaviour:
  * The link is signed for the bucket's REGIONAL endpoint
    (https://<bucket>.s3.<region>.amazonaws.com/...). boto3's default global
    endpoint produces a signature that fails for buckets outside us-east-1.
  * Falls back to the plain URL (with a warning) when presigning is switched
    off, the URL isn't an S3 URL, it is already signed, no AWS credentials are
    available, or the object isn't readable with them. Public buckets keep
    working exactly as before.
  * The returned URL is never printed; only the plain s3://bucket/key is.

Environment (optional):
  SHC_S3_PRESIGN          "true" (default) / "false" to switch presigning off
  SHC_S3_PRESIGN_EXPIRES  link lifetime in seconds, default 2400 (40 mins)
"""

import os
import re
from typing import Optional, Tuple
from urllib.parse import unquote, urlparse

DEFAULT_EXPIRES = 2400  # 40 min -- signed right before each download, so this only has to outlast one aria2c run (1800s timeout)

_REDACT_RE = re.compile(r"\?[^\s'\"<>]*X-Amz-(?:Signature|Credential|Algorithm)[^\s'\"<>]*")

# <bucket>.s3.<region>.amazonaws.com | <bucket>.s3-<region>.amazonaws.com | <bucket>.s3.amazonaws.com
_VHOST_RE = re.compile(r"^(?P<bucket>.+?)\.s3(?:[.-](?P<region>[a-z0-9-]+))?\.amazonaws\.com$")
# s3.<region>.amazonaws.com | s3-<region>.amazonaws.com | s3.amazonaws.com  (path-style)
_PATH_RE = re.compile(r"^s3(?:[.-](?P<region>[a-z0-9-]+))?\.amazonaws\.com$")


def _log(msg: str):
    print(f"[s3-presign] {msg}", flush=True)


def redact_presigned(text):
    """Replace the query string of any presigned URL in `text` with a marker,
    so failure messages and report attachments never carry a live link."""
    if not text:
        return text
    return _REDACT_RE.sub("?<presigned-url-redacted>", text)


def is_presigned(url: str) -> bool:
    q = urlparse(url).query
    return "X-Amz-Signature=" in q or ("Signature=" in q and "AWSAccessKeyId=" in q)


def parse_s3_url(url: str) -> Optional[Tuple[str, Optional[str], str]]:
    """Return (bucket, region_or_None, key) for an S3 object URL, else None.
    Region is None for global-endpoint / s3:// URLs and is looked up later."""
    p = urlparse(url)
    if p.scheme == "s3":
        key = unquote(p.path.lstrip("/"))
        return (p.netloc, None, key) if p.netloc and key else None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    host = p.hostname.lower()
    region = None
    m = _VHOST_RE.match(host)
    if m:
        bucket, region = m.group("bucket"), m.group("region")
        key = unquote(p.path.lstrip("/"))
    else:
        m = _PATH_RE.match(host)
        if not m:
            return None
        region = m.group("region")
        parts = p.path.lstrip("/").split("/", 1)
        if len(parts) != 2:
            return None
        bucket, key = parts[0], unquote(parts[1])
    if region in (None, "external-1"):
        region = None
    if not bucket or not key:
        return None
    return bucket, region, key


def _enabled() -> bool:
    return os.environ.get("SHC_S3_PRESIGN", "true").strip().lower() not in ("0", "false", "no", "off")


def _expires() -> int:
    raw = os.environ.get("SHC_S3_PRESIGN_EXPIRES", "").strip()
    if not raw:
        return DEFAULT_EXPIRES
    try:
        val = int(raw)
        if val > 0:
            return min(val, 604800)  # SigV4 hard limit: 7 days
    except ValueError:
        pass
    _log(f"WARNING: ignoring invalid SHC_S3_PRESIGN_EXPIRES={raw!r}, using {DEFAULT_EXPIRES}s")
    return DEFAULT_EXPIRES


def presign_s3_url(url: str, expires: Optional[int] = None, profile: Optional[str] = None) -> str:
    """Return a presigned GET URL for `url`, or `url` itself if it can't or
    shouldn't be signed (see module docstring). Never raises."""
    if not url:
        return url
    if not _enabled():
        _log("presigning disabled (SHC_S3_PRESIGN=false) -- using plain URL")
        return url
    if is_presigned(url):
        _log("URL is already presigned -- using it as-is")
        return url
    parsed = parse_s3_url(url)
    if not parsed:
        return url  # not an S3 URL (mirror, local file server, ...) -- nothing to sign
    bucket, region, key = parsed
    expires = expires or _expires()

    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        _log("WARNING: boto3 not installed -- using plain URL")
        return url

    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        if session.get_credentials() is None:
            _log(f"WARNING: no AWS credentials on this runner -- using plain URL for s3://{bucket}/{key}")
            return url

        if not region:
            loc = session.client("s3").get_bucket_location(Bucket=bucket).get("LocationConstraint")
            region = loc or "us-east-1"

        s3 = session.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
        # Check first, so a wrong key or missing s3:GetObject shows up here as
        # a clear warning instead of an opaque 403 from aria2c on the node.
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "?")
            _log(f"WARNING: s3://{bucket}/{key} not readable with the current AWS credentials "
                 f"({code}) -- check s3:GetObject permission and the object key; using plain URL")
            return url

        signed = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires,
        )
    except (BotoCoreError, ClientError, Exception) as e:  # never break a download over signing
        _log(f"WARNING: could not presign s3://{bucket}/{key} ({type(e).__name__}: "
             f"{redact_presigned(str(e))}) -- using plain URL")
        return url

    _log(f"s3://{bucket}/{key} ({region}) -> presigned URL, valid {expires // 60} min")
    return signed
