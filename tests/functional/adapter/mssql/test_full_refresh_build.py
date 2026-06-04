import pytest

from dbt.tests.util import run_dbt_and_capture

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
