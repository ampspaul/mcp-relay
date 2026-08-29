from starlette.requests import Request
from starlette.responses import JSONResponse


def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mcp-relay"})
