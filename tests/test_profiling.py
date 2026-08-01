import pandas as pd
from backend.services.profiling import profile_dataset, clean_dataset


def test_profile_counts():
    df = pd.DataFrame({"a": [1, 2, 2, None], "b": ["x", "y", "y", "z"]})
    p = profile_dataset(df)
    assert p["rows"] == 4
    assert p["columns_count"] == 2
    assert p["missing_detected"] is True
    assert p["duplicate_records"] == 1


def test_clean_removes_dupes():
    df = pd.DataFrame({"a": [1, 1], "b": [2, 2]})
    assert len(clean_dataset(df)) == 1
