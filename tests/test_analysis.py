import pandas as pd
from backend.agents.analysis_agents import choose_chart


def test_line_for_time_series():
    df = pd.DataFrame({"month": ["2024-01", "2024-02"], "revenue": [10, 20]})
    assert choose_chart(df) == "line"


def test_bar_for_high_cardinality_category():
    df = pd.DataFrame({"product": list("abcdefgh"), "sales": range(8)})
    assert choose_chart(df) == "bar"


def test_pie_for_low_cardinality_category():
    df = pd.DataFrame({"region": ["N", "S", "E"], "sales": [1, 2, 3]})
    assert choose_chart(df) == "pie"


def test_scatter_for_two_numerics():
    df = pd.DataFrame({"x": [1, 2, 3], "y": [4, 5, 6]})
    assert choose_chart(df) == "scatter"
