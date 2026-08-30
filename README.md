# mcp-relay

A config-driven MCP gateway that proxies tool calls to remote MCP servers with built-in authentication, secret resolution, and a layered security pipeline.

```
LLM client  ──SSE──►  mcp-relay  ──HTTP──►  Remote MCP Server A
                           │      ──HTTP──►  Remote MCP Server B
                           │      ──HTTP──►  Remote MCP Server C
                     (auth · sanitize · PII · injection · rate-limit)
```

---

## Features

- **Config-driven** — add/remove upstream servers by editing `config/remote_servers.yaml`; no code changes
- **Six auth patterns** — none, query param, URL path, custom header, bearer token, OAuth2 client credentials
- **Pluggable secrets** — env vars (default), GCP Secret Manager, AWS Secrets Manager, Azure Key Vault, HashiCorp Vault
- **Security pipeline** — input/output sanitization, PII redaction (regex + optional Ollama LLM), prompt-injection detection
- **Tool blocklist** — globally suppress specific upstream tools via `config/security_policies.yaml`
- **Inbound auth** — optional bearer-token auth on the relay's own SSE endpoint
- **Dynamic tool discovery** — optional background refresh loop picks up new/removed upstream tools without restart
- **Registry endpoint** — `GET /registry` lists all servers, their available tools, and any blocked tools
- **Observability** — structured JSON logging and in-memory metrics exposed at `/metrics`
- **Rate limiting** — per-server daily quota with response-signal detection; persistent counters via Redis
- **Token caching** — OAuth2 tokens and resolved credentials cached in-memory (5-min TTL)
- **Non-root container** — multi-stage Dockerfile, runs as uid 1000

---

## Project structure

```
mcp-relay/
├── src/mcp_relay/          # application code
│   ├── main.py             # Starlette app entrypoint
│   ├── server.py           # FastMCP instance
│   ├── auth/               # credential resolution and caching
│   │   ├── resolver.py     # resolve_connection() — picks auth strategy
│   │   ├── oauth2.py       # OAuth2 client credentials + token cache
│   │   ├── credential_cache.py
│   │   └── secret_resolver.py  # secret:: token resolution backends
│   ├── registry/           # tool discovery and proxy registration
│   │   ├── server_registry.py  # load config, call tools, register proxies
│   │   ├── tool_discovery.py   # list_tools() per server
│   │   ├── tool_refresher.py   # background refresh loop for dynamic discovery
│   │   └── proxy_builder.py    # typed async proxy callables
│   ├── transport/          # MCP session management
│   │   └── session.py      # open_session() — SSE and StreamableHTTP
│   ├── security/           # input/output protection
│   │   ├── api_key_redactor.py # redacts api_key patterns from tool output
│   │   ├── pii_redactor.py     # regex + optional Ollama LLM
│   │   └── prompt_injection.py
│   ├── resilience/
│   │   ├── rate_limiter.py         # per-server daily quota (increment-then-check, atomic)
│   │   ├── state_backend.py        # pluggable backend interface (memory / Redis)
│   │   └── backends/
│   │       ├── memory.py           # default — in-process, resets on restart
│   │       └── redis_backend.py    # persistent, multi-instance safe
│   ├── config/             # typed config models and YAML loader
│   ├── api/                # HTTP endpoints (health, metrics, registry)
│   ├── middleware/         # Starlette middleware stubs
│   ├── observability/      # structured JSON logging and in-memory metrics
│   ├── exceptions/
│   └── models/
├── config/
│   ├── remote_servers.yaml         # upstream server definitions (git-ignored)
│   ├── remote_servers.examples.yaml  # auth pattern reference — copy to get started
│   ├── security_policies.yaml      # global tool blocklist and future policy rules
│   └── logging.yaml
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── infra/gcp/              # Terraform — Cloud Run deployment
├── scripts/                # run_local.sh, smoke_test.py, validate_config.py
├── docs/                   # architecture, security, configuration guides
├── Makefile
├── Dockerfile
└── pyproject.toml
```

