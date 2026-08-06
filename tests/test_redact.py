import pytest

from factory.redact import MASK, redact, redact_text


@pytest.mark.parametrize(
    "raw,leaked",
    [
        ("password=Hunter2!x", "Hunter2!x"),
        ("PASSWORD: Hunter2!x", "Hunter2!x"),
        ('{"api_key": "abc123xyz789"}', "abc123xyz789"),
        ("API-KEY = abc123xyz789", "abc123xyz789"),
        ("secret_key=s3cr3tvalue", "s3cr3tvalue"),
        ("auth_token: tok_abcdefgh", "tok_abcdefgh"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("sshpass -p Hunter2!x ssh root@8.8.8.8", "Hunter2!x"),
        ("mysql --password Hunter2!x", "Hunter2!x"),
        ("sk-ant-api03-AAAAAAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAAAAAA"),
        ("ghp_AAAAAAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAAAAAA"),
        ("AKIAIOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
        ("xoxb-1234567890-abcdef", "1234567890-abcdef"),
    ],
)
def test_secret_values_are_masked(raw, leaked):
    out = redact_text(raw)
    assert leaked not in out
    assert MASK in out


def test_key_name_survives_so_audit_still_shows_where_the_secret_was():
    out = redact_text("DEPLOY_PASSWORD=Hunter2!x")
    assert "DEPLOY_PASSWORD" in out
    assert "Hunter2!x" not in out


def test_pem_private_key_block_is_removed():
    raw = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxxxxxxxx\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact_text(raw)
    assert "MIIEowIBAAKCAQEAxxxxxxxx" not in out
    assert MASK in out


def test_ordinary_text_is_untouched():
    raw = "exit 1: AssertionError: expected 'Hello, world!' got 'hi'"
    assert redact_text(raw) == raw


def test_redact_walks_nested_structures():
    claim = {
        "check": "deploy",
        "command": "sshpass -p Hunter2!x ssh root@host",
        "expected": "exit_zero",
        "got": ["exit 1", {"stderr": "api_key=abc123xyz789"}],
    }
    out = redact(claim)
    assert "Hunter2!x" not in str(out)
    assert "abc123xyz789" not in str(out)
    assert out["check"] == "deploy"


def test_non_string_scalars_pass_through():
    assert redact(7) == 7
    assert redact(None) is None
    assert redact(1.5) == 1.5
