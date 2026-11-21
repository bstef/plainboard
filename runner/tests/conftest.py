import pytest

from plainboard_runner import errors


@pytest.fixture(autouse=True)
def _error_log(tmp_path_factory, monkeypatch):
    """The tests' errors never go into the real ~/.plainboard/runner-errors.log (PLAINBOARD_RUNNER_ERRORS)."""
    monkeypatch.setattr(errors, "ERROR_LOG", tmp_path_factory.getbasetemp() / "runner-errors.log")
