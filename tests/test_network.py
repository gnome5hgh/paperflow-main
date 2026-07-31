import pytest
from paperflow.core.security.network import (
    SSRFError, validate_url_target,
)


class TestValidateUrlTarget:
    def test_rejects_loopback(self):
        with pytest.raises(SSRFError):
            validate_url_target("http://127.0.0.1:8070/api")

    def test_rejects_private_10(self):
        with pytest.raises(SSRFError):
            validate_url_target("http://10.0.0.5/x")

    def test_rejects_private_192(self):
        with pytest.raises(SSRFError):
            validate_url_target("http://192.168.1.1/x")

    def test_rejects_link_local(self):
        with pytest.raises(SSRFError):
            validate_url_target("http://169.254.169.254/latest/meta-data")

    def test_rejects_cloud_metadata_via_hostname(self):
        with pytest.raises(SSRFError):
            validate_url_target("http://metadata.google.internal/x")

    def test_allows_allowlisted_netloc(self):
        # allowlist 里是 netloc（host:port 或 host）
        validate_url_target(
            "http://127.0.0.1:8070/api",
            allowlist={"127.0.0.1:8070"},
        )  # 不应抛

    def test_allows_public_url(self):
        validate_url_target("http://export.arxiv.org/api/query")  # 不应抛
