import pytest
from dbt.tests.util import run_dbt

model_sql = """
{{ config(materialized="table") }}
select 1 as id
"""

class TestReservedKeywordsSchema:
    @pytest.fixture(scope="class")
    def models(self):
        return {"model.sql": model_sql}

    @pytest.fixture(scope="class")
    def profile_config(self, unique_schema, dbt_profile_target):
        # Configure the model to use the reserved keyword "group" as its schema.
        # This setup reproduces the issue where reserved keywords in schema names
        # were not correctly quoted, ensuring the adapter handles such identifiers properly.
        pass

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "name": "test_reserved_keywords_schema",
            "quoting": {
                "database": True,
                "schema": True,
                "identifier": True
            },
            "models": {
                "test_reserved_keywords_schema": {
                    "schema": "group"
                }
            }
        }

    @pytest.fixture(scope="class")
    def macros(self):
        return {
            "generate_schema_name.sql": """
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name -%}
        {{ custom_schema_name | trim }}
    {%- else -%}
        {{ target.schema }}
    {%- endif -%}
{%- endmacro %}
            """
        }

    def test_reserved_schema(self, project):
        # This triggers dbt run which should fail if the bug is present
        # We need to make sure we clean up if possible, but 'group' schema might be tricky.
        results = run_dbt(["run"])
        assert len(results) == 1
