"""Keep diagnostic unit tests independent of local checkout metadata."""
import pytest


@pytest.fixture(autouse=True)
def synthetic_diagnostic_repository_metadata(request, monkeypatch):
    """Source ZIPs and new checkouts have no HEAD; tests need no real Git commit.

    These modules test capture/validation behavior with fake ports and records.
    Supplying their unrelated provenance field avoids invoking Git while
    preserving every production validation and test assertion.
    """
    diagnostic_modules = {
        "test_checkpoint_02_runner", "test_nonrecording_visual_diagnostic",
        "test_recording_reset_diagnostic", "test_reset_capture_board",
    }
    if request.module.__name__ in diagnostic_modules:
        monkeypatch.setattr(
            "hardware.validation.checkpoint_02_runner.repository_commit",
            lambda: "synthetic-test-checkout",
        )
