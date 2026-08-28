# mcp-relay

A lightweight, config-driven MCP gateway that proxies tool calls to remote MCP servers with built-in authentication, secret resolution, and a layered security pipeline.

```
LLM client  ──SSE──►  mcp-relay  ──HTTP──►  Remote MCP Server A
                           │      ──HTTP──►  Remote MCP Server B
                           │      ──HTTP──►  Remote MCP Server C
                     (auth · sanitize · PII · injection · rate-limit)
```

---

## Features

- **Config-driven** — add/remove upstream servers by editing `remote_servers.yaml`; no code changes
- **Six auth patterns** — none, query param, URL path, custom header, bearer token, OAuth2 client credentials
- **Pluggable secrets** — env vars (default), GCP Secret Manager, AWS Secrets Manager, Azure Key Vault, HashiCorp Vault
- **Security pipeline** — input/output sanitization, PII redaction (regex + optional Ollama LLM), prompt-injection detection (13 patterns × 8 categories)
- **Rate limiting** — per-server RPM cap with a token-bucket implementation
- **Token caching** — OAuth2 tokens and resolved credentials are cached in-memory (5-min TTL) to avoid repeated secret lookups
- **Non-root container** — multi-stage Dockerfile; runs as uid 1000

---

## Quick start

### Docker Compose (recommended)

```bash
# 1. Clone the repo
git clone https://github.com/ampspaul/mcp-relay.git
cd mcp-relay

# 2. Create your server config
cp remote_servers.example.yaml remote_servers.yaml
# Edit remote_servers.yaml — add your upstream MCP servers

# 3. Create the env file
cat > .env <<'EOF'
PORT=8080
SECRET_BACKEND=env
MY_API_KEY=your-actual-key-here
EOF

# 4. Start
docker compose up -d

# 5. Verify
curl http://localhost:8080/health
# → {"status":"ok","service":"mcp-relay"}
```

### Run locally (no Docker)

```bash
pip install -e .
cp remote_servers.example.yaml remote_servers.yaml
# Edit remote_servers.yaml

export SECRET_BACKEND=env
export MY_API_KEY=your-actual-key-here
python -m src.mcp_gateway.main
```

The SSE endpoint is available at `http://localhost:8080/sse`.

---

## remote_servers.yaml reference

Copy `remote_servers.example.yaml` to `remote_servers.yaml` (this file is git-ignored — never commit secrets).

### Top-level structure

```yaml
servers:
  - name: my-server          # unique identifier — used as MCP tool namespace
    description: "..."       # human-readable description
    url: "https://..."       # upstream MCP server SSE URL
    auth:
      type: <auth-type>      # see Authentication section
      ...                    # auth-type-specific fields
    # Security flags (all optional, default false / 0)
    sanitize_input: true
    sanitize_output: true
    pii_scan_enabled: false
    injection_detection: false
    rate_limit_rpm: 0
```

### Authentication patterns

| Type | Description |
|------|-------------|
| `none` | No auth — public server |
| `query_param` | Appends `?<param_name>=<value>` to the URL |
| `url_path` | Substitutes `{placeholder}` in the URL path |
| `header` | Sends a custom HTTP header |
| `bearer` | Sends `Authorization: Bearer <token>` |
| `oauth2_client_credentials` | Fetches and caches a token from `token_url` |

#### Pattern 1 — No auth

```yaml
auth:
  type: none
```

#### Pattern 2 — Query param

```yaml
auth:
  type: query_param
  param_name: api_key
  param_value: "secret::my-api-key"
```

#### Pattern 3 — URL path

```yaml
url: "https://example.com/{api_key}/mcp"
auth:
  type: url_path
  path_param: api_key
  path_value: "secret::my-api-key"
```

#### Pattern 4 — Custom header

```yaml
auth:
  type: header
  header_name: "X-API-Key"
  header_value: "secret::my-api-key"
```

#### Pattern 5 — Bearer token

```yaml
auth:
  type: bearer
  token: "secret::my-bearer-token"
```

#### Pattern 6 — OAuth2 client credentials