---

## Quick start

### Docker Compose

```bash
# 1. Clone
git clone https://github.com/ampspaul/mcp-relay.git
cd mcp-relay

# 2. Configure upstream servers
cp config/remote_servers.examples.yaml config/remote_servers.yaml
# Edit config/remote_servers.yaml — add your upstream MCP servers

# 3. Set up secrets
cp .env.example .env
# Edit .env with your API keys

# 4. Start
docker compose up -d

# 5. Verify
curl http://localhost:8080/health
# → {"status":"ok","service":"mcp-relay","upstreams":[{"name":"alpha_vantage","status":"ok",...}]}
```

### Run locally

**Prerequisites:** Python 3.10+

```bash
# 1. Clone
git clone https://github.com/ampspaul/mcp-relay.git
cd mcp-relay

# 2. Install dependencies
pip3 install -e .

# 3. Configure upstream servers
#    Add your real MCP server URLs and secret references.
#    See config/remote_servers.examples.yaml for all auth patterns.
cp config/remote_servers.examples.yaml config/remote_servers.yaml
# Edit config/remote_servers.yaml

# 4. Set up secrets
cp .env.example .env
# Edit .env — add your API keys, e.g.:
#   export MY_API_KEY=your-actual-key-here

# 5. Start the relay
source .env && make run
```

The relay will connect to each configured server, discover their tools, and
register them as proxy tools. You'll see a line like:

```
[mcp_relay] ready — 133 remote tool(s) registered across all servers
```

```bash
# 6. Verify
curl http://localhost:8080/health
# → {"status":"ok","service":"mcp-relay"}
```

The SSE endpoint your LLM client connects to is `http://localhost:8080/sse`.

> **Note on secrets:** `.env` uses `export` so that vars are passed to child
> processes when you run `source .env`. Without `export`, `source .env` sets
> vars only in your current shell and the relay subprocess won't see them.

---

## config/remote_servers.yaml reference

`config/remote_servers.yaml` is git-ignored — never commit secrets. The file
defines all upstream MCP servers the relay connects to at startup.

### Top-level structure

```yaml
servers:
  - name: my-server          # unique identifier — used as MCP tool namespace
    description: "..."
    url: "https://..."        # upstream MCP server URL
    transport: sse            # sse (default) or streamable_http
    enabled: true
    tool_prefix: ""           # prefix added to all tool names from this server
    auth:
      type: <auth-type>       # see Authentication section below
      ...
    # Per-server tool blocklist (optional) — blocks these tools on this server only
    tool_blocklist: []
    # Security flags (all optional, default false)
    sanitize_input: false
    sanitize_output: false
    redact_pii: false
    pii_scan_enabled: false
    pii_scan_model: ""
    injection_detection: false
    # Rate limiting (optional)
    rate_limit:
      requests_per_day: 0     # 0 = disabled
      response_signal_keys: []
```

### Authentication patterns

| `type` | Description |
|--------|-------------|
| `none` | No auth — public server |
| `api_key_query` | Appends `?<param_name>=<value>` to the URL |
| `api_key_url_path` | Substitutes `{placeholder}` in the URL path |
| `api_key_header` | Sends a custom HTTP header |
| `bearer` | Sends `Authorization: Bearer <value>` |
| `oauth2_client_credentials` | Fetches and caches a token from `token_url` |

#### No auth

```yaml
auth:
  type: none
```

#### API key — query parameter

```yaml
auth:
  type: api_key_query
  param_name: api_key          # query param name (default: apikey)
  value: "secret::my-api-key"
```

#### API key — URL path

```yaml
url: "https://example.com/{api_key}/mcp"
auth:
  type: api_key_url_path
  placeholder: "{api_key}"     # placeholder in the URL (default: {api_key})
  value: "secret::my-api-key"
```

#### API key — custom header

```yaml
auth:
  type: api_key_header
  header_name: "X-API-Key"    # header name (default: X-Api-Key)
  value: "secret::my-api-key"
```

