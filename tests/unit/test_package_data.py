from importlib.resources import files

import repogent


def test_package_declares_inline_type_information() -> None:
    assert files(repogent).joinpath("py.typed").is_file()  # noqa: S101