```yaml
auth:
  type: oauth2_client_credentials
  token_url: "https://auth.example.com/oauth/token"
  client_id: "secret::my-client-id"
  client_secret: "secret::my-client-secret"
  scope: "read write"       # optional
  audience: "https://..."   # optional — required by some IdPs (e.g. Auth0)
```

### Security flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `sanitize_input` | bool | `false` | Strip/escape dangerous characters in tool arguments before forwarding |
| `sanitize_output` | bool | `false` | Strip/escape dangerous content in tool results before returning to the LLM |
| `pii_scan_enabled` | bool | `false` | Scan inputs and outputs for PII patterns; redact on match |
| `pii_scan_model` | string | — | Ollama model to use for LLM-assisted PII scanning (requires `pii_scan_enabled: true` and `OLLAMA_URL` env var) |
| `injection_detection` | bool | `false` | Detect and block prompt-injection attempts in tool arguments |
| `rate_limit_rpm` | int | `0` | Max requests per minute from any client to this server (`0` = disabled) |

---

## Secret resolution

Use the `secret::<name>` syntax anywhere in `remote_servers.yaml` to reference a secret without embedding it in the file:

```yaml
token: "secret::my-bearer-token"
```

Set `SECRET_BACKEND` to choose the resolver:

| Backend | Value | Notes |
|---------|-------|-------|
| Environment variable | `env` (default) | Secret name is uppercased and hyphens → underscores: `my-key` → `MY_KEY` |
| GCP Secret Manager | `gcp` | Requires `GCP_PROJECT_ID` and `pip install mcp-relay[gcp]` |
| AWS Secrets Manager | `aws` | Requires `AWS_REGION` + credentials and `pip install mcp-relay[aws]` |
| Azure Key Vault | `azure` | Requires `AZURE_VAULT_URL` + identity env vars and `pip install mcp-relay[azure]` |
| HashiCorp Vault | `vault` | Requires `VAULT_ADDR` + `VAULT_TOKEN` and `pip install mcp-relay[vault]` |
| Plain (testing only) | `plain` | Value is used as-is — never use in production |

Install only the extras you need:

```bash
pip install mcp-relay[gcp]      # GCP Secret Manager
pip install mcp-relay[aws]      # AWS Secrets Manager
pip install mcp-relay[azure]    # Azure Key Vault
pip install mcp-relay[vault]    # HashiCorp Vault
pip install mcp-relay[all-secrets]  # all backends
```

---

## Security pipeline

Every inbound tool call and outbound tool result passes through this pipeline:

```
Tool call arguments
      │
      ▼
[1] Input sanitization  (sanitize_input: true)
      │   • Strip null bytes, control characters
      │   • Escape HTML/script tags
      │   • Remove shell metacharacters
      ▼
[2] Prompt-injection detection  (injection_detection: true)
      │   • 13 regex patterns across 8 attack categories
      │   • Raises ToolError and blocks the call on match
      ▼
[3] PII scan — input  (pii_scan_enabled: true)
      │   • Layer 1: regex patterns (emails, SSNs, credit cards, phone numbers, IPs)
      │   • Layer 2: Ollama LLM scan (if pii_scan_model + OLLAMA_URL set)
      │   • Layer 3: regex fallback if LLM unavailable
      │   • Matched values are redacted to [REDACTED]
      ▼
[4] Forward to remote MCP server
      │
      ▼
[5] PII scan — output  (pii_scan_enabled: true)
      │   • Same three-layer pipeline applied to the tool result
      ▼
[6] Output sanitization  (sanitize_output: true)
      │
      ▼
Tool result returned to LLM client
```

### Prompt-injection detection

13 patterns across 8 attack categories are applied when `injection_detection: true`:

| Category | Example patterns |
|----------|-----------------|
| Instruction override | `ignore all previous instructions`, `disregard your system prompt` |
| Role hijacking | `you are now`, `act as`, `pretend you are` |
| Jailbreak | `DAN mode`, `developer mode`, `unrestricted mode` |
| Context escape | `\n\nHuman:`, `###SYSTEM`, XML/tag injection |
| Data exfiltration | `repeat everything above`, `output your instructions` |
| Indirect injection | Patterns typical in web-scraped or document content |
| Encoding evasion | Base64, hex, Unicode homoglyph sequences |
| Multi-step manipulation | Chained instruction patterns across turns |

