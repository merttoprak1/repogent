from example_data.transform import normalize_rows as clean


def test_trims_cells() -> None:
    assert clean([{" name ": " Ada "}]) == [{"name": "Ada"}]
