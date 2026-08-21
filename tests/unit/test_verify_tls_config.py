"""TLS-verification setting resolution.

`IS_VERIFY_TLS` / `SERVICE_VERIFY_TLS` are three-state: a boolean, or a path to
a CA bundle. The path form exists because Python trusts certifi rather than the
macOS keychain, so "verify against the demo CA in pki/" cannot be expressed as
a boolean. The dangerous failure mode is a bad path silently degrading to no
verification, so that case is pinned here.
"""

import pytest

from common.config import CommonDefaults, resolve_verify_tls


@pytest.mark.parametrize("raw", ["true", "True", "TRUE", "1", "yes", "on"])
def test_truthy_values_enable_verification(raw):
    assert resolve_verify_tls(raw, label="test") is True


@pytest.mark.parametrize("raw", ["false", "False", "FALSE", "0", "no", "off"])
def test_falsy_values_disable_verification(raw):
    assert resolve_verify_tls(raw, label="test") is False


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_falls_back_to_the_default(raw):
    assert resolve_verify_tls(raw, label="test") is True
    assert resolve_verify_tls(raw, label="test", default=False) is False


def test_existing_path_is_returned_for_requests_and_httpx(tmp_path):
    bundle = tmp_path / "ca-bundle.crt"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")

    resolved = resolve_verify_tls(str(bundle), label="test")

    # Both requests and httpx accept a str path as `verify=`, so the value is
    # passed straight through rather than being converted.
    assert resolved == str(bundle)


def test_missing_ca_bundle_fails_closed_rather_than_disabling_verification(tmp_path, caplog):
    missing = tmp_path / "nope.crt"

    with caplog.at_level("ERROR"):
        resolved = resolve_verify_tls(str(missing), label="test")

    # Must NOT be False: a typo in the path would otherwise silently turn into
    # an unverified connection. True makes it fail loudly at TLS handshake.
    assert resolved is True
    assert "does not exist" in caplog.text


def test_disabling_verification_is_logged_as_a_warning(caplog):
    with caplog.at_level("WARNING"):
        resolve_verify_tls("false", label="Web Portal -> WSO2 IS")
    assert "DISABLED" in caplog.text
    assert "Web Portal -> WSO2 IS" in caplog.text


def test_defaulting_to_off_is_warned_about_too(caplog):
    # IS_VERIFY_TLS defaults to off (CommonDefaults), because the quickstart
    # points at WSO2's self-signed certificate. That must still say so out loud:
    # an unset variable is exactly the case where nobody has thought about it.
    with caplog.at_level("WARNING"):
        assert resolve_verify_tls(None, label="AI Agent -> WSO2 IS", default=False) is False
    assert "DISABLED" in caplog.text
    assert "AI Agent -> WSO2 IS" in caplog.text


def test_defaulting_to_on_is_not_warned_about(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_verify_tls(None, label="test", default=True) is True
    assert "DISABLED" not in caplog.text


def test_both_quickstart_defaults_are_off_and_symmetric():
    # Deliberate, and the reason differs per variable:
    #   IS_VERIFY_TLS      — WSO2's self-signed certificate; True breaks OIDC
    #                        discovery on `bash start.sh` out of the box.
    #   SERVICE_VERIFY_TLS — inert over the quickstart's plain-HTTP hops, but
    #                        under TLS_CERT_DIR those hops carry demo-CA certs
    #                        that certifi does not trust, so True fails the
    #                        handshake on an otherwise correct stack.
    # Off is only acceptable because resolving to off is always warned about;
    # pinned here so the pair cannot drift apart again.
    assert CommonDefaults.IS_VERIFY_TLS is False
    assert CommonDefaults.SERVICE_VERIFY_TLS is False


def test_a_directory_is_not_accepted_as_a_bundle(tmp_path):
    # requests accepts a CA *directory*, but only one prepared with c_rehash;
    # pki/ produces a file, and silently accepting a directory here would defer
    # the error to handshake time with a far less obvious message.
    assert resolve_verify_tls(str(tmp_path), label="test") is True
