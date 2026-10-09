"""A raised warning is reported as a masked error, not as the failure itself.

When a warning such as 8153 (SQLSTATE 01003, "Null value is eliminated by an
aggregate") and an error arrive in the same result, pyodbc and mssql-python
raise the warning and the error is lost. The adapter must say so rather than
present the warning as what failed.
"""

import re
from types import SimpleNamespace

import pytest
from dbt_common.exceptions import DbtDatabaseError

from dbt.adapters.sqlserver.sqlserver_backend import (
    _MSSQL_PYTHON_WARNING_SQLSTATES,
    handle_backend_database_error,
    warning_sqlstate,
)
from dbt.adapters.sqlserver.sqlserver_connections import SQLServerConnectionManager
from dbt.adapters.sqlserver.sqlserver_credentials import SQLServerBackend, SQLServerCredentials
from dbt.adapters.sqlserver.sqlserver_runtime import (
    configure_runtime_state_for_test,
    reset_runtime_state_for_test,
)

WARNING_8153 = (
    "[01003] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Warning: Null value "
    "is eliminated by an aggregate or other SET operation. (8153) (SQLExecDirectW)"
)
MSSQL_PYTHON_DDBC_8153 = (
    "[Microsoft][SQL Server]Warning: Null value is eliminated by an aggregate or other "
    "SET operation."
)


def _raise_through_handler(error: Exception, database_error: type[Exception]) -> DbtDatabaseError:
    release_calls: list[int] = []
    with pytest.raises(DbtDatabaseError) as excinfo:
        handle_backend_database_error(error, database_error, lambda: release_calls.append(1))
    assert release_calls == [1]
    assert excinfo.value.__cause__ is error
    return excinfo.value


# pyodbc ---------------------------------------------------------------------


def test_pyodbc_warning_is_recognised() -> None:
    pyodbc = pytest.importorskip("pyodbc")
    # pyodbc has no class for SQLSTATE 01: the warning arrives as the base Error.
    assert warning_sqlstate(pyodbc.Error("01003", WARNING_8153)) == "01003"


def test_pyodbc_real_error_is_not_a_warning() -> None:
    pyodbc = pytest.importorskip("pyodbc")
    assert warning_sqlstate(pyodbc.DataError("22012", "Divide by zero (8134)")) is None
    assert warning_sqlstate(pyodbc.Error("HY000", "general error")) is None


def test_non_driver_exception_shaped_like_a_warning_is_ignored() -> None:
    assert warning_sqlstate(RuntimeError("01003", WARNING_8153)) is None


def test_pyodbc_warning_is_reported_as_masked() -> None:
    pyodbc = pytest.importorskip("pyodbc")
    error = pyodbc.Error("01003", WARNING_8153)

    raised = _raise_through_handler(error, pyodbc.DatabaseError)

    message = str(raised)
    assert "reported only a warning (SQLSTATE 01003) and lost the real error" in message
    assert "(8153)" in message


def test_pyodbc_real_error_message_is_unchanged() -> None:
    pyodbc = pytest.importorskip("pyodbc")
    error = pyodbc.DataError("22012", "Divide by zero error encountered. (8134)")

    raised = _raise_through_handler(error, pyodbc.DatabaseError)

    assert "lost the real error" not in str(raised)
    assert "(8134)" in str(raised)


def test_handler_ignores_other_non_database_errors() -> None:
    pyodbc = pytest.importorskip("pyodbc")
    handle_backend_database_error(
        pyodbc.Error("HY000", "not a database error"), pyodbc.DatabaseError, lambda: None
    )


def test_exception_handler_routes_pyodbc_warning_as_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The base pyodbc.Error is not a DatabaseError, so the connection manager
    must route it by its SQLSTATE, not by isinstance alone."""
    pyodbc = pytest.importorskip("pyodbc")
    manager = object.__new__(SQLServerConnectionManager)
    credentials = SQLServerCredentials(
        backend=SQLServerBackend.pyodbc,
        driver="ODBC Driver 18 for SQL Server",
        host="fake.sql.sqlserver.net",
        database="dbt",
        schema="sqlserver",
        encrypt=True,
        trust_cert=True,
    )
    release_calls: list[int] = []

    reset_runtime_state_for_test()
    configure_runtime_state_for_test(pyodbc_module=pyodbc)
    try:
        monkeypatch.setattr(
            manager, "get_thread_connection", lambda: SimpleNamespace(credentials=credentials)
        )
        monkeypatch.setattr(manager, "release", lambda: release_calls.append(1))

        with pytest.raises(DbtDatabaseError, match=re.escape("SQLSTATE 01003")):
            with manager.exception_handler("insert ..."):
                raise pyodbc.Error("01003", WARNING_8153)

        assert release_calls == [1]
    finally:
        reset_runtime_state_for_test()


# mssql-python ---------------------------------------------------------------


def test_mssql_python_warning_is_reported_as_masked() -> None:
    mssql_python = pytest.importorskip("mssql_python")
    from mssql_python.exceptions import sqlstate_to_exception

    error = sqlstate_to_exception("01003", MSSQL_PYTHON_DDBC_8153)
    assert isinstance(error, mssql_python.DatabaseError)
    assert warning_sqlstate(error) == "01003"

    raised = _raise_through_handler(error, mssql_python.DatabaseError)

    assert "reported only a warning (SQLSTATE 01003) and lost the real error" in str(raised)


def test_mssql_python_truncation_error_is_not_a_warning() -> None:
    """22001 shares its class with the 01xxx DataErrors but is a real error."""
    pytest.importorskip("mssql_python")
    from mssql_python.exceptions import sqlstate_to_exception

    error = sqlstate_to_exception("22001", "String or binary data would be truncated.")
    assert warning_sqlstate(error) is None


def test_mssql_python_warning_states_are_unambiguous() -> None:
    """Each listed state must be the only SQLSTATE that mssql-python maps to its
    (class, driver_error) pair, or a real error would be reported as masked."""
    pytest.importorskip("mssql_python")
    import mssql_python.exceptions as mssql_exceptions

    with open(mssql_exceptions.__file__, encoding="utf-8") as source:
        all_states = set(re.findall(r'"([0-9A-Z]{5})"\s*:', source.read()))
    assert set(_MSSQL_PYTHON_WARNING_SQLSTATES) <= all_states

    def signature(state: str) -> tuple[type, str]:
        error = mssql_exceptions.sqlstate_to_exception(state, "")
        return type(error), error.driver_error

    for listed in _MSSQL_PYTHON_WARNING_SQLSTATES:
        assert listed.startswith("01")
        clashes = {
            state
            for state in all_states - {listed}
            if mssql_exceptions.sqlstate_to_exception(state, "") is not None
            and signature(state) == signature(listed)
        }
        assert not clashes, f"{listed} is indistinguishable from {sorted(clashes)}"
