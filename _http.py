"""Shared hardened HTTP client primitives for berserk-mcp.

All outbound callers use one URL policy, a redirect-blocking opener, bounded
response reads, and validated headers. The module is deliberately stdlib-only.
"""

import http.client
import ipaddress
import json
import os
import re
import socket
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


class EgressRefused(OSError):
    """A connection refused by the egress policy at connect time. urllib wraps
    it in URLError; callers report it as a policy refusal, not a network error."""


# ---------- egress policy ----------
# Opt-in destination policy for every outbound integration (LLM providers,
# Hermes, CanonLoom, Discord, OTLP, the quota endpoint). Inactive by default:
# validate_http_url's scheme, redirect and plaintext rules still apply, and an
# operator may point integrations at any host. BERSERK_LOCAL_ONLY or an
# allowlist activates it: then only loopback, hosts listed by name, and
# addresses inside the listed networks are reachable.
# Cloud LLM API hosts. Under BERSERK_LOCAL_ONLY they are refused even if an
# allowlist names them: local-only means no cloud generation, for the server's
# provider ladder and the eval harness alike.
CLOUD_LLM_HOSTS = frozenset({"api.openai.com", "api.anthropic.com", "openrouter.ai"})
_EGRESS_ALLOWED_HOSTS_ENV = "BERSERK_EGRESS_ALLOWED_HOSTS"
_EGRESS_ALLOWED_CIDRS_ENV = "BERSERK_EGRESS_ALLOWED_CIDRS"


def local_only_enabled():
    """True when BERSERK_LOCAL_ONLY is set: cloud LLM providers are refused
    even if their API keys are present, and the egress policy is active."""
    return os.environ.get("BERSERK_LOCAL_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}


def egress_allowed_hosts():
    raw = os.environ.get("BERSERK_EGRESS_ALLOWED_HOSTS", "")
    return {_strip_dot(part.strip().lower()) for part in raw.split(",") if part.strip()}


def egress_allowed_networks():
    raw = os.environ.get("BERSERK_EGRESS_ALLOWED_CIDRS", "")
    networks = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            network = ipaddress.ip_network(part, strict=False)
        except ValueError as exc:
            raise UrlPolicyError(f"{_EGRESS_ALLOWED_CIDRS_ENV} contains invalid CIDR {part!r}") from exc
        if network.prefixlen == 0:
            raise UrlPolicyError(f"{_EGRESS_ALLOWED_CIDRS_ENV} must not allow everything ({part!r})")
        networks.append(network)
    return networks


def egress_policy_active():
    return local_only_enabled() or bool(egress_allowed_hosts()) or bool(egress_allowed_networks())


def _strip_dot(host):
    host = str(host or "")
    return host.removesuffix(".")


def validate_egress_destination(url, *, label="endpoint"):
    """Refuse a destination the active egress policy does not allow.

    Decided here by name: loopback, a host listed in BERSERK_EGRESS_ALLOWED_HOSTS,
    or an IP literal inside BERSERK_EGRESS_ALLOWED_CIDRS. Any other hostname is
    allowed only when networks are configured, and then its resolved addresses
    are checked at connect time (_checked_candidates), so DNS cannot move it."""
    if not isinstance(url, str) or not url.strip():
        raise UrlPolicyError(f"{label} url must be a non-empty string")
    try:
        host = _strip_dot((urllib.parse.urlsplit(url).hostname or "").lower())
    except ValueError as exc:
        raise UrlPolicyError(f"{label} url is malformed: {exc}") from None
    if is_loopback_host(host) or not egress_policy_active():
        return
    if local_only_enabled() and any(host == h or host.endswith("." + h) for h in CLOUD_LLM_HOSTS):
        raise UrlPolicyError(f"{label} destination {host!r} is a cloud LLM API, refused by BERSERK_LOCAL_ONLY")
    if host in egress_allowed_hosts():
        return
    networks = egress_allowed_networks()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if any(ip in network for network in networks):
            return
    elif networks:
        return  # checked against the networks after resolution, at connect time
    raise UrlPolicyError(
        f"{label} destination {host!r} is not loopback and is not allowed by "
        f"{_EGRESS_ALLOWED_HOSTS_ENV} or {_EGRESS_ALLOWED_CIDRS_ENV}"
        + (" (BERSERK_LOCAL_ONLY is set)" if local_only_enabled() else "")
    )


def _address(sockaddr):
    return ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])


def _checked_candidates(host, port):
    """Resolve `host` once and return the getaddrinfo candidates this
    connection may use; raise EgressRefused when none may be used.

    - A host the URL policy treated as loopback must resolve only to loopback
      addresses, so plaintext and credentials allowed "on this machine" stay on it.
    - Under an active egress policy, a host not approved by name must resolve
      inside BERSERK_EGRESS_ALLOWED_CIDRS; addresses outside are dropped.
    The connection then uses exactly these addresses: no second lookup, so a
    DNS answer that changes between check and connect cannot redirect it."""
    candidates = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    name = _strip_dot(str(host).lower())
    if is_loopback_host(name):
        if not all(_address(c[4]).is_loopback for c in candidates):
            raise EgressRefused(f"{name!r} resolves to a non-loopback address")
        return candidates
    if not egress_policy_active() or name in egress_allowed_hosts():
        return candidates
    networks = egress_allowed_networks()
    allowed = [c for c in candidates if any(_address(c[4]) in network for network in networks)]
    if not allowed:
        raise EgressRefused(f"{name!r} resolves outside {_EGRESS_ALLOWED_CIDRS_ENV}")
    return allowed


