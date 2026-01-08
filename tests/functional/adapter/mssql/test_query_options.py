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
        # This model requires recursion > 100 (default). 
        # Without MAXRECURSION 200 it would fail.
        run_dbt(["run"])


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
         # Just testing that it compiles and runs with other options
         run_dbt(["run"])

import os 

class TestQueryOptionsRestriction:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "table_model.sql": "{{ config(materialized='table', query_options={'MAXDOP': 1}) }} select 1 as id",
            "view_model.sql": "{{ config(materialized='view', query_options={'MAXDOP': 1}) }} select 1 as id",
        }

    def test_table_respects_options(self, project):
        run_dbt(["run"])
        
        path = os.path.join(project.project_root, "target/run/test/models/table_model.sql")
        with open(path, "r") as f:
            sql = f.read()
        
        assert "MAXDOP 1" in sql
        assert "LABEL =" in sql

    def test_view_ignores_options(self, project):
        run_dbt(["run", "--select", "view_model"])
        
        # Check target/run (create view statement is executed)
        path = os.path.join(project.project_root, "target/run/test/models/view_model.sql")
        with open(path, "r") as f:
            sql = f.read()
            
        # Views do not use get_query_options in default dbt-sqlserver implementation.
        # So MAXDOP should not be present.
        assert "MAXDOP" not in sql