#### Bearer token

```yaml
auth:
  type: bearer
  value: "secret::my-bearer-token"
```

#### OAuth2 client credentials

```yaml
auth:
  type: oauth2_client_credentials
  token_url: "https://auth.example.com/oauth/token"
  client_id: "secret::my-client-id"
  client_secret: "secret::my-client-secret"
  scope: "read write"           # optional
  audience: "https://..."       # optional — required by some IdPs (e.g. Auth0)
```

### Security flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `sanitize_input` | bool | `false` | Redact PII from tool arguments before forwarding |
| `sanitize_output` | bool | `false` | Redact API keys from tool results |
| `redact_pii` | bool | `false` | Apply regex PII redaction to tool results |
| `pii_scan_enabled` | bool | `false` | Enable LLM-assisted PII scan (requires `pii_scan_model` + `OLLAMA_URL`) |
| `pii_scan_model` | string | — | Ollama model name, e.g. `llama3.2` |
| `injection_detection` | bool | `false` | Block prompt-injection attempts in tool results |

---

## Secret resolution

Use `secret::<name>` anywhere in `config/remote_servers.yaml`:

```yaml
value: "secret::my-api-key"
```

Set `SECRET_BACKEND` to choose the resolver:

| Backend | `SECRET_BACKEND` | Notes |
|---------|-----------------|-------|
| Environment variable | `env` (default) | `my-api-key` → env var `MY_API_KEY` |
| GCP Secret Manager | `gcp` | Requires `GCP_PROJECT_ID` and `pip install mcp-relay[gcp]` |
| AWS Secrets Manager | `aws` | Requires `AWS_REGION` and `pip install mcp-relay[aws]` |
| Azure Key Vault | `azure` | Requires `AZURE_KEYVAULT_URL` and `pip install mcp-relay[azure]` |
| HashiCorp Vault | `vault` | Requires `VAULT_ADDR` + `VAULT_TOKEN` and `pip install mcp-relay[vault]` |
| Plain (testing only) | `plain` | Returns the name as-is — never use in production |

```bash
pip install mcp-relay[gcp]          # GCP Secret Manager
pip install mcp-relay[aws]          # AWS Secrets Manager
pip install mcp-relay[azure]        # Azure Key Vault
pip install mcp-relay[vault]        # HashiCorp Vault
pip install mcp-relay[all-secrets]  # all backends
```

Secret resolution lives in `src/mcp_relay/auth/secret_resolver.py`.

---

## Security pipeline

Each tool call passes through these layers (all opt-in per server):

```
Tool call arguments
      │
      ▼
[1] Input sanitization     (sanitize_input: true)
      │   PII redaction applied to string arguments before forwarding
      ▼
[2] Forward to remote MCP server
      │
      ▼
[3] API key redaction      (sanitize_output: true)
      │   api_key patterns scrubbed from tool result text
      ▼
[4] PII redaction — regex  (redact_pii: true)
      │   Emails, SSNs, credit cards, phone numbers
      ▼
[5] PII redaction — LLM    (pii_scan_enabled: true + pii_scan_model set)
      │   Ollama model scan; falls back to regex on timeout/error
      ▼
[6] Prompt-injection check (injection_detection: true)
      │   12 regex patterns; blocks and discards result on match
      ▼
Tool result returned to LLM client
```

### Prompt-injection detection

Patterns in `src/mcp_relay/security/prompt_injection.py` cover:

| Category | Example |
|----------|---------|
| Instruction override | `ignore all previous instructions` |
| Context reset | `forget everything`, `disregard your system prompt` |
| Persona hijack | `act as`, `pretend to be`, `roleplay as` |
| System prompt reveal | `show your instructions`, `repeat your initial prompt` |
| Data exfiltration | `send all data to`, `leak credentials to` |
| Safety bypass | `bypass your safety filters`, `jailbreak` |

