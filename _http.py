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


class _PinnedConnectionMixin:
    """Mixin for http.client.HTTPConnection/HTTPSConnection that closes a
    DNS-rebinding TOCTOU window: validate_egress_destination() approves a
    BERSERK_EGRESS_ALLOWED_HOSTS entry by comparing the URL's hostname as
    a STRING, then (without this) urllib performs its own, entirely
    separate DNS resolution moments later when actually connecting -- if
    the name's DNS record changes in between (an attacker-controlled
    domain, or any DNS record that simply isn't stable), the connection
    can land on an address nobody approved.

    Resolves the hostname via getaddrinfo() exactly ONCE per connection --
    inside connect(), using self.host/self.port -- and pins the actual TCP
    connect() to exactly that one resolution's candidate list, replayed
    verbatim, instead of letting a second, independent resolution happen.
    self.host itself is left completely UNCHANGED, so the Host header
    (HTTPConnection) and SNI/certificate hostname check (HTTPSConnection,
    via self._context.wrap_socket(..., server_hostname=self.host), which
    runs AFTER this connect() via HTTPSConnection's own unmodified
    connect() body) still correctly reference the ORIGINAL, approved
    hostname -- only which address the raw socket connects to is pinned.
    Verified end to end against a real local HTTP and HTTPS (self-signed
    cert, SNI, hostname-verified) server before landing this, including
    the IPv4/IPv6 fallback case (a real 'localhost' resolution returning
    both an AAAA and an A record, first candidate refused, correctly
    falling back to the second) and an unresolvable-hostname case
    (confirmed to still surface as urllib.error.URLError, matching
    pre-pinning behavior exactly, not a new exception type/message).

    Overriding self._create_connection (a per-INSTANCE attribute) rather
    than monkeypatching the module-level socket.getaddrinfo was a
    deliberate choice: this server is multithreaded (outbound Hermes/
    CanonLoom/Discord/OTLP calls can happen from different request-
    handling threads at the same time), and even a narrowly-scoped global
    monkeypatch would race against another thread's concurrent, unrelated
    getaddrinfo() call -- potentially handing one thread's pre-resolved
    address to a completely different hostname's connection. A per-
    instance attribute has no shared mutable state at all.

    The connect-loop below reimplements socket.create_connection()'s own
    multi-address fallback logic (verified against the installed stdlib
    via inspect.getsource) rather than reusing it directly, since
    create_connection() always calls getaddrinfo() itself -- there is no
    stdlib hook to hand it a pre-resolved candidate list instead."""

    def connect(self):
        candidates = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)

        def _pinned_create_connection(address, timeout, source_address):
            exceptions = []
            for af, socktype, proto, _canonname, sockaddr in candidates:
                sock = None
                try:
                    sock = socket.socket(af, socktype, proto)
                    sock.settimeout(timeout)
                    if source_address:
                        sock.bind(source_address)
                    sock.connect(sockaddr)
                    return sock
                except OSError as exc:
                    exceptions.append(exc)
                    if sock is not None:
                        sock.close()
            if exceptions:
                raise exceptions[-1]
            raise OSError(f"getaddrinfo returned no usable candidates for {self.host!r}")

        self._create_connection = _pinned_create_connection
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


# build_opener() replaces the default HTTPHandler/HTTPSHandler with these
# subclasses (it skips a default handler class whenever a subclass of it
# is passed in) -- every existing caller of NO_REDIRECT_OPENER gets
# connection pinning automatically, with no other code change required.
#
# urllib.request.ProxyHandler({}) (round-9 adversarial-review finding):
# build_opener()'s own default handler list always includes a ProxyHandler
# instantiated with proxies=None, which falls back to getproxies() --
# reading HTTP_PROXY/HTTPS_PROXY (or the OS proxy config) from the
# environment. Without overriding it, ANY outbound call through this
# opener -- including one whose LOGICAL destination is loopback or an
# explicitly BERSERK_EGRESS_ALLOWED_HOSTS-approved host -- gets silently
# rerouted to the configured proxy host if one is set: the connection
# pinning above pins resolution of self.host, but for a proxied request
# self.host at connect() time IS the proxy, not the logical destination,
# so pinning does nothing to stop this, and validate_egress_destination()
# never sees the proxy host at all since it only validates the logical
# URL. This would let BERSERK_LOCAL_ONLY=1 -- an explicit "no unapproved
# outbound destination" guarantee -- be defeated by an inherited/ambient
# HTTP_PROXY env var an operator set for an unrelated purpose. Passing an
# explicit ProxyHandler({}) instance here (not the class) makes
# build_opener() skip installing its own environment-reading default
# (isinstance() match in its skip logic), and an empty proxies mapping
# means this instance itself registers no *_open methods and adds nothing
# -- so no proxy handler is present at all, and every request the shape of
# self.host at connect() time always matches the caller's own logical URL.
NO_REDIRECT_OPENER = urllib.request.build_opener(
    NoRedirectHandler, urllib.request.ProxyHandler({}),
    _PinnedHTTPHandler, _PinnedHTTPSHandler,
)


