"""Every error response has the shape {"error": {"code": ..., "message": ...}} (spec §5)."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from notetaker.domain.errors import ErrorCode, PipelineError


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _describe_validation(exc: RequestValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    message = first.get("msg", "Invalid request.")
    return f"{location}: {message}" if location else message


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)

    @app.exception_handler(PipelineError)
    async def _pipeline_error(_: Request, exc: PipelineError):
        return JSONResponse(error_body(exc.code, exc.message), status_code=400)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(error_body(ErrorCode.INVALID_REQUEST, _describe_validation(exc)), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(error_body(code, str(exc.detail)), status_code=exc.status_code)
