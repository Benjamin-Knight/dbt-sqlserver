import pytest

from dbt.tests.util import get_connection, run_dbt, run_dbt_and_capture

delta_model_sql = """
{{ config(materialized="table", table_refresh_method="delta", as_columnstore=False) }}
select id, val, ci_val, loaded_at from {{ target.schema }}.delta_src
"""

delta_xml_model_sql = """
{{ config(materialized="table", table_refresh_method="delta", as_columnstore=False) }}
select 1 as id, cast('<a/>' as xml) as doc
"""


def execute(project, sql, fetch=False):
    with get_connection(project.adapter):
        _, table = project.adapter.execute(sql, fetch=fetch)
    return table.rows if fetch else None


def differences(project):
    """Rows of the model and its source that differ in value or byte length, both ways."""
    keys = "id, val, DATALENGTH(val) AS val_len, ci_val, DATALENGTH(ci_val) AS ci_len, loaded_at"
    src = f"SELECT {keys} FROM {project.test_schema}.delta_src"
    live = f"SELECT {keys} FROM {project.test_schema}.delta_model"
    return execute(
        project,
        f"SELECT (SELECT COUNT(*) FROM ({src} EXCEPT {live}) a)"
        f" + (SELECT COUNT(*) FROM ({live} EXCEPT {src}) b)"
        f" + ABS((SELECT COUNT(*) FROM {project.test_schema}.delta_src)"
        f"     - (SELECT COUNT(*) FROM {project.test_schema}.delta_model))",
        fetch=True,
    )[0][0]


def refresh(project):
    """Run the model; return the swap path it took."""
    _, log = run_dbt_and_capture(["run", "--select", "delta_model"])
    marker = "delta refresh of "
    line = next(line for line in log.splitlines() if marker in line)
    return line.rsplit(": ", 1)[1].strip()


class TestDeltaRefresh:
    @pytest.fixture(scope="class")
    def models(self):
        return {"delta_model.sql": delta_model_sql, "delta_xml_model.sql": delta_xml_model_sql}

    def test_delta_paths(self, project):
        s = project.test_schema
        # ci_val is case-insensitive under the test database's CS collation.
        execute(
            project,
            f"""
            CREATE TABLE {s}.delta_src (
              id int NOT NULL, val varchar(20) NULL,
              ci_val varchar(20) COLLATE Latin1_General_CI_AS NULL, loaded_at datetime2 NOT NULL);
            WITH n AS (SELECT TOP (100) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n
                       FROM sys.all_objects)
            INSERT {s}.delta_src
            SELECT n, IIF(n % 10 = 0, NULL, CONCAT('v', n)), 'abc', '2026-01-01' FROM n;
            """,
        )
        run_dbt(["run", "--select", "delta_model"])  # creates the table; no swap yet

        assert refresh(project) == "delta"
        assert differences(project) == 0

        # A few rows, a NULL becoming a value, and a trailing-space-only change.
        execute(
            project,
            f"""
            UPDATE {s}.delta_src SET loaded_at = '2026-01-02' WHERE id <= 5;
            UPDATE {s}.delta_src SET val = 'filled' WHERE id = 10;
            UPDATE {s}.delta_src SET val = 'v21 ' WHERE id = 21;
            """,
        )
        assert refresh(project) == "delta"
        assert differences(project) == 0

        # Case-only change under CI: equal under the column's collation, so kept as it was.
        execute(project, f"UPDATE {s}.delta_src SET ci_val = 'ABC' WHERE id = 30")
        assert refresh(project) == "delta"
        assert (
            execute(project, f"SELECT ci_val FROM {s}.delta_model WHERE id = 30", fetch=True)[0][0]
            == "abc"
        )
        execute(project, f"UPDATE {s}.delta_src SET ci_val = 'abc' WHERE id = 30")

        execute(project, f"UPDATE {s}.delta_src SET loaded_at = '2026-01-03' WHERE id <= 50")
        assert refresh(project) == "churn"
        assert differences(project) == 0

        # Duplicates in the build, then left behind in the target once the build is distinct again.
        execute(project, f"INSERT {s}.delta_src SELECT * FROM {s}.delta_src WHERE id = 1")
        assert refresh(project) == "duplicates"
        assert differences(project) == 0
        execute(project, f"DELETE TOP (1) FROM {s}.delta_src WHERE id = 1")
        assert refresh(project) == "count"
        assert differences(project) == 0

        assert (
            execute(
                project,
                f"SELECT COUNT(*) FROM sys.tables"
                f" WHERE schema_id = SCHEMA_ID('{s}') AND name LIKE '%[_][_]dbt[_]%'",
                fetch=True,
            )[0][0]
            == 0
        )

    def test_unsupported_type_warns_and_swaps(self, project):
        run_dbt(["run", "--select", "delta_xml_model"])
        _, log = run_dbt_and_capture(["run", "--select", "delta_xml_model"])
        assert "cannot compare column(s) doc" in log
        assert (
            execute(
                project,
                f"SELECT CAST(doc AS nvarchar(max)) FROM {project.test_schema}.delta_xml_model",
                fetch=True,
            )[0][0]
            == "<a/>"
        )
