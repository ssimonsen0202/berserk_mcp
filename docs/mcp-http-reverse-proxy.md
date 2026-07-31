# MCP HTTP transport and reverse proxy deployment

berserk-mcp's default transport is stdio. The optional HTTP transport is
disabled by default and should normally be exposed through a local reverse
proxy, not directly to the internet.

Recommended production shape:

```text
MCP client -> HTTPS/mTLS reverse proxy -> http://127.0.0.1:8765/mcp -> berserk-mcp
```

## Safe local HTTP baseline

Start with loopback-only HTTP:

```sh
export BERSERK_MCP_HTTP_ENABLE=1
export BERSERK_MCP_HTTP_BIND=127.0.0.1:8765
export BERSERK_MCP_HTTP_AUTH_TOKEN='<long-random-token>'
export BERSERK_MCP_HTTP_ALLOW_CIDRS=127.0.0.1/32,::1/128
python3 berserk_mcp.py --http
```

Do not bind to `0.0.0.0` unless you have explicitly configured the remote
controls below.

## Required controls for remote bind

For a non-loopback bind, all of these are required:

```sh
export BERSERK_MCP_HTTP_ENABLE=1
export BERSERK_MCP_HTTP_BIND=0.0.0.0:8765
export BERSERK_MCP_HTTP_ALLOW_REMOTE=1
export BERSERK_MCP_HTTP_AUTH_TOKEN='<long-random-token>'
export BERSERK_MCP_HTTP_ALLOWED_HOSTS=mcp.internal.example.com
export BERSERK_MCP_HTTP_ALLOW_CIDRS=100.64.0.0/10
```

Startup validation rejects global allow-all CIDRs such as `0.0.0.0/0` and
`::/0`.

## Caddy example

Terminate TLS in Caddy and forward to loopback:

```caddyfile
mcp.internal.example.com {
  encode zstd gzip

  reverse_proxy 127.0.0.1:8765 {
    header_up Host {host}
    header_up X-Forwarded-For {remote_host}
    header_up X-Forwarded-Proto {scheme}
  }
}
```

berserk-mcp config:

```sh
export BERSERK_MCP_HTTP_ENABLE=1
export BERSERK_MCP_HTTP_BIND=127.0.0.1:8765
export BERSERK_MCP_HTTP_AUTH_TOKEN='<long-random-token>'
export BERSERK_MCP_HTTP_ALLOWED_HOSTS=mcp.internal.example.com
export BERSERK_MCP_HTTP_ALLOW_CIDRS=127.0.0.1/32,::1/128
export BERSERK_MCP_HTTP_USE_FORWARDED_FOR=1
export BERSERK_MCP_HTTP_TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128
```

## nginx example

```nginx
server {
    listen 443 ssl http2;
    server_name mcp.internal.example.com;

    ssl_certificate     /etc/ssl/certs/mcp.internal.example.com.crt;
    ssl_certificate_key /etc/ssl/private/mcp.internal.example.com.key;

    client_max_body_size 1m;

    location /mcp {
        proxy_pass http://127.0.0.1:8765/mcp;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /healthz {
        proxy_pass http://127.0.0.1:8765/healthz;
        proxy_set_header Host $host;
    }
}
```

## mTLS at the proxy layer

Prefer mTLS at the proxy layer when machine-to-machine client identity is
required. Example nginx directives:

```nginx
ssl_client_certificate /etc/ssl/certs/internal-client-ca.pem;
ssl_verify_client on;
```

Keep berserk-mcp's bearer token enabled behind mTLS. mTLS authenticates the
client certificate; the bearer token remains a defense-in-depth application
secret.

## Forwarded headers

`X-Forwarded-For` is ignored unless both settings are enabled:

```sh
export BERSERK_MCP_HTTP_USE_FORWARDED_FOR=1
export BERSERK_MCP_HTTP_TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128
```

Requests from untrusted peers cannot spoof their source with
`X-Forwarded-For`; berserk-mcp uses the socket peer IP instead.

## Operational notes

- Only `POST /mcp` accepts JSON-RPC requests.
- `GET /healthz` returns only `{"status":"ok"}`.
- CORS is not enabled.
- Request bodies are not logged.
- Authorization tokens are not logged.
- Use HTTPS/TLS for every non-loopback deployment.