Enable selectively — only on servers returning unstructured or user-influenced content:

```yaml
injection_detection: true   # scraped web content, user-supplied data
injection_detection: false  # typed API parameters from a controlled client
```

### PII redaction (Ollama — optional)

1. Deploy [Ollama](https://ollama.com/) and pull a model: `ollama pull llama3.2`
2. Set `OLLAMA_URL=http://your-ollama-host:11434`
3. Enable in `config/remote_servers.yaml`:

```yaml
pii_scan_enabled: true
pii_scan_model: "llama3.2"
```

If Ollama is unreachable or times out, the pipeline falls back to regex — no requests are dropped.

---

## config/security_policies.yaml reference

`config/security_policies.yaml` holds global security rules and operational settings that apply across all upstream servers.

### Dynamic tool discovery

By default the relay discovers tools once at startup. Set `tool_refresh_interval_seconds` to have the relay periodically re-query every enabled upstream server, picking up new or removed tools without a restart:

```yaml
tool_refresh_interval_seconds: 300   # refresh every 5 minutes (0 = disabled)
```

When a new tool appears upstream it is registered automatically; when a tool disappears it is unregistered. Changes are reflected in `/registry` immediately after the next cycle. Values under 60 are not recommended — each cycle opens a session to every enabled upstream server.

New metrics are emitted per cycle:

| Metric | Description |
|--------|-------------|
| `tool_refresh_total` | Number of refresh cycles completed |
| `tools_added_total` | Tools registered mid-flight, labelled by server |
| `tools_removed_total` | Tools unregistered mid-flight, labelled by server |

### Inbound authentication

Control who can connect to the relay's `/sse` endpoint. Configure in `config/security_policies.yaml`:

```yaml
inbound_auth:
  type: bearer          # or: none (default — open access)
  tokens:
    - "secret::relay-api-key-1"   # resolved via SECRET_BACKEND at startup
    - "secret::relay-api-key-2"   # multiple tokens for key rotation
```

Add the corresponding env vars to `.env`:

```bash
export RELAY_API_KEY_1=your-strong-random-key
export RELAY_API_KEY_2=another-key-for-rotation
```

With bearer auth enabled, every request to `/sse` must include:

```
Authorization: Bearer your-strong-random-key
```

`/health` and `/metrics` are always exempt so load-balancer probes continue to work. All other endpoints — including `/registry` and `/sse` — require a valid token.

Rejected requests return `401` with `WWW-Authenticate: Bearer` and are counted in the `inbound_auth_rejected_total` metric (labelled by reason: `missing_token` or `invalid_token`).

### Tool blocklist

The relay supports two levels of tool blocking that are applied together:

#### Global blocklist — `config/security_policies.yaml`

Blocks a tool name across **all** upstream servers. Use this as a security kill-switch for tools that are dangerous regardless of source.

```yaml
tool_blocklist:
  - delete_account   # blocked on every server
  - drop_table
```

#### Per-server blocklist — `config/remote_servers.yaml`

Blocks tools only on that specific server. Use this to trim a noisy tool list without affecting other servers that happen to have the same tool name.

```yaml
servers:
  - name: alpha_vantage
    url: "https://..."
    tool_blocklist:
      - SUGAR
      - WHEAT
```

Both lists are combined — a tool blocked by either is suppressed. The relay logs which config file triggered the block:

```
[registry] alpha_vantage: tool 'SUGAR' blocked by remote_servers.yaml
[registry] alpha_vantage: tool 'delete_account' blocked by security_policies.yaml
```

Blocked tools appear in `GET /registry` under `tools.blocked` so you can always see what's been suppressed and why.

> **Note:** Both lists match against the **upstream tool name** (before any `tool_prefix` is applied).

### Per-call timeout

Set the maximum time (in seconds) the relay will wait for a single upstream tool call — session open plus response. Requests that exceed the limit are cancelled and counted as failures toward the circuit breaker threshold.

```yaml
# config/security_policies.yaml
tool_call_timeout_seconds: 30   # global default (applies to all servers)
```

Override per server:

```yaml
# config/remote_servers.yaml
servers:
  - name: slow_api
    url: "https://..."
    tool_call_timeout_seconds: 60   # this server gets a longer budget
```

When a call times out, the LLM receives a `RuntimeError` message and the relay logs:

```
[registry] alpha_vantage: tool=TIME_SERIES_DAILY timed out after 30.0s
```

### Circuit breaker

The circuit breaker stops cascading failures when an upstream server is slow or unreachable. Configure globally in `config/security_policies.yaml`:

```yaml
circuit_breaker:
  failure_threshold: 5        # consecutive failures before opening the circuit
  recovery_timeout_seconds: 60 # seconds to wait before probing again (HALF_OPEN)
  success_threshold: 1         # successful probes needed to return to CLOSED
```

Override per server in `config/remote_servers.yaml`:

```yaml
servers:
  - name: alpha_vantage
    url: "https://..."
    circuit_breaker:
      failure_threshold: 3
      recovery_timeout_seconds: 30
```

**State machine:**

| State | Behaviour |
|-------|-----------|
| `closed` | Normal — calls flow through; failures are counted |
| `open` | Tripped — calls are rejected immediately without hitting upstream |
| `half_open` | Recovery window elapsed — one probe is allowed through; success → `closed`, failure → `open` |

When the circuit is open, the LLM receives a `CircuitOpenError` message explaining when to retry. The relay logs a warning and increments the `circuit_open_total` metric. Both `asyncio.TimeoutError` (from the per-call timeout) and transport errors count toward the failure threshold.

> **Single-worker limitation:** Circuit breaker state is held in-process memory. With multiple uvicorn workers or multiple container replicas, each process has its own independent circuit — a tripped breaker on one worker does not protect the others. For consistent protection, run with a single worker per container and scale via replicas.

Current circuit breaker state per server is visible in `GET /registry` under `circuit_breaker`:

```json
{
  "circuit_breaker": {
    "state": "open",
    "failure_count": 5
  }
}
```

---

## Observability

### Structured logging

By default the relay emits structured JSON logs, one object per line — easy to ship to Datadog, CloudWatch, or any log aggregator:

```json
{"ts": "2026-08-29T09:00:26Z", "level": "INFO", "logger": "mcp_relay.registry.server_registry", "msg": "[registry] alpha_vantage: 133 proxy tool(s) registered (prefix='')"}
{"ts": "2026-08-29T09:00:27Z", "level": "INFO", "logger": "mcp_relay.registry.server_registry", "msg": "[registry] alpha_vantage: calling tool=TIME_SERIES_DAILY args_keys=['symbol']"}
```

Set `LOG_FORMAT=text` for human-readable output during local development:

```bash
export LOG_FORMAT=text
source .env && make run
```

### Metrics endpoint

The relay tracks in-memory metrics and exposes them at `GET /metrics`:

```bash
curl http://localhost:8080/metrics
```

```json
{
  "counters": {
    "tool_calls_total{server=alpha_vantage,tool=TIME_SERIES_DAILY}": 4,
    "tool_errors_total{server=alpha_vantage,type=transport}": 1,
    "rate_limit_exceeded_total{server=alpha_vantage}": 0,
    "rate_limit_signal_total{server=alpha_vantage}": 0,
    "injection_blocked_total{server=alpha_vantage,pattern=jailbreak}": 0
  },
  "gauges": {
    "tools_registered{server=alpha_vantage}": 133.0
  },
  "histograms": {
    "tool_call_duration_seconds{server=alpha_vantage}": {
      "count": 4, "sum": 1.2, "min": 0.18, "max": 0.55, "avg": 0.3
    }
  }
}
```

| Metric | Type | Description |
|--------|------|-------------|
| `tool_calls_total` | counter | Total calls, labelled by server and tool name |
| `tool_errors_total` | counter | Failures by type: `transport` or `tool_error` |
| `tools_registered` | gauge | Tools currently registered per server |
| `tool_call_duration_seconds` | histogram | Latency per server — min, max, avg, sum, count |
| `tool_refresh_total` | counter | Background discovery cycles completed |
| `tools_added_total` | counter | Tools registered mid-flight by refresh loop, per server |
| `tools_removed_total` | counter | Tools unregistered mid-flight by refresh loop, per server |
| `rate_limit_exceeded_total` | counter | Daily quota exhausted, per server |
| `rate_limit_signal_total` | counter | API-signalled rate limits (e.g. `Note` key in response), per server |
| `injection_blocked_total` | counter | Blocked responses per server and injection pattern |
| `inbound_auth_rejected_total` | counter | Rejected inbound requests, labelled by `reason` (`missing_token` / `invalid_token`) |
| `inbound_auth_accepted_total` | counter | Accepted inbound requests |
| `circuit_open_total` | counter | Calls rejected because the circuit is open, per server |
| `tool_timeout_total` | counter | Tool calls cancelled due to timeout, per server |

Metrics are in-memory and reset on restart. For persistent metrics, scrape `/metrics` with a cron job or sidecar and push to your preferred store.

### Registry endpoint

`GET /registry` returns a real-time view of every configured server, the tools currently registered from it, and any tools blocked by `security_policies.yaml`:

```bash
curl http://localhost:8080/registry
```

```json
{
  "servers": [
    {
      "name": "alpha_vantage",
      "url": "https://mcp.alpha-vantage.io/sse",
      "enabled": true,
      "tool_prefix": "av_",
      "tools": {
        "available": [
          {
            "name": "av_get_quote",
            "description": "Get the latest price quote for a stock ticker.",
            "parameters": [
              {"name": "symbol", "type": "string", "required": true, "description": "Ticker symbol, e.g. MSFT"}
            ]
          }
        ],
        "blocked": [
          {
            "name": "av_admin_reset",
            "description": "Resets all account data."
          }
        ]
      },
      "counts": {
        "available": 1,
        "blocked": 1
      },
      "circuit_breaker": {
        "state": "closed",
        "failure_count": 0
      }
    }
  ],
  "summary": {
    "total_servers": 1,
    "enabled_servers": 1,
    "available_tools": 1,
    "blocked_tools": 1
  }
}
```

Each available tool includes its `description` and a `parameters` list so AI agents can introspect what arguments to pass without an extra round-trip. Blocked tools show `name` and `description` only — `parameters` is omitted because the tool cannot be called. Circuit breaker state (`closed`, `open`, or `half_open`) and the current failure count are reported per server.

Only safe fields are returned — `headers`, `auth`, and other internal config are never included. The endpoint is protected by inbound bearer auth when enabled (it exposes server topology).

---

## Persistent state (Redis)

By default rate limit counters are stored in-process memory and reset on restart.

| `STATE_BACKEND` | Behaviour |
|-----------------|-----------|
| `memory` (default) | In-process dict — zero deps, resets on restart, single container only |
| `redis` | Atomic Redis counters — persists across restarts, accurate across multiple containers |

> **Multi-container deployments must use Redis.** With the memory backend each container maintains its own independent counter. Three containers with a limit of 1,000 requests/day effectively allow 3,000 — the quota is meaningless. Redis `INCR` is atomic and the counter is shared, so the limit is enforced correctly regardless of how many replicas are running.

```bash
pip3 install mcp-relay[redis]
```

```bash
# .env
export STATE_BACKEND=redis
export REDIS_URL=redis://your-redis:6379
```

Rate limit keys are namespaced as `mcp_relay:ratelimit:{server}:{date}` and automatically expire at midnight so daily quotas reset without any manual intervention.

---

## Horizontal scaling

The relay is designed to scale horizontally. Run as many replicas as needed with the following configuration:

### Requirements

**Redis is required for accurate rate limiting** (see above). Everything else works correctly at any replica count without coordination.

**Run one uvicorn worker per container** and scale via replica count, not `--workers`. The circuit breaker holds state in-process — bumping workers inside a container multiplies the number of independent breakers without the replica visibility that a load balancer provides.

```bash
# Correct — one worker, many replicas
docker run mcp-relay   # CMD in Dockerfile already uses single-process mode
# Scale: increase replica count in your orchestrator

# Incorrect — multiple workers per container
# uvicorn ... --workers 4   ← each worker gets its own circuit breaker state
```

### What is shared across replicas

| Component | Shared? | Notes |
|---|---|---|
| Rate limit counters | Yes (with Redis) | Atomic `INCR` — accurate across all replicas |
| Inbound bearer auth | Yes | Stateless token check — no coordination needed |
| Config / tool registry | Yes | All replicas read the same mounted config at startup |
| Security pipeline | Yes | Fully stateless |
| Secret resolution | Yes | Each replica fetches on cache miss; cloud backends are the source of truth |

### What is per-replica

| Component | Per-replica? | Impact |
|---|---|---|
| Circuit breaker state | Yes | A failing upstream absorbs up to `N × failure_threshold` calls before all replicas stop. Documented tradeoff — see `config/security_policies.yaml`. |
| MCP session pool | Yes | Each replica holds its own persistent connections to upstreams. Independent pools are correct behavior, not shared state. |
| OAuth2 / credential cache | Yes | Each replica may fetch a token independently on cold start. Slightly wasteful but correct — tokens are stateless and reusable. |
| `/metrics` counters | Yes | Each replica's `/metrics` is a partial view. Scrape all instances in your metrics platform and aggregate. |
| Tool refresh loop | Yes | Each replica opens its own sessions per cycle. Keep `tool_refresh_interval_seconds` conservative (≥300) or disabled when running many replicas. |

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Port the relay listens on |
| `SECRET_BACKEND` | `env` | Secret resolution backend |
| `LOG_FORMAT` | `json` | Log format — `json` for structured output, `text` for human-readable |
| `STATE_BACKEND` | `memory` | State backend for rate limit counters — `memory` or `redis` |
| `REDIS_URL` | `redis://localhost:6379` | Required when `STATE_BACKEND=redis` |
| `GCP_PROJECT_ID` | — | Required when `SECRET_BACKEND=gcp` |
| `AWS_REGION` | — | Required when `SECRET_BACKEND=aws` |
| `AZURE_KEYVAULT_URL` | — | Required when `SECRET_BACKEND=azure` |
| `VAULT_ADDR` | — | Required when `SECRET_BACKEND=vault` |
| `VAULT_TOKEN` | — | Required when `SECRET_BACKEND=vault` |
| `OLLAMA_URL` | `http://localhost:11434` | Required when `pii_scan_model` is set |

Copy `.env.example` to `.env` for a full reference.

---

## Endpoints

| Method | Path | Auth-exempt | Description |
|--------|------|-------------|-------------|
| `GET` | `/health` | Yes | Relay health + per-upstream status — `ok` or `degraded` |
| `GET` | `/metrics` | No | In-memory metrics snapshot — counters, gauges, histograms |
| `GET` | `/registry` | No | Server and tool inventory — available tools, blocked tools, per-server counts |
| `GET` | `/sse` | No | MCP SSE transport — connect your LLM client here |

Only `/health` is auth-exempt when `inbound_auth.type: bearer` is configured, so load-balancer probes always succeed. `/metrics` requires a valid token because it exposes tool names, call counts, and error rates that can be used to profile the deployment.

### /health response

`GET /health` always returns HTTP `200`. Read the `status` field to distinguish healthy from degraded (a `503` would incorrectly pull the relay out of load-balancer rotation when only one upstream is down).

```json
{
  "status": "degraded",
  "service": "mcp-relay",
  "upstreams": [
    {
      "name": "alpha_vantage",
      "status": "ok",
      "session": "connected",
      "circuit_breaker": "closed"
    },
    {
      "name": "slow_api",
      "status": "degraded",
      "session": "connected",
      "circuit_breaker": "open"
    }
  ]
}
```

| `status` | Meaning |
|----------|---------|
| `ok` | Session connected and circuit closed |
| `degraded` | Circuit open or half-open (upstream failing) |
| `connecting` | Pool is (re)connecting after a drop |
| `disabled` | Server is `enabled: false` in config |

The overall `status` is `ok` only when every enabled upstream reports `ok`. Disabled servers are ignored in the aggregate.

---

## Connecting an LLM client

### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "relay": {
      "url": "http://localhost:8080/sse",
      "transport": "sse"
    }
  }
}
```

### Python client

```python
from mcp import ClientSession
from mcp.client.sse import sse_client

