import pytest

from lean_runtime.discovery import DiscoveryPolicy, PolicyError


@pytest.mark.parametrize("value", [0, -1, True])
def test_candidate_limit_must_be_positive_integer(value: object) -> None:
    with pytest.raises(PolicyError):
        DiscoveryPolicy(max_candidates=value)  # type: ignore[arg-type]


def test_channels_must_be_unique() -> None:
    with pytest.raises(PolicyError, match="duplicates"):
        DiscoveryPolicy(channels=("stable", "stable"))


def test_flags_must_be_boolean() -> None:
    with pytest.raises(PolicyError, match="Boolean"):
        DiscoveryPolicy(allow_download=1)  # type: ignore[arg-type]


def test_remote_acquisition_limit_is_nonnegative() -> None:
    with pytest.raises(PolicyError, match="max_remote_acquisitions"):
        DiscoveryPolicy(max_remote_acquisitions=-1)
