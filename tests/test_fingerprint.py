from cacheopt.fingerprint import fingerprint


def test_same_shape_different_literals_share_template():
    a = fingerprint("SELECT * FROM orders WHERE region = 'US'")
    b = fingerprint("SELECT * FROM orders WHERE region = 'EU'")
    assert a.template_id == b.template_id
    assert a.cache_key != b.cache_key


def test_whitespace_and_case_insensitive_for_cache_key():
    a = fingerprint("select  *  from Orders where region='US'")
    b = fingerprint("SELECT * FROM orders WHERE region = 'US'")
    assert a.cache_key == b.cache_key


def test_extracts_referenced_tables():
    fp = fingerprint("SELECT f.x FROM fact_orders f JOIN dim_region r ON f.region_id = r.region_id")
    assert fp.tables == ("dim_region", "fact_orders")


def test_unparseable_sql_falls_back_gracefully():
    fp = fingerprint("this is not sql at all {{{")
    assert fp.template_id == fp.cache_key
    assert len(fp.template_id) == 64  # sha256 hex digest
