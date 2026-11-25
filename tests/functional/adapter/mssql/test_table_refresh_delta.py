import pytest

from dbt.tests.util import get_connection, run_dbt, run_dbt_and_capture

delta_model_sql = """
{{ config(materialized="table", table_refresh_method="delta", as_columnstore=False) }}
select id, val, ci_val, sys_val, at_tz, loaded_at from {{ target.schema }}.delta_src
"""

delta_variant_model_sql = """
{{ config(materialized="table", table_refresh_method="delta", as_columnstore=False) }}
select 1 as id, cast('a' as sql_variant) as doc
"""


def execute(project, sql, fetch=False):
    with get_connection(project.adapter):
        _, table = project.adapter.execute(sql, fetch=fetch)
    return table.rows if fetch else None


def differences(project):
    """Rows whose value, byte length, offset or copy count differs between model and source."""
    keys = "id, val, DATALENGTH(val), ci_val, DATALENGTH(ci_val), sys_val, DATALENGTH(sys_val)"
    keys = f"{keys}, at_tz, DATEPART(TZOFFSET, at_tz), loaded_at".replace(
        "ci_val", "ci_val COLLATE Latin1_General_CI_AS"
    )
    names = "id, val, val_len, ci_val, ci_len, sys_val, sys_len, at_tz, tz, loaded_at"
    src, live = (
        f"SELECT {names}, COUNT(*) AS copies"
        f" FROM (SELECT {keys} FROM {project.test_schema}.{table}) t ({names}) GROUP BY {names}"
        for table in ("delta_src", "delta_model")
    )
    return execute(
        project,
        f"SELECT (SELECT COUNT(*) FROM ({src} EXCEPT {live}) a)"
        f" + (SELECT COUNT(*) FROM ({live} EXCEPT {src}) b) AS differences",
        fetch=True,
    )[0][0]


def refresh(project, expect_pass=True, model="delta_model"):
    """Run the model; return the reason it fell back to the full swap, or None."""
    _, log = run_dbt_and_capture(["run", "--select", model], expect_pass=expect_pass)
    marker = "fell back to the full swap: "
    return next(
        (line.split(marker)[1].strip() for line in log.splitlines() if marker in line), None
    )


