"""统一响应包裹与分页参数。"""

from typing import Any

from pydantic import BaseModel, Field


def ok(data: Any = None, message: str = "ok") -> dict:
    """统一响应包裹：{"code": 0, "message": "ok", "data": ...}。"""
    return {"code": 0, "message": message, "data": data}


class PageQuery(BaseModel):
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)
