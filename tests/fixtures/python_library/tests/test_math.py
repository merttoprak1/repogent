from example_math import clamp as limit


def test_limits_values() -> None:
    assert limit(9, 0, 5) == 5
