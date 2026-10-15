"""Missing-value check that works for rows from both JSON and pandas.

Rows read back from Parquet with pandas 3 mark missing strings as NaN (a float), not None,
so `value is None` alone is not a safe test for "this log has no topic3".
"""

import math
from typing import Any

import pandas as pd


def is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def nulls_to_none(frame: pd.DataFrame) -> pd.DataFrame:
    """Replace every missing value with None (e.g. so JSON output holds null, not NaN)."""
    return frame.astype(object).where(frame.notna(), None)
