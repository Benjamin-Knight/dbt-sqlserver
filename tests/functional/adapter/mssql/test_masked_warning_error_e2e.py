import pytest
from dbt_common.exceptions import DbtDatabaseError

from dbt.tests.util import get_connection

# Inside a nested batch under NOCOUNT, both drivers raise the 8153 warning that
# precedes the divide-by-zero and drop the error itself. In a plain batch they
# report the error, which is the control case below.
WARNING_THEN_ERROR = (
    "DECLARE @m int; "
    "SELECT @m = MAX(x) FROM (VALUES (CAST(NULL AS int)), (1)) v(x); "
    "SELECT 1/0 AS z INTO #masked_warning_error;"
)


class BaseMaskedWarningError:
    def test_nested_batch_reports_masked_error(self, project):
        sql = f"SET NOCOUNT ON; EXEC('{WARNING_THEN_ERROR}');"

        with get_connection(project.adapter):
            with pytest.raises(DbtDatabaseError) as excinfo:
                project.adapter.execute(sql)

        message = str(excinfo.value)
        assert "reported only a warning (SQLSTATE 01003) and lost the real error" in message
        assert "Null value is eliminated by an aggregate" in message

    def test_plain_batch_reports_real_error(self, project):
        sql = f"SET NOCOUNT ON; {WARNING_THEN_ERROR}"

        # The error arrives on a later result set, which execute() drains
        # outside exception_handler, so it surfaces as the raw driver error.
        with get_connection(project.adapter):
            with pytest.raises(Exception) as excinfo:
                project.adapter.execute(sql)

        message = str(excinfo.value)
        assert "Divide by zero" in message
        assert "lost the real error" not in message


class TestMaskedWarningErrorPyodbc(BaseMaskedWarningError):
    @pytest.fixture(scope="class")
    def dbt_profile_target_update(self):
        return {"backend": "pyodbc"}


class TestMaskedWarningErrorMssqlPython(BaseMaskedWarningError):
    @pytest.fixture(scope="class")
    def dbt_profile_target_update(self):
        return {"backend": "mssql-python"}
