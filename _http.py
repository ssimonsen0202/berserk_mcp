"""Shared hardened HTTP client primitives for berserk-mcp.

All outbound callers use one URL policy, a redirect-blocking opener, bounded
response reads, and validated headers. The module is deliberately stdlib-only.
"""

import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_STATUS_RESPONSE_BYTES = 64 * 1024
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class UrlPolicyError(ValueError):
    """Raised when an outbound endpoint violates the shared URL policy."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into HTTPError instead of forwarding credentials."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirectHandler)


def is_loopback_host(host):
    if not host:
        return False
    if str(host).lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_http_url(url, *, label="endpoint",
                      allow_plaintext_remote=None):
    """Validate an absolute HTTP(S) URL and its plaintext transport policy.

    ``allow_plaintext_remote=None`` uses the explicit
    ``BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE=1`` operator opt-in. Pass ``False``
    for call sites such as OTLP that require TLS for every remote host.
    """
    if not isinstance(url, str) or not url.strip():
        raise UrlPolicyError(f"{label} url must be a non-empty string")
    if any(ord(char) < 32 or ord(char) == 127 or char in " \t\r\n" for char in url):
        raise UrlPolicyError(f"{label} url contains invalid control characters")
    try:
        parsed = urllib.parse.urlsplit(url)
        # Accessing port makes malformed/non-numeric ports fail here.
        parsed.port
    except ValueError as exc:
        raise UrlPolicyError(f"{label} url is malformed: {exc}") from None
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlPolicyError(
            f"{label} url scheme must be one of {sorted(ALLOWED_SCHEMES)}"
        )
    if not parsed.netloc or not parsed.hostname:
        raise UrlPolicyError(f"{label} url missing host")
    if parsed.username is not None or parsed.password is not None:
        raise UrlPolicyError(f"{label} url must not contain embedded credentials")
    if parsed.fragment:
        raise UrlPolicyError(f"{label} url must not contain a fragment")
    env_controlled_plaintext = allow_plaintext_remote is None
    if env_controlled_plaintext:
        allow_plaintext_remote = (
            os.environ.get("BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE") == "1"
        )
    if scheme == "http" and not is_loopback_host(parsed.hostname):
        if not allow_plaintext_remote:
            suffix = (
                "; use https, point at localhost/127.0.0.1, or set "
                     "BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE=1 to explicitly allow "
                     "it on a trusted private network"
                if env_controlled_plaintext
                else "; use https or a loopback endpoint"
            )
            raise UrlPolicyError(
                "plaintext http to a non-loopback host is rejected by default "
                "(credentials would cross the network unencrypted)" + suffix
            )
    return url


def _validated_headers(headers, *, force_json=False):
    clean = {}
    for raw_key, raw_value in dict(headers or {}).items():
        key = str(raw_key).strip()
        value = str(raw_value).strip()
        if not key or not _HEADER_NAME_RE.fullmatch(key):
            raise ValueError(f"invalid HTTP header name: {key!r}")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f"HTTP header {key!r} contains control characters")
        if force_json and key.lower() == "content-type":
            continue
        clean[key] = value
    if force_json:
        clean["Content-Type"] = "application/json"
    return clean


def parse_header_items(raw, *, force_json=True):
    """Parse comma-separated ``name=value`` headers, failing on any typo."""
    headers = {}
    for raw_item in str(raw or "").split(","):
        item = raw_item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"malformed HTTP header item {item!r}; expected name=value")
        key, value = item.split("=", 1)
        key = key.strip()
        if force_json and key.lower() == "content-type":
            continue
        headers[key] = value.strip()
    return _validated_headers(headers, force_json=force_json)


def read_bounded(response, cap=MAX_RESPONSE_BYTES):
    body = response.read(int(cap) + 1)
    if len(body) > int(cap):
        raise ValueError(f"response body exceeds {int(cap)} bytes")
    return body


def read_bounded_json(response, cap=MAX_RESPONSE_BYTES):
    return json.loads(read_bounded(response, cap).decode("utf-8"))


def request_json(url, headers, payload=None, *, method="POST", timeout=120,
                 label="endpoint", allow_plaintext_remote=None,
                 cap=MAX_RESPONSE_BYTES):
    """Issue one no-redirect JSON request and return its parsed response."""
    validate_http_url(
        url, label=label, allow_plaintext_remote=allow_plaintext_remote,
    )
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=_validated_headers(headers, force_json=payload is not None),
    )
    with NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
        return read_bounded_json(response, cap)


def http_post_json(url, headers, payload, timeout=120):
    """Compatibility contract: return ``(json, None)`` or ``(None, error)``."""
    try:
        return request_json(url, headers, payload, timeout=timeout), None
    except UrlPolicyError as exc:
        return None, f"invalid endpoint: {exc}"
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return None, f"HTTP {code}"
    except urllib.error.URLError:
        return None, "connection failed"
    except ValueError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, type(exc).__name__


def http_get_json(url, headers, timeout=120):
    try:
        return request_json(
            url, headers, None, method="GET", timeout=timeout,
        ), None
    except UrlPolicyError as exc:
        return None, f"invalid endpoint: {exc}"
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return None, f"HTTP {code}"
    except urllib.error.URLError:
        return None, "connection failed"
    except ValueError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, type(exc).__name__


def post_bytes_status(url, headers, data, *, timeout=15, label="endpoint",
                      allow_plaintext_remote=None,
                      cap=MAX_STATUS_RESPONSE_BYTES):
    """POST bytes, reject redirects, bound the response, and return status."""
    validate_http_url(
        url, label=label, allow_plaintext_remote=allow_plaintext_remote,
    )
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers=_validated_headers(headers),
    )
    try:
        with NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            read_bounded(response, cap)
            return int(response.status)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise
