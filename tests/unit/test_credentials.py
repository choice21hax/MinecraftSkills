import json

import keyring
import pytest
from keyring.backend import KeyringBackend

from mcat.cli.main import main
from mcat.reporting import Result
from mcat.security import credentials as cred
from mcat.security.redact import REDACTED, redact, redact_obj, register_secret


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError("missing")
        del self.store[(service, username)]


class PlaintextKeyring(MemoryKeyring):
    pass


@pytest.fixture
def secure_kr(monkeypatch):
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    monkeypatch.setattr(cred, "_SECURE_BACKENDS", {f"{MemoryKeyring.__module__}.MemoryKeyring"})
    return kr


def test_rejects_insecure_backend(monkeypatch):
    keyring.set_keyring(PlaintextKeyring())
    monkeypatch.setattr(cred, "_SECURE_BACKENDS", set())
    assert not cred.backend_status().secure
    with pytest.raises(cred.CredentialError, match="plaintext"):
        cred.set_secret("curseforge", "abcdefgh")


def test_store_and_status_never_reveal(secure_kr, capsys):
    cred.set_secret("curseforge", "TOPSECRETKEY123")
    st = cred.credential_status()
    assert st["curseforge"] == {"configured": True, "source": "keyring"}
    assert "TOPSECRETKEY123" not in json.dumps(st)
    assert main(["auth", "status", "--json"]) == 0
    out = capsys.readouterr().out
    assert "TOPSECRETKEY123" not in out


def test_remove(secure_kr):
    cred.set_secret("modrinth", "mrp_" + "a" * 30)
    assert cred.remove_secret("modrinth") is True
    assert cred.remove_secret("modrinth") is False


def test_empty_secret_rejected(secure_kr):
    with pytest.raises(cred.CredentialError):
        cred.set_secret("curseforge", "   ")


def test_env_credentials_require_opt_in(secure_kr, monkeypatch):
    monkeypatch.setenv("MCAT_CURSEFORGE_API_KEY", "ENVKEY12345")
    monkeypatch.delenv("MCAT_ALLOW_ENV_CREDENTIALS", raising=False)
    assert cred.get_secret("curseforge") == (None, "none")
    assert "ignored" in cred.credential_status()["curseforge"]["note"]
    monkeypatch.setenv("MCAT_ALLOW_ENV_CREDENTIALS", "1")
    assert cred.get_secret("curseforge") == ("ENVKEY12345", "env")
    assert redact("key=ENVKEY12345") == f"key={REDACTED}"


def test_auth_set_requires_tty(secure_kr, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["auth", "set", "curseforge", "--json"]) == 4


def test_no_show_command(capsys):
    with pytest.raises(SystemExit) as e:
        main(["auth", "show", "curseforge"])
    assert e.value.code == 2


def test_redaction_patterns():
    register_secret("supersecretvalue")
    assert "supersecretvalue" not in redact("error: supersecretvalue rejected")
    assert REDACTED in redact("x-api-key: $2a$10$" + "a" * 50)
    assert redact("Authorization: Bearer-ish-token") == f"Authorization: {REDACTED}"
    assert redact_obj({"api_key": "plain", "nested": ["mrp_" + "b" * 30]}) == {
        "api_key": REDACTED,
        "nested": [REDACTED],
    }


def test_result_envelope_redacts():
    register_secret("leakyleaky")
    r = Result("x", summary="leakyleaky")
    r.fail("auth failed with leakyleaky")
    assert "leakyleaky" not in r.to_json() and "leakyleaky" not in r.to_text()