async with sse_client("http://localhost:8080/sse") as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
```

---

## Deployment

### GCP Cloud Run

Terraform in `infra/gcp/`:

```bash
cd infra/gcp
terraform init
terraform apply -var="project_id=YOUR_PROJECT"
```

Or build and push manually:

```bash
make docker-build
docker tag mcp-relay gcr.io/YOUR_PROJECT/mcp-relay:v1
docker push gcr.io/YOUR_PROJECT/mcp-relay:v1
```

Cloud Run terminates TLS automatically. For horizontal scaling set `--min-instances` and `--max-instances` and point `REDIS_URL` at a Cloud Memorystore instance:

```bash
gcloud run deploy mcp-relay \
  --image gcr.io/YOUR_PROJECT/mcp-relay:v1 \
  --set-env-vars SECRET_BACKEND=gcp,GCP_PROJECT_ID=YOUR_PROJECT \
  --set-env-vars STATE_BACKEND=redis,REDIS_URL=redis://MEMORYSTORE_IP:6379 \
  --min-instances 1 --max-instances 10
```

### AWS ECS / Fargate

```bash
make docker-build
docker tag mcp-relay YOUR_ACCOUNT.dkr.ecr.REGION.amazonaws.com/mcp-relay:v1
docker push YOUR_ACCOUNT.dkr.ecr.REGION.amazonaws.com/mcp-relay:v1
```

Deploy via ECS task definition. For horizontal scaling set `desiredCount > 1` and point `REDIS_URL` at an ElastiCache instance:

```json
{
  "environment": [
    {"name": "SECRET_BACKEND", "value": "aws"},
    {"name": "STATE_BACKEND",  "value": "redis"},
    {"name": "REDIS_URL",      "value": "redis://YOUR_ELASTICACHE:6379"}
  ]
}
```

### Azure Container Apps

```bash
az acr build --registry YOUR_ACR --image mcp-relay:v1 .
az containerapp create \
  --name mcp-relay \
  --env-vars SECRET_BACKEND=azure \
             AZURE_KEYVAULT_URL=https://YOUR_VAULT.vault.azure.net \
             STATE_BACKEND=redis \
             REDIS_URL=redis://YOUR_AZURE_CACHE:6379 \
  --min-replicas 1 --max-replicas 10 \
  --image YOUR_ACR.azurecr.io/mcp-relay:v1
```

Container Apps terminates TLS and load-balances across replicas automatically.

---

## Development

```bash
make dev        # install with dev extras
make lint       # ruff + mypy
make test       # pytest
make run        # start locally
make docker-build
make docker-run
```

Manual equivalents:

```bash
pip3 install -e ".[dev]"
ruff check src/ tests/
mypy src/
python3 -m pytest tests/
```

---

## Adding a custom secret backend

Add a new `elif` branch in `src/mcp_relay/auth/secret_resolver.py`:

```python
elif backend == "mybackend":
    return await _resolve_mybackend(name)
```

Then implement `_resolve_mybackend(name: str) -> str` in the same file following the pattern of the existing backends (`_resolve_gcp`, `_resolve_aws`, etc.).

---

## License

MIT — see [LICENSE](LICENSE).
