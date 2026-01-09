import pytest
from dbt.tests.util import run_dbt

class TestLegacyTypes:
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "flags": {
                "MSSQL_LEGACY_STRING_TYPES": True
            }
        }

    def test_legacy_types_flag_sets_correct_types(self, project):
        adapter = project.adapter
        string_type = adapter.Column.TYPE_LABELS.get("STRING")
        assert string_type == "VARCHAR(8000)", f"Expected VARCHAR(8000) but got {string_type}"

class TestDefaultTypes:
    def test_default_types(self, project):
        adapter = project.adapter
        string_type = adapter.Column.TYPE_LABELS.get("STRING")
        assert string_type == "VARCHAR(MAX)", f"Expected VARCHAR(MAX) but got {string_type}"
