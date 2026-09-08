"""指标查询：ES 聚合透出，供前端曲线渲染。"""

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.schemas.common import ok
from app.services import es_client

router = APIRouter()


@router.get("/metrics/timeseries")
async def timeseries(
    run_no: str,
    start: int,
    end: int,
    interval: int = 15,
    _: str = Depends(get_current_user),
) -> dict:
    """按 label 维度聚合时间序列（start/end 为 unix 秒）。"""
    return ok(await es_client.query_timeseries(run_no, start, end, interval))
