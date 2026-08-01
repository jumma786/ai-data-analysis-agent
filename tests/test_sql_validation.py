from backend.agents.sql_validation import validate_sql, enforce_limit


def test_allows_plain_select():
    assert validate_sql("SELECT * FROM sales").ok


def test_allows_cte():
    assert validate_sql("WITH t AS (SELECT 1) SELECT * FROM t").ok


def test_blocks_drop():
    assert not validate_sql("DROP TABLE users").ok


def test_blocks_delete():
    assert not validate_sql("DELETE FROM users WHERE id=1").ok


def test_blocks_update():
    assert not validate_sql("UPDATE users SET x=1").ok


def test_blocks_stacked_statements():
    assert not validate_sql("SELECT 1; DROP TABLE t").ok


def test_blocks_keyword_hidden_in_comment_free_but_present():
    assert not validate_sql("SELECT * FROM t; DELETE FROM t").ok


def test_comment_hidden_drop_still_blocked_by_readonly_rule():
    # A leading comment then DROP -> not starting with SELECT after strip.
    r = validate_sql("/* comment */ DROP TABLE t")
    assert not r.ok


def test_enforce_limit_adds_limit():
    out = enforce_limit("SELECT * FROM t", 100)
    assert "LIMIT 100" in out


def test_enforce_limit_respects_existing():
    out = enforce_limit("SELECT * FROM t LIMIT 5", 100)
    assert out.count("LIMIT") == 1
