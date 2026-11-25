"""验证下载重试继续校验证书与主机名。"""

import ssl
import urllib.error
import urllib.request

import pytest

from core import updater


def _invalid_cert(code):
    error = ssl.SSLCertVerificationError(1, "certificate verify failed")
    error.verify_code = code
    return urllib.error.URLError(error)


def test_missing_issuer_retries_with_verified_public_ca(monkeypatch):
    request = urllib.request.Request("https://example.test/file")
    tried = []
    response = object()

    def fake_open(actual_request, *, timeout, context=None):
        assert actual_request is request and timeout == 5
        tried.append(context)
        if context is None:
            raise _invalid_cert(20)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
        assert context.cert_store_stats()["x509_ca"] > 0
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    assert updater._open_url(request, 5) is response
    assert len(tried) == 2


@pytest.mark.parametrize(("code", "attempts"), [(20, 2), (62, 1)])
def test_invalid_certificate_blocks_download(monkeypatch, tmp_path, code, attempts):
    tried = []

    def reject(*args, **kwargs):
        tried.append(kwargs)
        raise _invalid_cert(code)

    monkeypatch.setattr(urllib.request, "urlopen", reject)
    destination = tmp_path / "payload.exe"
    with pytest.raises(updater.DownloadError, match="HTTPS 证书校验失败"):
        updater.download_exe("https://example.test/file", destination)
    assert len(tried) == attempts and not destination.exists()