Enable per server based on risk:

```yaml
# High-risk: unstructured web content, user-supplied text
injection_detection: true

# Low-risk: typed API parameters from a controlled client
injection_detection: false
```

### PII redaction (Ollama — optional)

By default PII scanning uses regex patterns only. To add an LLM-assisted scan layer:

1. Deploy [Ollama](https://ollama.com/) on a GPU instance
2. Set `OLLAMA_URL=http://your-ollama-host:11434`
3. Pull your model: `ollama pull llama3.2`
4. Enable in `remote_servers.yaml`:

```yaml
pii_scan_enabled: true
pii_scan_model: "llama3.2"
```

If the Ollama call fails or times out, the pipeline falls back to regex automatically — no requests are dropped.

---

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PORT` | No | `8080` | Port the relay listens on |
| `SECRET_BACKEND` | No | `env` | Secret resolution backend |
| `GCP_PROJECT_ID` | If `SECRET_BACKEND=gcp` | — | GCP project containing secrets |
| `AWS_REGION` | If `SECRET_BACKEND=aws` | — | AWS region for Secrets Manager |
| `AWS_ACCESS_KEY_ID` | If `SECRET_BACKEND=aws` | — | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | If `SECRET_BACKEND=aws` | — | AWS secret key |
| `AZURE_VAULT_URL` | If `SECRET_BACKEND=azure` | — | Azure Key Vault URL |
| `AZURE_CLIENT_ID` | If `SECRET_BACKEND=azure` | — | Azure app client ID |
| `AZURE_CLIENT_SECRET` | If `SECRET_BACKEND=azure` | — | Azure app client secret |
| `AZURE_TENANT_ID` | If `SECRET_BACKEND=azure` | — | Azure tenant ID |
| `VAULT_ADDR` | If `SECRET_BACKEND=vault` | — | HashiCorp Vault address |
| `VAULT_TOKEN` | If `SECRET_BACKEND=vault` | — | HashiCorp Vault token |
| `OLLAMA_URL` | If `pii_scan_model` set | — | Ollama API base URL |

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health probe — returns `{"status":"ok","service":"mcp-relay"}` |
| `GET` | `/sse` | MCP SSE transport — connect your LLM client here |

---

## Connecting an LLM client

### Claude Desktop / Claude Code

Add to your MCP config:

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

### LangChain / custom client

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

```bash
# Build and push via Cloud Build
gcloud builds submit \
  --config=deploy/gcp/cloudbuild.yaml \
  --substitutions="_IMAGE=us-central1-docker.pkg.dev/YOUR_PROJECT/YOUR_REPO/mcp-relay:v1" \
  --project=YOUR_PROJECT

# Mount remote_servers.yaml via a Secret Manager secret or Cloud Run volume mount
```

### AWS ECS / Fargate

```bash
docker build -t mcp-relay:v1 .
docker tag mcp-relay:v1 YOUR_ACCOUNT.dkr.ecr.REGION.amazonaws.com/mcp-relay:v1
docker push YOUR_ACCOUNT.dkr.ecr.REGION.amazonaws.com/mcp-relay:v1
# Deploy via ECS task definition with SECRET_BACKEND=aws
```

### Azure Container Apps

```bash
az acr build --registry YOUR_ACR --image mcp-relay:v1 .
az containerapp create \
  --name mcp-relay \
  --env-vars SECRET_BACKEND=azure AZURE_VAULT_URL=https://... \
  --image YOUR_ACR.azurecr.io/mcp-relay:v1
```

---

## Development

```bash
# Install with dev extras
pip install -e ".[dev]"

# Lint
ruff check src/

# Type check
mypy src/

# Tests
pytest

# Pre-commit hooks
pre-commit install
```

---

## Adding a custom secret backend

Implement the `resolve_secret_refs` function in `src/secret_resolver.py`:

```python
async def resolve_secret_refs(obj: dict) -> dict:
    """Recursively resolve secret::<name> references in a config dict."""
    ...
```

The function receives the raw `auth` dict from `remote_servers.yaml` and must return the same dict with all `secret::<name>` strings replaced by their resolved values.

---

## License

MIT — see [LICENSE](LICENSE).
