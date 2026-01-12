import pytest
from dbt.tests.util import run_dbt

model_with_comment_sql = """
-- This is a comment before the WITH clause
WITH input as (SELECT 1 as id)
SELECT * FROM input
"""

model_yml = """
version: 2
models:
  - name: model_with_comment
    config:
      contract:
        enforced: true
    columns:
      - name: id
        data_type: int
"""

class TestLeadingComments:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "model_with_comment.sql": model_with_comment_sql,
            "schema.yml": model_yml,
        }

    def test_initial_comment(self, project):
        # This should fail if the bug is present because contract enforcement
        # triggers empty_subquery_sql which incorrectly handles the comment
        run_dbt(["run"])