class TestDeltaRefresh:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "delta_model.sql": delta_model_sql,
            "delta_variant_model.sql": delta_variant_model_sql,
        }

    def test_delta_paths(self, project):
        s = project.test_schema

        def ci_val(row_id):
            return execute(
                project, f"SELECT ci_val FROM {s}.delta_model WHERE id = {row_id}", fetch=True
            )[0][0]

        # ci_val is case-insensitive under the test database's CS collation.
        execute(
            project,
            f"""
            CREATE TABLE {s}.delta_src (
              id int NOT NULL, val varchar(20) NULL,
              ci_val varchar(20) COLLATE Latin1_General_CI_AS NULL,
              sys_val sysname NOT NULL, at_tz datetimeoffset NOT NULL,
              loaded_at datetime2 NOT NULL);
            WITH n AS (SELECT TOP (100) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n
                       FROM sys.all_objects)
            INSERT {s}.delta_src
            SELECT n, IIF(n % 10 = 0, NULL, CONCAT('v', n)), 'abc', 's',
                   '2026-01-01 10:00 +00:00', '2026-01-01' FROM n;
            """,
        )
        run_dbt(["run", "--select", "delta_model"])  # creates the table; no swap yet

        assert refresh(project) is None
        assert differences(project) == 0

        # A few rows, a NULL becoming a value, and changes = misses: trailing spaces, and an
        # offset that keeps the instant.
        execute(
            project,
            f"""
            UPDATE {s}.delta_src SET loaded_at = '2026-01-02' WHERE id <= 5;
            UPDATE {s}.delta_src SET val = 'filled' WHERE id = 10;
            UPDATE {s}.delta_src SET val = 'v21 ' WHERE id = 21;
            UPDATE {s}.delta_src SET sys_val = 's ' WHERE id = 22;
            UPDATE {s}.delta_src SET at_tz = SWITCHOFFSET(at_tz, '+01:00') WHERE id = 23;
            """,
        )
        assert refresh(project) is None
        assert differences(project) == 0

        # Case-only change under CI: equal under the column's collation, so the delta keeps the
        # old value. Every full swap below then shows itself by writing the new one.
        execute(project, f"UPDATE {s}.delta_src SET ci_val = 'ABC' WHERE id = 30")
        assert refresh(project) is None
        assert ci_val(30) == "abc"

        execute(project, f"UPDATE {s}.delta_src SET loaded_at = '2026-01-03' WHERE id <= 50")
        assert refresh(project) == "churn"
        assert differences(project) == 0
        assert ci_val(30) == "ABC"

        # Duplicates in the build; then moved to another row, so the target's copies and the
        # build's cancel out in a row count; then left behind in the target alone.
        execute(project, f"INSERT {s}.delta_src SELECT * FROM {s}.delta_src WHERE id = 1")
        assert refresh(project) == "duplicates"
        assert differences(project) == 0
        execute(
            project,
            f"""
            DELETE TOP (1) FROM {s}.delta_src WHERE id = 1;
            INSERT {s}.delta_src SELECT * FROM {s}.delta_src WHERE id = 2;
            """,
        )
        assert refresh(project) == "duplicates"
        assert differences(project) == 0
        execute(project, f"DELETE TOP (1) FROM {s}.delta_src WHERE id = 2")
        assert refresh(project) == "count"
        assert differences(project) == 0

        # A swap that fails part-way rolls back whole, and the next run clears its leftovers.
        execute(
            project,
            f"""
            ALTER TABLE {s}.delta_model ADD CONSTRAINT delta_ck CHECK (id < 1000);
            DELETE {s}.delta_src WHERE id = 99;
            INSERT {s}.delta_src SELECT 1000, val, ci_val, sys_val, at_tz, loaded_at
            FROM {s}.delta_src WHERE id = 1;
            """,
        )
        refresh(project, expect_pass=False)
        assert differences(project) == 2
        execute(project, f"DELETE {s}.delta_src WHERE id = 1000")
        assert refresh(project) is None
        assert differences(project) == 0

        # The build's collation no longer matches the target's: EXCEPT cannot compare them.
        execute(
            project,
            f"""
            ALTER TABLE {s}.delta_src
              ALTER COLUMN ci_val varchar(20) COLLATE Latin1_General_CS_AS NULL;
            UPDATE {s}.delta_src SET ci_val = 'Abc' WHERE id = 30;
            """,
        )
        _, log = run_dbt_and_capture(["run", "--select", "delta_model"])
        assert "cannot compare column(s) ci_val" in log
        assert ci_val(30) == "Abc"

        assert (
            execute(
                project,
                f"SELECT COUNT(*) AS leftovers FROM sys.tables"
                f" WHERE schema_id = SCHEMA_ID('{s}') AND name LIKE '%[_][_]dbt[_]%'",
                fetch=True,
            )[0][0]
            == 0
        )

    def test_unsupported_type_warns_and_swaps(self, project):
        run_dbt(["run", "--select", "delta_variant_model"])
        _, log = run_dbt_and_capture(["run", "--select", "delta_variant_model"])
        assert "cannot compare column(s) doc" in log
        assert (
            execute(
                project,
                f"SELECT CAST(doc AS varchar(10)) AS doc"
                f" FROM {project.test_schema}.delta_variant_model",
                fetch=True,
            )[0][0]
            == "a"
        )


class TestDeltaRefreshWithoutDbtTransactions(TestDeltaRefresh):
    """The swap batch carries its own BEGIN and COMMIT."""

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"flags": {"dbt_sqlserver_use_dbt_transactions": False}}


delta_key_model_sql = """
{{ config(materialized="table", table_refresh_method="delta", delta_key="id", as_columnstore=False,
          query_options={"max_grant_percent": 50}) }}
select id, grp, val from {{ target.schema }}.key_src
"""

delta_composite_key_model_sql = """
{{ config(materialized="table", table_refresh_method="delta", delta_key=["ID", "grp"],
          as_columnstore=False) }}
select id, grp, val from {{ target.schema }}.key_src
"""

delta_bad_key_model_sql = """
{{ config(materialized="table", table_refresh_method="delta", delta_key=["id", "nope"],
          as_columnstore=False) }}
select id, grp, val from {{ target.schema }}.key_src
"""


class TestDeltaKey:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "key_model.sql": delta_key_model_sql,
            "key_composite_model.sql": delta_composite_key_model_sql,
            "key_bad_model.sql": delta_bad_key_model_sql,
        }

    def test_key_narrows_the_check_without_trusting_it(self, project):
        s = project.test_schema
        execute(
            project,
            f"""
            CREATE TABLE {s}.key_src (id int NOT NULL, grp int NOT NULL, val varchar(20) NULL);
            WITH n AS (SELECT TOP (100) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n
                       FROM sys.all_objects)
            INSERT {s}.key_src SELECT n, n % 5, CONCAT('v', n) FROM n;
            """,
        )
        for model in ("key_model", "key_composite_model"):
            run_dbt(["run", "--select", model])
            assert refresh(project, model=model) is None

        # Unique key, changed row.
        execute(project, f"UPDATE {s}.key_src SET val = 'z' WHERE id = 2")
        assert refresh(project, model="key_model") is None

        # Key repeats but the rows differ: the key proves nothing, the exact check passes.
        execute(project, f"INSERT {s}.key_src VALUES (1, 1, 'other')")
        assert refresh(project, model="key_model") is None
        assert refresh(project, model="key_composite_model") is None

        # Key repeats and so does the whole row: only the exact check can see it.
        execute(project, f"INSERT {s}.key_src VALUES (1, 1, 'other')")
        assert refresh(project, model="key_model") == "duplicates"
        assert refresh(project, model="key_composite_model") == "duplicates"
        assert (
            execute(project, f"SELECT COUNT(*) FROM {s}.key_model", fetch=True)[0][0]
            == execute(project, f"SELECT COUNT(*) FROM {s}.key_src", fetch=True)[0][0]
        )

    def test_unknown_key_column_is_named(self, project):
        run_dbt(["run", "--select", "key_bad_model"])
        results = run_dbt(["run", "--select", "key_bad_model"], expect_pass=False)
        assert "delta_key column 'nope'" in results[0].message


