def normalize_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{key.strip(): value.strip() for key, value in row.items()} for row in rows]