class _PinnedConnectionMixin:
    """Connect only to the addresses _checked_candidates approved. self.host is
    left unchanged, so the Host header and TLS SNI/certificate check still use
    the original name. A per-instance _create_connection, not a patched
    socket.getaddrinfo, so concurrent requests on other threads are unaffected."""

    def connect(self):
        candidates = _checked_candidates(self.host, self.port)

        def create_connection(address, timeout=None, source_address=None, *args, **kwargs):
            errors = []
            for family, socktype, proto, _canonname, sockaddr in candidates:
                sock = None
                try:
                    sock = socket.socket(family, socktype, proto)
                    if timeout is not None:
                        sock.settimeout(timeout)
                    if source_address:
                        sock.bind(source_address)
                    sock.connect(sockaddr)
                    return sock
                except OSError as exc:
                    errors.append(exc)
                    if sock is not None:
                        sock.close()
            raise errors[-1] if errors else OSError(f"no usable address for {self.host!r}")

        self._create_connection = create_connection
        super().connect()


class _PinnedHTTPConnection(_PinnedConnectionMixin, http.client.HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnectionMixin, http.client.HTTPSConnection):
    pass


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


def _build_opener(*, proxies):
    handlers = [NoRedirectHandler, _PinnedHTTPHandler, _PinnedHTTPSHandler]
    if not proxies:
        handlers.insert(0, urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


NO_REDIRECT_OPENER = _build_opener(proxies=True)
# Loopback requests never go through a proxy: plaintext is allowed to loopback on
# the promise that the bytes stay on this host, and an ambient http_proxy would
# carry them, and their Authorization header, off it.
LOOPBACK_OPENER = _build_opener(proxies=False)
# Under an active egress policy nothing goes through an ambient proxy either: a
# proxy would hide the real destination from the connect-time address check.
POLICY_OPENER = LOOPBACK_OPENER


def _opener_for(url):
    host = urllib.parse.urlsplit(url).hostname
    if is_loopback_host(host) or egress_policy_active():
        return LOOPBACK_OPENER
    return NO_REDIRECT_OPENER


def is_loopback_host(host):
    if not host:
        return False
    # One trailing dot is the fully qualified spelling of the same name
    # ("localhost.", "127.0.0.1.").
    host = str(host)[:-1] if str(host).endswith(".") else host
    if str(host).lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_http_url(url, *, label="endpoint", allow_plaintext_remote=None):
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
        _ = parsed.port  # noqa: B018 — side-effect: raises ValueError on malformed port
    except ValueError as exc:
        raise UrlPolicyError(f"{label} url is malformed: {exc}") from None
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlPolicyError(f"{label} url scheme must be one of {sorted(ALLOWED_SCHEMES)}")
    if not parsed.netloc or not parsed.hostname:
        raise UrlPolicyError(f"{label} url missing host")
    if parsed.username is not None or parsed.password is not None:
        raise UrlPolicyError(f"{label} url must not contain embedded credentials")
    if parsed.fragment:
        raise UrlPolicyError(f"{label} url must not contain a fragment")
    env_controlled_plaintext = allow_plaintext_remote is None
    if env_controlled_plaintext:
        allow_plaintext_remote = os.environ.get("BERSERK_LLM_ALLOW_PLAINTEXT_REMOTE") == "1"
    if scheme == "http" and not is_loopback_host(parsed.hostname) and not allow_plaintext_remote:
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


def request_json(
    url,
    headers,
    payload=None,
    *,
    method="POST",
    timeout=120,
    label="endpoint",
    allow_plaintext_remote=None,
    cap=MAX_RESPONSE_BYTES,
):
    """Issue one no-redirect JSON request and return its parsed response."""
    validate_http_url(
        url,
        label=label,
        allow_plaintext_remote=allow_plaintext_remote,
    )
    validate_egress_destination(url, label=label)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=_validated_headers(headers, force_json=payload is not None),
    )
    with _opener_for(url).open(request, timeout=timeout) as response:
        return read_bounded_json(response, cap)


def http_post_json(url, headers, payload, timeout=120, *, allow_plaintext_remote=None):
    """Compatibility contract: return ``(json, None)`` or ``(None, error)``.

    ``allow_plaintext_remote=None`` applies the LLM plaintext opt-in; OTLP and
    CanonLoom callers pass ``False`` so that opt-in cannot weaken them."""
    try:
        return request_json(url, headers, payload, timeout=timeout, allow_plaintext_remote=allow_plaintext_remote), None
    except UrlPolicyError as exc:
        return None, f"invalid endpoint: {exc}"
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return None, f"HTTP {code}"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, EgressRefused):
            return None, f"invalid endpoint: {exc.reason}"
        return None, "connection failed"
    except ValueError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, type(exc).__name__


def http_get_json(url, headers, timeout=120, *, allow_plaintext_remote=None):
    try:
        return request_json(
            url,
            headers,
            None,
            method="GET",
            timeout=timeout,
            allow_plaintext_remote=allow_plaintext_remote,
        ), None
    except UrlPolicyError as exc:
        return None, f"invalid endpoint: {exc}"
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return None, f"HTTP {code}"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, EgressRefused):
            return None, f"invalid endpoint: {exc.reason}"
        return None, "connection failed"
    except ValueError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, type(exc).__name__


def post_bytes_status(
    url, headers, data, *, timeout=15, label="endpoint", allow_plaintext_remote=None, cap=MAX_STATUS_RESPONSE_BYTES
):
    """POST bytes, reject redirects, bound the response, and return status."""
    validate_http_url(
        url,
        label=label,
        allow_plaintext_remote=allow_plaintext_remote,
    )
    validate_egress_destination(url, label=label)
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers=_validated_headers(headers),
    )
    try:
        with _opener_for(url).open(request, timeout=timeout) as response:
            read_bounded(response, cap)
            return int(response.status)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise
