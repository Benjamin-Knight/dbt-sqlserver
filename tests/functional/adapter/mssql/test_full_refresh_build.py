import pytest

from dbt.tests.util import run_dbt, run_dbt_and_capture

models__invalid_value_sql = """
{{
  config(
    materialized = "table",
    full_refresh_build = "bogus"
  )
}}

select 1 as column_a

"""


class TestFullRefreshBuildInvalid:
    @pytest.fixture(scope="class")
    def models(self):
        return {"invalid_value.sql": models__invalid_value_sql}

    def test_invalid_value(self, project):
        _, output = run_dbt_and_capture(["run", "--models", "invalid_value"], expect_pass=False)
        assert "Invalid full_refresh_build" in output
        assert "bogus" in output
        assert "heap_then_index" in output
        assert "prebuilt" in output


models__cci_prebuilt_sql = """
{{
  config(
    materialized = "table",
    full_refresh_build = "prebuilt"
  )
}}

select *
from (
  select 1 as column_a, 2 as column_b
  union all
  select 3, 4
) ordered_inner
order by column_a
offset 0 rows

"""


def get_cci_indexes(project, unique_schema, table_name):
    sql = f"""
    select i.[name], i.type_desc
    from sys.indexes i
    where i.object_id = OBJECT_ID('{unique_schema}.{table_name}')
      and i.index_id > 0
    """
    return project.run_sql(sql, fetch="all")


class TestFullRefreshBuildColumnstore:
    @pytest.fixture(scope="class")
    def models(self):
        return {"cci_prebuilt.sql": models__cci_prebuilt_sql}

    def test_cci_prebuilt_lifecycle(self, project, unique_schema):
        # First build: prebuilt path creates the table empty with its CCI in
        # place, then bulk-loads. (Tiny test row counts land in delta
        # rowgroups rather than compressed segments - the <102,400 rows/thread
        # threshold - so we assert physical design, not rowgroup state.)
        _, output = run_dbt_and_capture(["run", "--models", "cci_prebuilt"])
        assert "full_refresh_build=prebuilt" in output

        indexes = get_cci_indexes(project, unique_schema, "cci_prebuilt")
        assert len(indexes) == 1
        assert indexes[0][1] == "CLUSTERED COLUMNSTORE"
        # Named for the TARGET relation, not the dbt_tmp intermediate.
        assert "dbt_tmp" not in indexes[0][0]
        first_name = indexes[0][0]

        # data made it through the two-step load
        rows = project.run_sql(f"select count(*) from {unique_schema}.cci_prebuilt", fetch="one")
        assert rows[0] == 2

        # Rebuild via the intermediate->swap path: still exactly one CCI,
        # stable name, no leftover heap or backup copies.
        _, output = run_dbt_and_capture(["run", "--models", "cci_prebuilt", "--full-refresh"])
        assert "full_refresh_build=prebuilt" in output
        indexes = get_cci_indexes(project, unique_schema, "cci_prebuilt")
        assert len(indexes) == 1
        assert indexes[0][0] == first_name
        leftovers = project.run_sql(
            f"""select count(*) from sys.tables t
                join sys.schemas s on s.schema_id = t.schema_id
                where s.name = '{unique_schema}'
                and (t.name like '%__dbt_tmp%' or t.name like '%__dbt_backup%')""",
            fetch="one",
        )
        assert leftovers[0] == 0


models__rowstore_prebuilt_sql = """
{{
  config(
    materialized = "table",
    as_columnstore = False,
    full_refresh_build = "prebuilt",
    indexes=[
      {'columns': ['column_b'], 'type': 'clustered', 'data_compression': 'page'},
      {'columns': ['column_a'], 'type': 'nonclustered'},
    ]
  )
}}

select 1 as column_a, 2 as column_b

"""

models__fallback_no_clustered_sql = """
{{
  config(
    materialized = "table",
    as_columnstore = False,
    full_refresh_build = "prebuilt",
    indexes=[
      {'columns': ['column_a'], 'type': 'nonclustered'},
    ]
  )
}}

select 1 as column_a, 2 as column_b

"""

models__prebuilt_default_cci_clustered_sql = """
{{
  config(
    materialized = "table",
    full_refresh_build = "prebuilt",
    indexes=[
      {'columns': ['column_a'], 'type': 'clustered'},
    ]
  )
}}

select 1 as column_a

"""


def get_rowstore_indexes(project, unique_schema, table_name):
    sql = f"""
    select i.[name], i.type_desc, isnull(max(p.data_compression_desc), '') as compression
    from sys.indexes i
    left join sys.partitions p
      on p.object_id = i.object_id and p.index_id = i.index_id
    where i.object_id = OBJECT_ID('{unique_schema}.{table_name}')
      and i.index_id > 0
    group by i.[name], i.type_desc
    """
    return {row[1]: (row[0], row[2]) for row in project.run_sql(sql, fetch="all")}


class TestFullRefreshBuildRowstore:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "rowstore_prebuilt.sql": models__rowstore_prebuilt_sql,
            "fallback_no_clustered.sql": models__fallback_no_clustered_sql,
            "prebuilt_default_cci_clustered.sql": models__prebuilt_default_cci_clustered_sql,
        }

    def test_rowstore_prebuilt(self, project, unique_schema):
        _, output = run_dbt_and_capture(["run", "--models", "rowstore_prebuilt"])
        assert "full_refresh_build=prebuilt" in output

        by_type = get_rowstore_indexes(project, unique_schema, "rowstore_prebuilt")
        assert set(by_type) == {"CLUSTERED", "NONCLUSTERED"}

        clustered_name, clustered_compression = by_type["CLUSTERED"]
        # Named for the TARGET relation hash (pre-created on the intermediate,
        # surviving the swap with the name create_indexes expects).
        assert clustered_name.startswith("dbt_idx_")
        # Compress-on-insert: PAGE already applied, no rebuild needed.
        assert clustered_compression == "PAGE"
        # The NCI came from create_indexes(target) after the swap.
        assert by_type["NONCLUSTERED"][0].startswith("dbt_idx_")

        rows = project.run_sql(
            f"select count(*) from {unique_schema}.rowstore_prebuilt", fetch="one"
        )
        assert rows[0] == 1

        # Rebuild: stable names, still exactly one clustered.
        run_dbt(["run", "--models", "rowstore_prebuilt"])
        second = get_rowstore_indexes(project, unique_schema, "rowstore_prebuilt")
        assert second == by_type

    def test_fallback_without_clustered(self, project, unique_schema):
        _, output = run_dbt_and_capture(["run", "--models", "fallback_no_clustered"])
        assert "falling back to heap_then_index" in output

        by_type = get_rowstore_indexes(project, unique_schema, "fallback_no_clustered")
        assert set(by_type) == {"NONCLUSTERED"}

    def test_prebuilt_clustered_with_default_columnstore_errors(self, project):
        # as_columnstore defaults true: the existing cross-config validation
        # must reject the clustered rowstore entry with guidance.
        _, output = run_dbt_and_capture(
            ["run", "--models", "prebuilt_default_cci_clustered"], expect_pass=False
        )
        assert "as_columnstore" in output
