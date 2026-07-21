def clamp(value: int, lower: int, upper: int) -> int:
    return min(max(value, lower), upper)
