from starlette.requests import Request
from starlette.responses import JSONResponse

from ..observability import metrics


def metrics_handler(_: Request) -> JSONResponse:
    return JSONResponse(metrics.snapshot())
