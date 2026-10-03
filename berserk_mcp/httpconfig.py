"""Parse and check the HTTP transport settings.

Split out of berserk_mcp.py in v1.37.0.
"""

from berserk_mcp import config as bm_config
from urllib.parse import urlsplit
import hmac
import ipaddress
import re
import threading


class HttpConfigError(ValueError):
    pass


def _parse_http_bind(bind):
    text = str(bind or "").strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        if not rest.startswith(":"):
            raise HttpConfigError("BERSERK_MCP_HTTP_BIND must be host:port")
        port_text = rest[1:]
    else:
        if ":" not in text:
            raise HttpConfigError("BERSERK_MCP_HTTP_BIND must be host:port")
        host, port_text = text.rsplit(":", 1)
    host = host.strip()
    if not host:
        raise HttpConfigError("BERSERK_MCP_HTTP_BIND host is empty")
    try:
        port = int(port_text)
    except (TypeError, ValueError):
        raise HttpConfigError("BERSERK_MCP_HTTP_BIND port must be an integer") from None
    if not 1 <= port <= 65535:
        raise HttpConfigError("BERSERK_MCP_HTTP_BIND port must be 1..65535")
    return host, port


# Host header values a loopback-bound server accepts by default (issue #84).
_LOOPBACK_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _host_is_loopback(host):
    lowered = str(host or "").strip().lower()
    if lowered == "localhost":
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def _parse_cidr_list(raw, label, *, allow_empty=False, allow_global=False):
    text = str(raw or "").strip()
    if not text:
        if allow_empty:
            return []
        raise HttpConfigError(f"{label} must not be empty")
    networks = []
    for item in text.split(","):
        part = item.strip()
        if not part:
            continue
        try:
            network = ipaddress.ip_network(part, strict=False)
        except ValueError as exc:
            raise HttpConfigError(f"{label} contains invalid CIDR {part!r}") from exc
        if not allow_global and network.prefixlen == 0:
            raise HttpConfigError(f"{label} must not include global allow-all CIDR {part!r}")
        networks.append(network)
    if not networks and not allow_empty:
        raise HttpConfigError(f"{label} must not be empty")
    return networks


def _parse_host_list(raw, *, allow_empty=False):
    text = str(raw or "").strip()
    if not text:
        if allow_empty:
            return set()
        raise HttpConfigError("BERSERK_MCP_HTTP_ALLOWED_HOSTS must not be empty")
    hosts = set()
    for item in text.split(","):
        host = item.strip().lower()
        if not host:
            continue
        if any(ch in host for ch in "\r\n/\\"):
            raise HttpConfigError("BERSERK_MCP_HTTP_ALLOWED_HOSTS contains an invalid host")
        hosts.add(host)
    if not hosts and not allow_empty:
        raise HttpConfigError("BERSERK_MCP_HTTP_ALLOWED_HOSTS must not be empty")
    return hosts


# A Host header is "host[:port]": a DNS name or IPv4 address, or a bracketed
# IPv6 address. Anything else (userinfo, paths, spaces) is malformed.
_HOST_HEADER_RE = re.compile(r"^(?:\[([0-9a-f:.]+)\]|([a-z0-9.-]+))(?::\d{1,5})?$")


def _normalize_host_header(value):
    """The host part of a Host header, lower-cased and without one trailing
    dot; "" for a malformed value, which no allowlist contains."""
    match = _HOST_HEADER_RE.match(str(value or "").strip().lower())
    if not match:
        return ""
    host = match.group(1) or match.group(2)
    return host[:-1] if host.endswith(".") and not host.endswith("..") else host


def _http_peer_ip(handler):
    return str(handler.client_address[0])


def _ip_allowed(ip_text, networks):
    try:
        ip = ipaddress.ip_address(str(ip_text))
    except ValueError:
        return False
    return any(ip in network for network in networks)


def _http_effective_client_ip(handler, config):
    peer = _http_peer_ip(handler)
    if not config["use_forwarded_for"]:
        return peer
    if not _ip_allowed(peer, config["trusted_proxy_cidrs"]):
        return peer
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        try:
            ipaddress.ip_address(first)
            return first
        except ValueError:
            return peer
    return peer


