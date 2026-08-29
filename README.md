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
- **Observability** — structured JSON logging and in-memory metrics exposed at `/metrics`
- **Rate limiting** — per-server daily quota with response-signal detection
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
│   │   └── proxy_builder.py    # typed async proxy callables
│   ├── transport/          # MCP session management
│   │   └── session.py      # open_session() — SSE and StreamableHTTP
│   ├── security/           # input/output protection
│   │   ├── secret_redactor.py
│   │   ├── pii_redactor.py     # regex + optional Ollama LLM
│   │   └── prompt_injection.py
│   ├── resilience/
│   │   └── rate_limiter.py
│   ├── config/             # typed config models and YAML loader
│   ├── api/                # HTTP endpoints (health, admin)
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
# → {"status":"ok","service":"mcp-relay"}
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
      │   PII redaction applied to string arguments
      ▼
[2] Forward to remote MCP server
      │
      ▼
[3] Secret redaction       (sanitize_output: true)
      │   API key patterns scrubbed from tool result text
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

`config/security_policies.yaml` holds global security rules that apply across all upstream servers.

### Tool blocklist

Prevent specific upstream tools from ever being exposed to the LLM client, regardless of which server provides them. Matches against the **upstream tool name** (before any `tool_prefix` is applied).

```yaml
tool_blocklist:
  - delete_account   # never expose destructive ops
  - drop_table
  - admin_reset
```

The relay logs each blocked tool at startup:

```
[registry] alpha_vantage: tool 'admin_reset' blocked by security_policies.yaml
```

Use this to:
- Lock out dangerous or privileged tools from a server you don't fully control
- Trim a large upstream tool list down to only what your app needs
- Enforce a consistent policy across multiple servers without editing each one individually

> **Note:** `tool_blocklist: []` (the default) means no tools are blocked.

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
| `tools_registered` | gauge | Tools discovered per server at startup |
| `tool_call_duration_seconds` | histogram | Latency per server — min, max, avg, sum, count |
| `rate_limit_exceeded_total` | counter | Daily quota exhausted, per server |
| `rate_limit_signal_total` | counter | API-signalled rate limits (e.g. `Note` key in response), per server |
| `injection_blocked_total` | counter | Blocked responses per server and injection pattern |

Metrics are in-memory and reset on restart. For persistent metrics, scrape `/metrics` with a cron job or sidecar and push to your preferred store.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Port the relay listens on |
| `SECRET_BACKEND` | `env` | Secret resolution backend |
| `LOG_FORMAT` | `json` | Log format — `json` for structured output, `text` for human-readable |
| `GCP_PROJECT_ID` | — | Required when `SECRET_BACKEND=gcp` |
| `AWS_REGION` | — | Required when `SECRET_BACKEND=aws` |
| `AZURE_KEYVAULT_URL` | — | Required when `SECRET_BACKEND=azure` |
| `VAULT_ADDR` | — | Required when `SECRET_BACKEND=vault` |
| `VAULT_TOKEN` | — | Required when `SECRET_BACKEND=vault` |
| `OLLAMA_URL` | `http://localhost:11434` | Required when `pii_scan_model` is set |

Copy `.env.example` to `.env` for a full reference.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health probe — `{"status":"ok","service":"mcp-relay"}` |
| `GET` | `/metrics` | In-memory metrics snapshot — counters, gauges, histograms |
| `GET` | `/sse` | MCP SSE transport — connect your LLM client here |

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

### AWS ECS / Fargate

```bash
make docker-build
docker tag mcp-relay YOUR_ACCOUNT.dkr.ecr.REGION.amazonaws.com/mcp-relay:v1
docker push YOUR_ACCOUNT.dkr.ecr.REGION.amazonaws.com/mcp-relay:v1
# Deploy via ECS task definition with SECRET_BACKEND=aws
```

### Azure Container Apps

```bash
az acr build --registry YOUR_ACR --image mcp-relay:v1 .
az containerapp create \
  --name mcp-relay \
  --env-vars SECRET_BACKEND=azure AZURE_KEYVAULT_URL=https://... \
  --image YOUR_ACR.azurecr.io/mcp-relay:v1
```

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
