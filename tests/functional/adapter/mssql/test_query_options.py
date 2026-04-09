import os

import pytest
from dbt.tests.util import run_dbt

recursive_model_sql = """
{{
  config({
    "materialized": 'table',
    "query_options": {
      "MAXRECURSION": 200
    }
  })
}}
WITH cte AS (
    SELECT 1 AS n
    UNION ALL
    SELECT n + 1 FROM cte WHERE n < 150
)
SELECT * FROM cte
"""


class TestQueryOptionsRecursive:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "recursive_model.sql": recursive_model_sql,
        }

    def test_max_recursion_option(self, project):
        results = run_dbt(["run"])
        assert len(results) == 1
        assert results[0].status == "success"


generic_options_model_sql = """
{{
  config({
    "materialized": 'table',
    "query_options": {
      "MAXDOP": 1,
    }
  })
}}
select 1 as id
"""


class TestQueryOptionsGeneric:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "generic_model.sql": generic_options_model_sql,
        }

    def test_generic_option(self, project):
        results = run_dbt(["run"])
        assert len(results) == 1
        assert results[0].status == "success"


class TestQueryOptionsRestriction:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "table_model.sql": "{{ config(materialized='table', query_options={'MAXDOP': 1}) }} select 1 as id",
            "view_model.sql": "{{ config(materialized='view', query_options={'MAXDOP': 1}) }} select 1 as id",
        }

    def test_table_respects_options(self, project):
        run_dbt(["run", "--select", "table_model"])

        path = os.path.join(project.project_root, "target", "run", project.test_schema, "models", "table_model.sql")
        if not os.path.exists(path):
            # Fall back to searching for compiled SQL
            target_dir = os.path.join(project.project_root, "target", "run")
            for root, dirs, files in os.walk(target_dir):
                if "table_model.sql" in files:
                    path = os.path.join(root, files[files.index("table_model.sql")])
                    break

        with open(path, "r") as f:
            sql = f.read()

        assert "MAXDOP 1" in sql
        assert "LABEL =" in sql

    def test_view_ignores_options(self, project):
        run_dbt(["run", "--select", "view_model"])

        target_dir = os.path.join(project.project_root, "target", "run")
        path = None
        for root, dirs, files in os.walk(target_dir):
            if "view_model.sql" in files:
                path = os.path.join(root, files[files.index("view_model.sql")])
                break

        assert path is not None, "Could not find compiled view_model.sql"
        with open(path, "r") as f:
            sql = f.read()

        assert "MAXDOP" not in sql


invalid_option_model_sql = """
{{
  config({
    "materialized": 'table',
    "query_options": {
      "INVALID_OPTION": 1
    }
  })
}}
select 1 as id
"""


class TestQueryOptionsValidation:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "invalid_model.sql": invalid_option_model_sql,
        }

    def test_invalid_option_raises_error(self, project):
        results = run_dbt(["run"], expect_pass=False)
        assert len(results) == 1
        assert results[0].status == "error"
