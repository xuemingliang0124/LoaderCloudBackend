"""插件同步差异集计算单测。

不依赖 DB / WS：测 on_heartbeat 内的 set 比对逻辑（提取为纯函数 _diff_sets）。
验证：
- 缺失集 = expected - reported
- 多余集 = reported - expected
- 无差异时不推任何消息
"""

from app.services.plugin_sync import _diff_sets


def test_no_diff_when_sets_equal() -> None:
    expected = {"sha_a", "sha_b"}
    reported = {"sha_a", "sha_b"}
    missing, extra = _diff_sets(expected, reported)
    assert missing == set()
    assert extra == set()


def test_missing_when_agent_lacks_plugin() -> None:
    expected = {"sha_a", "sha_b", "sha_c"}
    reported = {"sha_a"}  # Agent 只装了 a
    missing, extra = _diff_sets(expected, reported)
    assert missing == {"sha_b", "sha_c"}
    assert extra == set()


def test_extra_when_agent_has_disabled_plugin() -> None:
    expected = {"sha_a"}  # 全局只启用 a
    reported = {"sha_a", "sha_b"}  # Agent 还装着已禁用的 b
    missing, extra = _diff_sets(expected, reported)
    assert missing == set()
    assert extra == {"sha_b"}


def test_both_missing_and_extra() -> None:
    expected = {"sha_a", "sha_b"}
    reported = {"sha_b", "sha_c"}  # 缺 a，多 c
    missing, extra = _diff_sets(expected, reported)
    assert missing == {"sha_a"}
    assert extra == {"sha_c"}


def test_empty_reported_all_missing() -> None:
    expected = {"sha_a", "sha_b"}
    reported = set()
    missing, extra = _diff_sets(expected, reported)
    assert missing == {"sha_a", "sha_b"}
    assert extra == set()


def test_empty_expected_all_extra() -> None:
    expected = set()
    reported = {"sha_a"}
    missing, extra = _diff_sets(expected, reported)
    assert missing == set()
    assert extra == {"sha_a"}