def is_loopback_host(host):
    if not host:
        return False
    if str(host).lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_only_enabled():
    """True when the operator has enabled BERSERK_LOCAL_ONLY.

    When enabled, cloud LLM providers (OpenAI, Anthropic) must be refused by
    every call site regardless of whether their API keys happen to be set in
    the environment -- an inherited credential must not silently re-enable
    cloud fallback. See task-01-local-egress-and-side-effects.md.
    """
    return os.environ.get("BERSERK_LOCAL_ONLY", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


_EGRESS_ALLOWED_HOSTS_ENV = "BERSERK_EGRESS_ALLOWED_HOSTS"
_EGRESS_ALLOWED_CIDRS_ENV = "BERSERK_EGRESS_ALLOWED_CIDRS"


def _egress_allowed_hosts():
    # Literal string (not _EGRESS_ALLOWED_HOSTS_ENV) so this read is
    # discoverable by tests.EnvExampleDriftTest's static os.environ.get scan.
    raw = os.environ.get("BERSERK_EGRESS_ALLOWED_HOSTS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _egress_allowed_networks():
    # Literal string, same reason as _egress_allowed_hosts above.
    raw = os.environ.get("BERSERK_EGRESS_ALLOWED_CIDRS", "")
    networks = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError as exc:
            raise UrlPolicyError(
                f"{_EGRESS_ALLOWED_CIDRS_ENV} contains invalid CIDR {part!r}"
            ) from exc
    return networks


def validate_egress_destination(url, *, label="endpoint"):
    """Enforce the operator's approved-destination policy for one outbound
    integration call (Hermes, CanonLoom, Discord, or a future integration).

    Loopback is always allowed -- the default and most common deployment is
    a local sidecar process. The destination policy itself is opt-in: it
    activates when BERSERK_LOCAL_ONLY is enabled (a sovereignty-constrained
    deployment must reject an unapproved non-loopback destination, not just
    cloud LLM providers) or when the operator has explicitly configured
    BERSERK_EGRESS_ALLOWED_HOSTS/_CIDRS. Without either, a non-loopback
    destination is unrestricted here -- validate_http_url's scheme/redirect/
    plaintext protections still apply -- preserving the existing supported
    case of pointing Hermes/CanonLoom at an operator-chosen remote host.
    When the policy is active, a non-loopback host must be an exact match
    in BERSERK_EGRESS_ALLOWED_HOSTS or fall inside a BERSERK_EGRESS_ALLOWED_CIDRS
    network, else it fails closed rather than silently sending data there.

    Several callers (doctor's reachability checks, the startup egress-policy
    summary) call this directly on operator-supplied configuration --
    CANONLOOM_SERVER_URL, a saved/derived Hermes URL -- without first routing
    through validate_http_url()'s own parse step. A malformed URL (e.g. an
    invalid bracketed IPv6 host) makes urlsplit()/.hostname raise ValueError,
    which must not escape as an unhandled exception and abort the caller
    (server startup, in the summary's case) over what is, for every one of
    those callers, an optional integration. Parse failures are therefore
    folded into the same fail-closed UrlPolicyError as an explicitly
    disallowed destination.

    Round-10 adversarial-review finding: a locally persisted config file
    (llm_config.json, hand-editable, not just written through
    save_hermes_url's own validation) can hold a non-string value for
    "hermes_url" -- an int, dict, or list survive JSON parsing fine and
    _hermes_url()'s `or` chain happily returns one if it's truthy. That
    used to reach urlsplit() as a non-string argument, raising
    AttributeError/TypeError instead of ValueError -- not caught by the
    fix above, so it still crashed the same startup call site. Checked
    explicitly up front, the same way validate_http_url() already checks
    its own url argument, rather than only widening the except clause --
    an explicit isinstance check documents the actual contract (this
    function requires a URL string) instead of leaving readers to infer it
    from which exception types happen to be caught.
    """
    if not isinstance(url, str) or not url.strip():
        raise UrlPolicyError(f"{label} url must be a non-empty string")
    try:
        hostname = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError as exc:
        raise UrlPolicyError(f"{label} url is malformed: {exc}") from None
    if is_loopback_host(hostname):
        return
    allowed_hosts = _egress_allowed_hosts()
    allowed_networks = _egress_allowed_networks()
    if not (local_only_enabled() or allowed_hosts or allowed_networks):
        return
    if hostname in allowed_hosts:
        return
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None and any(ip in network for network in allowed_networks):
        return
    raise UrlPolicyError(
        f"{label} destination {hostname!r} is not loopback and is not listed in "
        f"{_EGRESS_ALLOWED_HOSTS_ENV} or {_EGRESS_ALLOWED_CIDRS_ENV}"
    )


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
    validate_egress_destination(url, label=label)
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
    validate_egress_destination(url, label=label)
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
