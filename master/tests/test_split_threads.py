"""线程按压力机规格拆分（最大余数法）单测。"""

from app.services.orchestrator import split_threads


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