delta_ci_model_sql = """
{{ config(materialized="table", table_refresh_method="delta", as_columnstore=False) }}
select id, s, n, b, f from {{ target.schema }}.ci_src
"""


class TestDeltaDuplicateCheckEquality:
    """No delta_key: the checksum-narrowed check must flag exactly what the full GROUP BY does."""

    @pytest.fixture(scope="class")
    def models(self):
        return {"ci_model.sql": delta_ci_model_sql}

    def test_checksum_agrees_with_group_by(self, project):
        s = project.test_schema
        # Rows the GROUP BY keys treat as equal must share a CHECKSUM, or stage 1 misses them.
        values = (
            "('abc'),('ABC'),('Abc'),('àbc'),('ÀBÇ'),('abç'),('abc '),('ABC '),('abç  '),"
            "('aa'),('Aa'),('áa'),('ae'),('æ'),('ß'),('ss'),('SS'),('o'),('ö'),('Ö'),(''),(' '),"
            "(NULL)"
        )
        violations = sum(
            execute(
                project,
                f"""
                SELECT COUNT(*) FROM (
                  SELECT 1 AS x FROM (SELECT CAST(s AS varchar(10)) COLLATE {coll} AS s
                                      FROM (VALUES {values}) v(s)) t
                  GROUP BY s, DATALENGTH(s)
                  HAVING COUNT(DISTINCT CHECKSUM(s, DATALENGTH(s))) > 1) q
                """,
                fetch=True,
            )[0][0]
            for coll in (
                "Latin1_General_CI_AI",
                "Latin1_General_CI_AS",
                "Latin1_General_CS_AS",
                "Latin1_General_BIN2",
            )
        )
        assert violations == 0

        execute(
            project,
            f"""
            CREATE TABLE {s}.ci_src (
              id int NOT NULL,
              s varchar(20) COLLATE Latin1_General_CI_AI NULL,
              n nvarchar(20) COLLATE Latin1_General_CI_AI NULL,
              b varbinary(8) NULL, f float NULL);
            INSERT {s}.ci_src VALUES
              (1, 'abc', N'abc', 0x01, 0), (2, 'abc ', N'abc ', 0x0100, 1),
              (3, NULL, NULL, NULL, NULL), (4, 'x', N'x', 0x02, 2);
            """,
        )
        run_dbt(["run", "--select", "ci_model"])
        assert refresh(project, model="ci_model") is None

        # Trailing spaces, varbinary zero padding and NULL-vs-value are distinct rows.
        execute(
            project,
            f"""
            INSERT {s}.ci_src VALUES
              (1, 'abc  ', N'abc', 0x01, 0), (1, 'abc', N'abc', 0x010000, 0),
              (1, NULL, N'abc', 0x01, 0);
            """,
        )
        assert refresh(project, model="ci_model") is None

        # Equal under CI_AI: case-only, accent-only, both.
        execute(project, f"INSERT {s}.ci_src VALUES (50, 'abc', N'abc', 0x01, 0)")
        assert refresh(project, model="ci_model") is None
        for dup in ("'ABC', N'abc'", "'ábc', N'abc'", "'abc', N'ÀBC'"):
            execute(project, f"INSERT {s}.ci_src VALUES (50, {dup}, 0x01, 0)")
            assert refresh(project, model="ci_model") == "duplicates", dup
            execute(project, f"DELETE TOP (1) FROM {s}.ci_src WHERE id = 50")
            assert refresh(project, model="ci_model") == "count", dup
            assert refresh(project, model="ci_model") is None, dup

        # NULLs compare equal to each other, and 0 equals -0.
        execute(project, f"INSERT {s}.ci_src VALUES (3, NULL, NULL, NULL, NULL)")
        assert refresh(project, model="ci_model") == "duplicates"
