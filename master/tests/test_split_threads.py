"""线程/TPS 按压力机规格拆分单测：线程最大余数法，TPS 浮点均摊总量守恒。"""

from app.models.scenario_script_tg import ScenarioScriptTG
from app.services.orchestrator import _per_agent_tg_settings, split_threads, split_tps


def test_sum_equals_total() -> None:
    shares = split_threads(100, {"a": 8, "b": 4, "c": 2})
    assert sum(shares.values()) == 100


def test_proportional_by_cores() -> None:
    # 2:1 权重下，总线程 30 恰好整除
    shares = split_threads(30, {"big": 8, "small": 4})
    assert shares == {"big": 20, "small": 10}


def test_every_agent_at_least_one_when_total_ge_agents() -> None:
    # 权重悬殊 + total 刚好等于机数：每台保底 1
    shares = split_threads(3, {"a": 64, "b": 1, "c": 1})
    assert sum(shares.values()) == 3
    assert min(shares.values()) >= 1


def test_zero_weight_treated_as_one() -> None:
    shares = split_threads(10, {"a": 0, "b": 0})
    assert sum(shares.values()) == 10
    assert shares["a"] == 5
    assert shares["b"] == 5


def test_total_less_than_agents_allows_zero() -> None:
    shares = split_threads(1, {"a": 1, "b": 1, "c": 1})
    assert sum(shares.values()) == 1


def test_invalid_input() -> None:
    assert split_threads(0, {"a": 1}) == {}
    assert split_threads(10, {}) == {}


# ---------- TPS 均摊（集群总量守恒，允许小数份额） ----------


def test_tps_sum_equals_total() -> None:
    shares = split_tps(100, {"a": 8, "b": 4, "c": 2})
    assert sum(shares.values()) == 100
    # 与线程同权重：份额按 CPU 比例
    assert shares == {"a": 57.14, "b": 28.57, "c": 14.29}


def test_tps_proportional_exact() -> None:
    shares = split_tps(30, {"big": 8, "small": 4})
    assert shares == {"big": 20.0, "small": 10.0}


def test_tps_zero_means_unlimited_for_all() -> None:
    shares = split_tps(0, {"a": 8, "b": 4})
    assert shares == {"a": 0.0, "b": 0.0}


def test_tps_single_agent_keeps_full_total() -> None:
    assert split_tps(100, {"a": 8}) == {"a": 100.0}


def test_tps_less_than_agents_every_agent_throttled() -> None:
    # 3 TPS / 5 机：每台 0.6（36 TPM），无 0 份额，集群不会因某台不限速而失控
    shares = split_tps(3, {a: 1 for a in ("a", "b", "c", "d", "e")})
    assert set(shares.values()) == {0.6}
    assert sum(shares.values()) == 3
    assert min(shares.values()) > 0


def test_tps_skewed_weights_conserves_total() -> None:
    # 权重悬殊（100:1:1:1:1）+ 小总量：份额合计仍精确守恒
    shares = split_tps(5, {"big": 100, "x1": 1, "x2": 1, "x3": 1, "x4": 1})
    assert round(sum(shares.values()), 2) == 5
    assert min(shares.values()) > 0


def test_tps_empty_weights() -> None:
    assert split_tps(10, {}) == {}


# ---------- per-Agent 设置副本：线程 + TPS 同时拆分 ----------


def _eff_tg(
    tps: int = 100, num_threads: int = 100, enabled: bool = True
) -> ScenarioScriptTG:
    return ScenarioScriptTG(
        scenario_script_id=1,
        thread_group_name="tg1",
        testclass="ThreadGroup",
        enabled=enabled,
        num_threads=num_threads,
        ramp_time=5,
        tps=tps,
        scheduler=True,
        duration=600,
    )


def test_per_agent_settings_splits_threads_and_tps() -> None:
    weights = {"a": 8, "b": 4}
    per_agent = _per_agent_tg_settings([_eff_tg(tps=60, num_threads=60)], weights)
    tg_a, tg_b = per_agent["a"][0], per_agent["b"][0]
    # 线程整数拆分
    assert tg_a.num_threads + tg_b.num_threads == 60
    assert (tg_a.num_threads, tg_b.num_threads) == (40, 20)
    # TPS 浮点均摊，份额守恒（40 + 20 = 60 TPS）
    assert (tg_a.tps, tg_b.tps) == (40.0, 20.0)
    # 非拆分字段原样复制
    assert tg_a.ramp_time == 5 and tg_b.ramp_time == 5
    assert tg_a.scheduler is True and tg_a.duration == 600
    assert tg_a.enabled is True and tg_b.enabled is True


def test_per_agent_settings_carries_disabled_flag() -> None:
    """禁用线程组的开关原样复制到每个 Agent 副本。"""
    per_agent = _per_agent_tg_settings([_eff_tg(enabled=False)], {"a": 8, "b": 4})
    assert all(tgs[0].enabled is False for tgs in per_agent.values())


def test_per_agent_settings_zero_tps_all_unlimited() -> None:
    per_agent = _per_agent_tg_settings(
        [_eff_tg(tps=0, num_threads=10)], {"a": 8, "b": 2}
    )
    assert all(tgs[0].tps == 0.0 for tgs in per_agent.values())


def test_per_agent_settings_single_agent_full_share() -> None:
    per_agent = _per_agent_tg_settings([_eff_tg()], {"a": 8})
    tg = per_agent["a"][0]
    assert tg.num_threads == 100
    assert tg.tps == 100.0


def test_per_agent_settings_matches_by_name_and_type() -> None:
    tg1 = _eff_tg(tps=30, num_threads=30)
    tg2 = ScenarioScriptTG(
        scenario_script_id=1,
        thread_group_name="tg2",
        testclass="ThreadGroup",
        num_threads=30,
        ramp_time=0,
        tps=30,
        scheduler=True,
        duration=600,
    )
    per_agent = _per_agent_tg_settings([tg1, tg2], {"a": 8, "b": 4})
    names = {t.thread_group_name for t in per_agent["a"]}
    assert names == {"tg1", "tg2"}
    for aid in ("a", "b"):
        assert sum(t.tps for t in per_agent[aid]) in (40.0, 20.0)
