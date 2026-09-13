"""指标查询：ES 聚合透出，供前端曲线渲染。

run 指标属于其归属项目：经 run_no → 场景 → 项目派生归属，
要求项目内 viewer 及以上（与 WS run 流共用 ensure_run_visible），
非成员无法读取他人项目的执行数据。
"""

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, ensure_run_visible, get_current_user
from app.db.session import get_db
from app.schemas.common import ok
from app.services import es_client

router = APIRouter()


@router.get("/metrics/timeseries")
async def timeseries(
    run_no: str,
    start: int,
    end: int,
    interval: int = 15,
    sample_type: Literal["request", "transaction"] | None = None,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """按 label 维度聚合时间序列（start/end 为 unix 秒）。

    sample_type 可选过滤：request=仅请求、transaction=仅事务；
    缺省时全量返回（点内含 sample_type 字段供前端分组）。
    """
    await ensure_run_visible(db, run_no, user)
    return ok(
        await es_client.query_timeseries(
            run_no, start, end, interval, sample_type=sample_type
        )
    )