def _build_http_config(
    *,
    enable=bm_config.HTTP_ENABLE,
    bind=bm_config.HTTP_BIND,
    allow_remote=bm_config.HTTP_ALLOW_REMOTE,
    auth_token=bm_config.HTTP_AUTH_TOKEN,
    allowed_hosts=bm_config.HTTP_ALLOWED_HOSTS,
    allow_cidrs=bm_config.HTTP_ALLOW_CIDRS,
    max_request_bytes=bm_config.HTTP_MAX_REQUEST_BYTES,
    max_concurrent_requests=bm_config.HTTP_MAX_CONCURRENT_REQUESTS,
    use_forwarded_for=bm_config.HTTP_USE_FORWARDED_FOR,
    trusted_proxy_cidrs=bm_config.HTTP_TRUSTED_PROXY_CIDRS,
):
    host, port = _parse_http_bind(bind)
    loopback = _host_is_loopback(host)
    remote = not loopback
    allowed = _parse_cidr_list(allow_cidrs, "BERSERK_MCP_HTTP_ALLOW_CIDRS")
    trusted = _parse_cidr_list(
        trusted_proxy_cidrs,
        "BERSERK_MCP_HTTP_TRUSTED_PROXY_CIDRS",
        allow_empty=not use_forwarded_for,
    )
    hosts = _parse_host_list(allowed_hosts, allow_empty=True)
    if loopback and not hosts:
        # Issue #84: an empty allowlist used to accept any Host header, so a
        # web page could reach a loopback server through DNS rebinding (its
        # own domain resolving to 127.0.0.1). A loopback bind now accepts only
        # the loopback names unless the operator lists hosts explicitly.
        hosts = set(_LOOPBACK_HOST_NAMES) | {host.lower()}
    if remote:
        if not allow_remote:
            raise HttpConfigError("non-loopback HTTP bind requires BERSERK_MCP_HTTP_ALLOW_REMOTE=1")
        if not str(auth_token or ""):
            raise HttpConfigError("non-loopback HTTP bind requires BERSERK_MCP_HTTP_AUTH_TOKEN")
        if not hosts:
            raise HttpConfigError("non-loopback HTTP bind requires BERSERK_MCP_HTTP_ALLOWED_HOSTS")
    if use_forwarded_for and not trusted:
        raise HttpConfigError("forwarded-header mode requires BERSERK_MCP_HTTP_TRUSTED_PROXY_CIDRS")
    if max_request_bytes <= 0:
        raise HttpConfigError("BERSERK_MCP_HTTP_MAX_REQUEST_BYTES must be positive")
    if max_concurrent_requests <= 0:
        raise HttpConfigError("BERSERK_MCP_HTTP_MAX_CONCURRENT_REQUESTS must be positive")
    return {
        "enabled": bool(enable),
        "host": host,
        "port": port,
        "remote": remote,
        "auth_token": str(auth_token or ""),
        "allowed_hosts": hosts,
        "allow_cidrs": allowed,
        "max_request_bytes": int(max_request_bytes),
        "semaphore": threading.BoundedSemaphore(int(max_concurrent_requests)),
        "use_forwarded_for": bool(use_forwarded_for),
        "trusted_proxy_cidrs": trusted,
    }


def _origin_host(value):
    """Host of an Origin header ("scheme://host[:port]"), or None when absent.
    "null" and anything unparseable yield "" so they are refused."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return (urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""


def _http_request_allowed(handler, config):
    if len(handler.headers.get_all("Host") or []) > 1:
        return False, 400, "multiple host headers"
    if len(handler.headers.get_all("Origin") or []) > 1:
        return False, 403, "multiple origin headers"
    host = _normalize_host_header(handler.headers.get("Host", ""))
    if config["allowed_hosts"] and host not in config["allowed_hosts"]:
        return False, 403, "host not allowed"
    # MCP Streamable HTTP: validate Origin. Browsers send it on cross-origin
    # requests; non-browser clients usually omit it and are unaffected.
    origin = _origin_host(handler.headers.get("Origin"))
    if origin is not None and config["allowed_hosts"] and origin not in config["allowed_hosts"]:
        return False, 403, "origin not allowed"
    client_ip = _http_effective_client_ip(handler, config)
    if not _ip_allowed(client_ip, config["allow_cidrs"]):
        return False, 403, "client ip not allowed"
    token = config["auth_token"]
    if token:
        supplied = handler.headers.get("Authorization", "")
        prefix = "Bearer "
        if not supplied.startswith(prefix) or not hmac.compare_digest(supplied[len(prefix) :], token):
            return False, 401, "unauthorized"
    return True, 200, "ok"
