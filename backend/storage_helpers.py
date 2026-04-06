import re
from typing import Optional


def to_int_or_none(val) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, int):
        return val
    match = re.search(r"\d+", str(val))
    return int(match.group()) if match else None
