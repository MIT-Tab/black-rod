import pytest
from django.urls import resolve, Resolver404


@pytest.mark.parametrize("path", ["/core/tournaments/mittab_bundle/upload"])
def test_separate_bundle_upload_is_removed(path):
    with pytest.raises(Resolver404):
        resolve(path)
