from example_cli.__main__ import greeting


def test_greeting() -> None:
    assert greeting("Ada") == "Hello, Ada!"
