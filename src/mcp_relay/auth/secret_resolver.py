"""
secret_resolver.py — Secret resolution for MCP Relay.

Exposes:
  - resolve(name: str) -> str
      Resolve a single secret by name using the configured backend.

  - resolve_secret_refs(data: Any) -> Any
      Recursively walk a data structure (e.g. parsed YAML) and replace all
      occurrences of the token  ``secret::<name>``  with the resolved secret
      value.

Backend selection is controlled by the ``SECRET_BACKEND`` environment variable
(default: ``env``).  Supported backends (FR-02 through FR-09):

  env    — Look up the secret name as an environment variable, converting the
            name to UPPERCASE and replacing hyphens with underscores (FR-04).
  gcp    — Google Cloud Secret Manager; requires ``GCP_PROJECT_ID`` env var
            and the ``google-cloud-secret-manager`` package  [gcp]  (FR-05).
  aws    — AWS Secrets Manager; requires ``AWS_REGION`` env var and the
            ``boto3`` package  [aws]  (FR-06).
  azure  — Azure Key Vault; requires ``AZURE_KEYVAULT_URL`` env var and the
            ``azure-keyvault-secrets`` + ``azure-identity`` packages  [azure]
            (FR-07).
  vault  — HashiCorp Vault; requires ``VAULT_ADDR`` and ``VAULT_TOKEN`` env
            vars and the ``hvac`` package  [vault]  (FR-08).
  plain  — Returns the secret *name* as its own value; useful for testing
            only — never use in production  (FR-09).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Environment variable that selects the secret backend.
_BACKEND_ENV_VAR = "SECRET_BACKEND"

#: Default backend when ``SECRET_BACKEND`` is not set.
_DEFAULT_BACKEND = "env"

#: Regex that matches a secret reference token of the form ``secret::<name>``.
#: The capture group ``name`` holds the secret identifier.
_SECRET_REF_RE = re.compile(r"secret::([A-Za-z0-9_\-/.]+)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def resolve(name: str) -> str:
    """Resolve *name* to its secret value using the configured backend.

    The backend is read from the ``SECRET_BACKEND`` environment variable each
    time this function is called (so it can be changed between test runs
    without restarting the process).

    Parameters
    ----------
    name:
        The logical name of the secret, e.g. ``"my-api-key"`` or
        ``"projects/my-project/secrets/my-secret"``.

    Returns
    -------
    str
        The resolved secret value.

    Raises
    ------
    ValueError
        If the backend name is unknown.
    KeyError
        If the secret cannot be found in the selected backend.
    RuntimeError
        If a required environment variable (e.g. ``GCP_PROJECT_ID``) is not
        set, or if the optional backend library is not installed.
    """
    backend = os.environ.get(_BACKEND_ENV_VAR, _DEFAULT_BACKEND).strip().lower()
    logger.debug("Resolving secret %r via backend %r", name, backend)

    if backend == "env":
        return await _resolve_env(name)
    elif backend == "gcp":
        return await _resolve_gcp(name)
    elif backend == "aws":
        return await _resolve_aws(name)
    elif backend == "azure":
        return await _resolve_azure(name)
    elif backend == "vault":
        return await _resolve_vault(name)
    elif backend == "plain":
        return await _resolve_plain(name)
    else:
        raise ValueError(
            f"Unknown SECRET_BACKEND {backend!r}.  "
            f"Choose one of: env, gcp, aws, azure, vault, plain."
        )


async def resolve_secret_refs(data: Any) -> Any:
    """Recursively replace ``secret::<name>`` tokens in *data*.

    Walks dicts, lists, and plain strings, replacing every occurrence of the
    pattern ``secret::<name>`` with the resolved secret value.  Non-string
    scalars (int, bool, float, None) are returned unchanged.

    Parameters
    ----------
    data:
        Arbitrary Python object, typically the result of ``yaml.safe_load()``.

    Returns
    -------
    Any
        A new object of the same shape with all secret references resolved.

    Raises
    ------
    KeyError / RuntimeError:
        Propagated from :func:`resolve` if a secret cannot be found.
    """
    if isinstance(data, dict):
        return {k: await resolve_secret_refs(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [await resolve_secret_refs(item) for item in data]
    elif isinstance(data, str):
        return await _replace_refs_in_string(data)
    else:
        # int, float, bool, None — pass through untouched
        return data


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _replace_refs_in_string(value: str) -> str:
    """Replace all ``secret::<name>`` occurrences in *value* with resolved secrets."""
    # Fast path: nothing to replace
    if "secret::" not in value:
        return value

    # If the entire string is a single reference, return the raw secret value
    # (preserving non-string types if needed by callers that cast later).
    single_match = _SECRET_REF_RE.fullmatch(value)
    if single_match:
        return await resolve(single_match.group(1))

    # Multiple references embedded in a larger string: replace each one.
    result = value
    for match in _SECRET_REF_RE.finditer(value):
        secret_name = match.group(1)
        secret_value = await resolve(secret_name)
        result = result.replace(match.group(0), secret_value, 1)
    return result


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------


async def _resolve_env(name: str) -> str:
    """FR-04 — resolve from environment variable.

    Converts *name* to UPPERCASE and replaces hyphens with underscores, then
    looks up the resulting name as an environment variable.

    Example::

        name = "my-api-key"   →   env var  MY_API_KEY
    """
    env_name = name.upper().replace("-", "_")
    value = os.environ.get(env_name)
    if value is None:
        raise KeyError(
            f"Secret {name!r} not found in environment "
            f"(looked for env var {env_name!r})."
        )
    return value


async def _resolve_gcp(name: str) -> str:
    """FR-05 — resolve from Google Cloud Secret Manager.

    Requires:
      - ``GCP_PROJECT_ID`` environment variable.
      - ``google-cloud-secret-manager`` package: ``pip install mcp-relay[gcp]``.

    The *name* may be either a bare secret name (e.g. ``"my-secret"``) or a
    fully-qualified resource name
    (``"projects/<project>/secrets/<name>/versions/latest"``).
    """
    try:
        from google.cloud import secretmanager  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "The 'gcp' secret backend requires the google-cloud-secret-manager "
            "package.  Install it with:  pip install mcp-relay[gcp]"
        ) from exc

    project_id = os.environ.get("GCP_PROJECT_ID")
    if not project_id:
        raise RuntimeError(
            "SECRET_BACKEND=gcp requires the GCP_PROJECT_ID environment variable."
        )

    # Accept both bare names and fully-qualified resource names.
    if name.startswith("projects/"):
        resource_name = name if "/versions/" in name else f"{name}/versions/latest"
    else:
        resource_name = (
            f"projects/{project_id}/secrets/{name}/versions/latest"
        )

    import anyio

    client = secretmanager.SecretManagerServiceClient()

    def _sync_fetch() -> str:
        response = client.access_secret_version(name=resource_name)
        return response.payload.data.decode("utf-8")

    # Run the blocking GCP client call in a thread to stay async-friendly.
    return await anyio.to_thread.run_sync(_sync_fetch)


async def _resolve_aws(name: str) -> str:
    """FR-06 — resolve from AWS Secrets Manager.

    Requires:
      - ``AWS_REGION`` environment variable (or standard AWS credential chain).
      - ``boto3`` package: ``pip install mcp-relay[aws]``.
    """
    try:
        import boto3  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "The 'aws' secret backend requires the boto3 package.  "
            "Install it with:  pip install mcp-relay[aws]"
        ) from exc

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise RuntimeError(
            "SECRET_BACKEND=aws requires the AWS_REGION (or AWS_DEFAULT_REGION) "
            "environment variable."
        )

    import anyio

    def _sync_fetch() -> str:
        client = boto3.client("secretsmanager", region_name=region)
        try:
            response = client.get_secret_value(SecretId=name)
        except client.exceptions.ResourceNotFoundException as exc:
            raise KeyError(
                f"Secret {name!r} not found in AWS Secrets Manager "
                f"(region: {region!r})."
            ) from exc
        # SecretString for text secrets; SecretBinary for binary.
        if "SecretString" in response:
            return response["SecretString"]
        import base64
        return base64.b64decode(response["SecretBinary"]).decode("utf-8")

    return await anyio.to_thread.run_sync(_sync_fetch)


async def _resolve_azure(name: str) -> str:
    """FR-07 — resolve from Azure Key Vault.

    Requires:
      - ``AZURE_KEYVAULT_URL`` environment variable
        (e.g. ``https://my-vault.vault.azure.net``).
      - ``azure-keyvault-secrets`` and ``azure-identity`` packages:
        ``pip install mcp-relay[azure]``.
    """
    try:
        from azure.identity import DefaultAzureCredential  # type: ignore[import]
        from azure.keyvault.secrets import SecretClient  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "The 'azure' secret backend requires azure-keyvault-secrets and "
            "azure-identity.  Install them with:  pip install mcp-relay[azure]"
        ) from exc

    vault_url = os.environ.get("AZURE_KEYVAULT_URL")
    if not vault_url:
        raise RuntimeError(
            "SECRET_BACKEND=azure requires the AZURE_KEYVAULT_URL environment "
            "variable (e.g. https://my-vault.vault.azure.net)."
        )

    import anyio

    def _sync_fetch() -> str:
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_url, credential=credential)
        try:
            secret = client.get_secret(name)
        except Exception as exc:
            raise KeyError(
                f"Secret {name!r} not found in Azure Key Vault {vault_url!r}."
            ) from exc
        if secret.value is None:
            raise KeyError(
                f"Secret {name!r} in Azure Key Vault {vault_url!r} has no value."
            )
        return secret.value

    return await anyio.to_thread.run_sync(_sync_fetch)


async def _resolve_vault(name: str) -> str:
    """FR-08 — resolve from HashiCorp Vault.

    Requires:
      - ``VAULT_ADDR`` environment variable (e.g. ``http://127.0.0.1:8200``).
      - ``VAULT_TOKEN`` environment variable.
      - ``hvac`` package: ``pip install mcp-relay[vault]``.

    The *name* should be the full KV path, e.g. ``"secret/data/my-app/api-key"``.
    For KV v2, the payload is read from ``data.data``.  For KV v1, from ``data``.

    If *name* is a bare name (no ``/``), it is treated as a KV v2 path:
    ``secret/data/<name>``.
    """
    try:
        import hvac  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "The 'vault' secret backend requires the hvac package.  "
            "Install it with:  pip install mcp-relay[vault]"
        ) from exc

    vault_addr = os.environ.get("VAULT_ADDR")
    vault_token = os.environ.get("VAULT_TOKEN")

    if not vault_addr:
        raise RuntimeError(
            "SECRET_BACKEND=vault requires the VAULT_ADDR environment variable."
        )
    if not vault_token:
        raise RuntimeError(
            "SECRET_BACKEND=vault requires the VAULT_TOKEN environment variable."
        )

    import anyio

    def _sync_fetch() -> str:
        client = hvac.Client(url=vault_addr, token=vault_token)
        if not client.is_authenticated():
            raise RuntimeError(
                f"HashiCorp Vault authentication failed (VAULT_ADDR={vault_addr!r})."
            )

        # Normalise path — if bare name, default to KV v2 mount at "secret"
        path = name if "/" in name else f"secret/data/{name}"

        # Detect KV v2 vs v1 by path structure
        if "/data/" in path:
            # KV v2: path is e.g. "secret/data/my-app"
            # Split into mount_point and secret_path
            parts = path.split("/", 2)
            mount_point = parts[0]
            secret_path = parts[2] if len(parts) > 2 else parts[1]
            response = client.secrets.kv.v2.read_secret_version(
                path=secret_path,
                mount_point=mount_point,
            )
            data = response.get("data", {}).get("data", {})
        else:
            # KV v1
            response = client.read(path)
            if response is None:
                raise KeyError(f"Secret {name!r} not found in Vault at path {path!r}.")
            data = response.get("data", {})

        if not data:
            raise KeyError(f"Secret {name!r} not found in Vault at path {path!r}.")

        # If the data dict has a single key, return its value.
        # Otherwise, look for common key names: "value", "secret", "password".
        if len(data) == 1:
            return str(next(iter(data.values())))
        for key in ("value", "secret", "password", "token"):
            if key in data:
                return str(data[key])

        raise KeyError(
            f"Secret {name!r} at Vault path {path!r} has multiple keys "
            f"({list(data.keys())!r}) — specify the key explicitly in the name, "
            f"e.g. 'secret/data/my-app/value'."
        )

    return await anyio.to_thread.run_sync(_sync_fetch)


async def _resolve_plain(name: str) -> str:
    """FR-09 — return the secret name as its own value (no-op / testing backend).

    .. warning::
        This backend performs **no secret lookup** — it simply returns *name*
        unchanged.  Use only in unit tests or local development.  Never use in
        production.
    """
    logger.warning(
        "SECRET_BACKEND=plain: returning secret name %r as its own value.  "
        "This backend should NEVER be used in production.",
        name,
    )
    return name
