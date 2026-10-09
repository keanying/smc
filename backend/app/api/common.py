"""统一响应包装与异常处理。"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse


def ok(data: Any = None, message: str = "") -> dict:
    return {"code": 0, "data": data, "message": message}


def fail(message: str, code: int = 1, data: Any = None) -> dict:
    return {"code": code, "data": data, "message": message}


def error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=fail(message, code=status_code))


def not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail=message)


def bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)
