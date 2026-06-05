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
        # prebuilt applies ONLY under --full-refresh; it creates the table
        # empty with its CCI in place, then bulk-loads in place. (Tiny test
        # row counts land in delta rowgroups rather than compressed segments
        # - the <102,400 rows/thread threshold - so we assert physical
        # design, not rowgroup state.)
        _, output = run_dbt_and_capture(["run", "--models", "cci_prebuilt", "--full-refresh"])
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

        # Second full refresh: still exactly one CCI, stable name, no
        # leftover heap or backup copies.
        _, output = run_dbt_and_capture(["run", "--models", "cci_prebuilt", "--full-refresh"])
        assert "full_refresh_build=prebuilt" in output
        indexes = get_cci_indexes(project, unique_schema, "cci_prebuilt")
        assert len(indexes) == 1
        assert indexes[0][0] == first_name

        # NORMAL run: default swap build, tables stay in place/visible -
        # prebuilt must not fire without --full-refresh.
        _, output = run_dbt_and_capture(["run", "--models", "cci_prebuilt"])
        assert "full_refresh_build=prebuilt" not in output
        assert len(get_cci_indexes(project, unique_schema, "cci_prebuilt")) == 1
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
        _, output = run_dbt_and_capture(["run", "--models", "rowstore_prebuilt", "--full-refresh"])
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

        # Full-refresh rebuild: stable names, still exactly one clustered.
        run_dbt(["run", "--models", "rowstore_prebuilt", "--full-refresh"])
        second = get_rowstore_indexes(project, unique_schema, "rowstore_prebuilt")
        assert second == by_type

        # Normal run: default swap path, no prebuilt.
        _, output = run_dbt_and_capture(["run", "--models", "rowstore_prebuilt"])
        assert "full_refresh_build=prebuilt" not in output

    def test_fallback_without_clustered(self, project, unique_schema):
        # No clustered index in config: nothing to prebuild, so the default
        # SELECT INTO heap path runs. That outcome is fine - so the fallback
        # is a debug-level trace, NOT a console warning (a table model would
        # otherwise emit it on every single run).
        _, output = run_dbt_and_capture(
            ["run", "--models", "fallback_no_clustered", "--full-refresh"]
        )
        assert "falling back" not in output

        by_type = get_rowstore_indexes(project, unique_schema, "fallback_no_clustered")
        assert set(by_type) == {"NONCLUSTERED"}

        # rerun: still quiet, still heap + NCI
        _, output = run_dbt_and_capture(
            ["run", "--models", "fallback_no_clustered", "--full-refresh"]
        )
        assert "falling back" not in output

    def test_prebuilt_clustered_with_default_columnstore_errors(self, project):
        # as_columnstore defaults true: the existing cross-config validation
        # must reject the clustered rowstore entry with guidance.
        _, output = run_dbt_and_capture(
            ["run", "--models", "prebuilt_default_cci_clustered"], expect_pass=False
        )
        assert "as_columnstore" in output


models__incr_prebuilt_sql = """
{{
  config(
    materialized = "incremental",
    as_columnstore = False,
    full_refresh_build = "prebuilt",
    indexes=[
      {'columns': ['column_a'], 'type': 'clustered'},
    ]
  )
}}

select *
from (
  select 1 as column_a, 2 as column_b
) t

{% if is_incremental() %}
    where column_a > (select max(column_a) from {{this}})
{% endif %}

"""

models__contract_prebuilt_sql = """
{{
  config(
    materialized = "table",
    as_columnstore = False,
    full_refresh_build = "prebuilt",
    contract = {"enforced": True},
    indexes=[
      {'columns': ['column_a'], 'type': 'clustered'},
    ]
  )
}}

select 1 as column_a, cast('x' as varchar(10)) as column_b

"""

models__contract_prebuilt_yml = """
version: 2
models:
  - name: contract_prebuilt
    config:
      contract:
        enforced: true
    columns:
      - name: column_a
        data_type: int
      - name: column_b
        data_type: varchar(10)
"""

models__dml_prebuilt_sql = """
{{
  config(
    materialized = "table",
    as_columnstore = False,
    table_refresh_method = "dml",
    full_refresh_build = "prebuilt",
    indexes=[
      {'columns': ['column_a'], 'type': 'clustered'},
    ]
  )
}}

select 1 as column_a, 2 as column_b

"""


class TestFullRefreshBuildIncrementalAndContract:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "incr_prebuilt.sql": models__incr_prebuilt_sql,
            "contract_prebuilt.sql": models__contract_prebuilt_sql,
            "contract_prebuilt.yml": models__contract_prebuilt_yml,
            "dml_prebuilt.sql": models__dml_prebuilt_sql,
        }

    def test_incremental_lifecycle(self, project, unique_schema):
        # First build: no pre-existing table to keep visible, so prebuilt
        # applies - the initial load lands compressed into its clustered
        # design instead of building a heap first.
        _, output = run_dbt_and_capture(["run", "--models", "incr_prebuilt"])
        assert "full_refresh_build=prebuilt" in output
        first = get_rowstore_indexes(project, unique_schema, "incr_prebuilt")
        assert set(first) == {"CLUSTERED"}
        assert first["CLUSTERED"][0].startswith("dbt_idx_")

        # Plain incremental run: the temp relation build must NOT take the
        # prebuilt path (temporary=True bypass); reconcile leaves the index.
        _, output = run_dbt_and_capture(["run", "--models", "incr_prebuilt"])
        assert "full_refresh_build=prebuilt" not in output
        assert get_rowstore_indexes(project, unique_schema, "incr_prebuilt") == first

        # Full refresh: rebuild via intermediate->swap, stable name.
        _, output = run_dbt_and_capture(["run", "--models", "incr_prebuilt", "--full-refresh"])
        assert "full_refresh_build=prebuilt" in output
        assert get_rowstore_indexes(project, unique_schema, "incr_prebuilt") == first

    def test_contract_enforced_prebuilt(self, project, unique_schema):
        _, output = run_dbt_and_capture(["run", "--models", "contract_prebuilt", "--full-refresh"])
        assert "full_refresh_build=prebuilt" in output
        by_type = get_rowstore_indexes(project, unique_schema, "contract_prebuilt")
        assert set(by_type) == {"CLUSTERED"}
        assert by_type["CLUSTERED"][0].startswith("dbt_idx_")

        # stable on full-refresh rerun; normal run keeps the default path
        run_dbt(["run", "--models", "contract_prebuilt", "--full-refresh"])
        assert get_rowstore_indexes(project, unique_schema, "contract_prebuilt") == by_type
        _, output = run_dbt_and_capture(["run", "--models", "contract_prebuilt"])
        assert "full_refresh_build=prebuilt" not in output

    def test_dml_refresh_ignores_prebuilt(self, project, unique_schema):
        # table_refresh_method=dml builds its scratch via its own SELECT INTO,
        # not create_table_as: prebuilt is out of scope there by design. First
        # build goes through create_table_as (rename path) and DOES prebuild;
        # the second, pure-DML run must not.
        run_dbt(["run", "--models", "dml_prebuilt"])
        _, output = run_dbt_and_capture(["run", "--models", "dml_prebuilt"])
        assert "full_refresh_build=prebuilt" not in output
        by_type = get_rowstore_indexes(project, unique_schema, "dml_prebuilt")
        assert set(by_type) == {"CLUSTERED"}

        # dml takes precedence even under --full-refresh (it never swaps, so
        # the swap-avoidance flag has nothing to do)
        _, output = run_dbt_and_capture(["run", "--models", "dml_prebuilt", "--full-refresh"])
        assert "full_refresh_build=prebuilt" not in output
