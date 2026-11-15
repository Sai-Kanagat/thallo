"""Uniform errors. A response says what went wrong in fixed words; it never echoes the request.

FastAPI's default 422 body quotes the submitted input (a pasted key included), so it is replaced:
the body names the fields that failed and nothing they held.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("thallo.api")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def not_found() -> ApiError:
    """The same answer for "missing" and "belongs to another tenant"."""
    return ApiError(404, "not_found", "not found")


def unauthenticated() -> ApiError:
    return ApiError(401, "unauthenticated", "sign in first")


def forbidden(message: str = "your role does not allow this") -> ApiError:
    return ApiError(403, "forbidden", message)


def conflict(message: str) -> ApiError:
    return ApiError(409, "conflict", message)


def bad_request(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def _body(code: str, message: str, **extra: object) -> dict:
    return {"error": code, "message": message, **extra}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted({".".join(str(p) for p in e.get("loc", ()) if p != "body") for e in exc.errors()})
        return JSONResponse(_body("invalid_request", "the request is not valid", fields=fields), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return JSONResponse(_body("not_found", "not found"), status_code=404)
        if exc.status_code == 405:
            return JSONResponse(_body("method_not_allowed", "method not allowed"), status_code=405)
        return JSONResponse(_body("error", "the request failed"), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _crash(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error type=%s", type(exc).__name__)  # the type only: messages can carry values
        return JSONResponse(_body("internal", "something went wrong"), status_code=500)


__all__ = ["ApiError", "bad_request", "conflict", "forbidden", "install", "not_found", "unauthenticated"]
