import pytest

from dbt.adapters.sqlserver.sqlserver_adapter import SQLServerAdapter


def _adapter():
    """A bare adapter instance (no real connection), enough to exercise the
    index helper / validation methods."""
    return object.__new__(SQLServerAdapter)


@pytest.mark.parametrize(
    "build_options,expected",
    [
        (None, False),
        ({"maxdop": 4}, False),
        ({"online": True}, True),
        ({"resumable": True}, True),
        ({"online": False}, False),
        ({"maxdop": 4, "resumable": True}, True),
    ],
)
def test_index_needs_own_batch(build_options, expected):
    adapter = _adapter()
    raw = {"columns": ["a"]}
    if build_options is not None:
        raw["build_options"] = build_options
    assert adapter.index_needs_own_batch(raw) is expected


@pytest.mark.parametrize("build_options", [{"online": True}, {"resumable": True}])
def test_validate_indexes_does_not_reject_online_resumable(build_options):
    # ONLINE/RESUMABLE are built post-commit (sqlserver__create_indexes_post_commit),
    # so validation no longer rejects them and no behavior flag is consulted.
    adapter = _adapter()
    adapter.validate_indexes([{"columns": ["a"], "build_options": build_options}])
