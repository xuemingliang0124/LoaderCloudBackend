"""场景管理：CRUD（创建支持多脚本组合 + 线程组级设置，名称唯一）。"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.scenario import Scenario
from app.models.scenario_script import ScenarioScript
from app.models.scenario_script_tg import ScenarioScriptTG
from app.models.script import Script
from app.schemas import (
    ScenarioIn,
    ScenarioOut,
    ScenarioScriptOut,
    ThreadGroupSettingOut,
)
from app.schemas.common import ok
from app.services.exceptions import BusinessError

router = APIRouter()


def _build_scenario_out(scenario: Scenario) -> dict:
    """把 Scenario ORM（含 scripts/thread_groups/script 关联）转为响应 dict。"""
    scripts_out: list[dict] = []
    for ss in scenario.scripts:
        script_name = ss.script.name if ss.script is not None else ""
        tgs = [
            ThreadGroupSettingOut.model_validate(tg).model_dump(mode="json")
            for tg in ss.thread_groups
        ]
        scripts_out.append(
            ScenarioScriptOut(
                id=ss.id,
                script_id=ss.script_id,
                order_index=ss.order_index,
                agent_tags=ss.agent_tags,
                agent_count=ss.agent_count,
                script_name=script_name,
                thread_groups=tgs,
            ).model_dump(mode="json")
        )
    return ScenarioOut(
        id=scenario.id,
        name=scenario.name,
        param_overrides=scenario.param_overrides,
        description=scenario.description,
        scripts=scripts_out,
    ).model_dump(mode="json")


@router.post("/scenarios")
async def create_scenario(
    payload: ScenarioIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    """创建场景：名称不可重复，支持多脚本组合，每个脚本可配置各线程组加压参数。"""
    # 名称唯一校验
    exists = (
        await db.execute(select(Scenario).where(Scenario.name == payload.name))
    ).scalar_one_or_none()
    if exists is not None:
        raise BusinessError(f"场景名称已存在: {payload.name}", code=3010)

    # 校验脚本存在性 + 去重（同一场景内同一脚本只允许出现一次）
    script_ids = [s.script_id for s in payload.scripts]
    if len(script_ids) != len(set(script_ids)):
        raise BusinessError("同一场景内脚本不可重复", code=3011)
    if script_ids:
        scripts = (
            (await db.execute(select(Script).where(Script.id.in_(script_ids))))
            .scalars()
            .all()
        )
        found_ids = {s.id for s in scripts}
        missing = [sid for sid in script_ids if sid not in found_ids]
        if missing:
            raise BusinessError(f"脚本不存在: {missing}", code=3012)

    scenario = Scenario(
        name=payload.name,
        param_overrides=payload.param_overrides,
        description=payload.description,
    )
    db.add(scenario)
    await db.flush()

    for idx, s in enumerate(payload.scripts):
        ss = ScenarioScript(
            scenario_id=scenario.id,
            script_id=s.script_id,
            order_index=s.order_index if s.order_index else idx,
            agent_tags=s.agent_tags,
            agent_count=s.agent_count,
        )
        db.add(ss)
        await db.flush()
        for tg in s.thread_groups:
            db.add(
                ScenarioScriptTG(
                    scenario_script_id=ss.id,
                    thread_group_name=tg.thread_group_name,
                    testclass=tg.testclass,
                    num_threads=tg.num_threads,
                    ramp_time=tg.ramp_time,
                    loops=tg.loops,
                    scheduler=tg.scheduler,
                    duration=tg.duration,
                )
            )

    await db.commit()
    # 回读关联以构造完整响应
    scenario = (
        await db.execute(
            select(Scenario)
            .options(selectinload(Scenario.scripts).selectinload(ScenarioScript.script))
            .options(
                selectinload(Scenario.scripts).selectinload(
                    ScenarioScript.thread_groups
                )
            )
            .where(Scenario.id == scenario.id)
        )
    ).scalar_one()
    return ok(_build_scenario_out(scenario))


@router.get("/scenarios")
async def list_scenarios(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    rows = (
        (
            await db.execute(
                select(Scenario)
                .options(
                    selectinload(Scenario.scripts).selectinload(ScenarioScript.script)
                )
                .options(
                    selectinload(Scenario.scripts).selectinload(
                        ScenarioScript.thread_groups
                    )
                )
                .order_by(Scenario.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return ok([_build_scenario_out(r) for r in rows])
