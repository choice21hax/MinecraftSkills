"""Provider credentials backed by the OS keyring.

Rules:
* Secrets live only in a secure native keyring backend (Secret Service / KWallet / macOS Keychain /
  Windows Credential Locker). Plaintext or "fail" backends are rejected - never silently used.
* There is deliberately no API that returns a secret for display. ``get_secret`` is used internally by
  provider clients only, and every loaded secret is registered with the redactor.
* CI environments may supply credentials through environment variables, but only when explicitly
  enabled with ``MCAT_ALLOW_ENV_CREDENTIALS=1``. Those values are never written anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import keyring
import keyring.errors
from keyring.backend import KeyringBackend

from mcat.security.redact import register_secret

SERVICE = "mcat"
Provider = Literal["curseforge", "modrinth"]
PROVIDERS: tuple[Provider, ...] = ("curseforge", "modrinth")
ENV_VARS: dict[Provider, str] = {
    "curseforge": "MCAT_CURSEFORGE_API_KEY",
    "modrinth": "MCAT_MODRINTH_TOKEN",
}
ENV_OPT_IN = "MCAT_ALLOW_ENV_CREDENTIALS"

# Backend module/class names known to be secure native stores.
_SECURE_BACKENDS = {
    "keyring.backends.SecretService.Keyring",
    "keyring.backends.libsecret.Keyring",
    "keyring.backends.kwallet.DBusKeyring",
    "keyring.backends.macOS.Keyring",
    "keyring.backends.Windows.WinVaultKeyring",
}


class CredentialError(Exception):
    pass


@dataclass
class BackendStatus:
    name: str
    secure: bool
    detail: str


def _qualname(backend: KeyringBackend) -> str:
    return f"{type(backend).__module__}.{type(backend).__name__}"


def backend_status() -> BackendStatus:
    try:
        kr = keyring.get_keyring()
    except Exception as e:  # keyring raises assorted errors when DBus is unreachable
        return BackendStatus("unavailable", False, f"keyring initialisation failed: {e}")
    name = _qualname(kr)
    if name == "keyring.backends.chainer.ChainerBackend":
        inner = [_qualname(b) for b in getattr(kr, "backends", [])]
        secure = [b for b in inner if b in _SECURE_BACKENDS]
        if secure:
            return BackendStatus(f"chainer({', '.join(inner)})", True, f"using secure backend {secure[0]}")
        return BackendStatus(f"chainer({', '.join(inner) or 'empty'})", False, "no secure backend in chain")
    if name in _SECURE_BACKENDS:
        return BackendStatus(name, True, "secure native backend")
    return BackendStatus(name, False, "not a recognised secure native backend (plaintext/fail/null?)")


def require_secure_backend() -> None:
    st = backend_status()
    if not st.secure:
        raise CredentialError(
            f"No secure keyring backend available ({st.name}: {st.detail}). "
            "MCAT will not store secrets in plaintext. Run `mcat doctor` for setup guidance."
        )


def env_credentials_enabled() -> bool:
    return os.environ.get(ENV_OPT_IN) == "1"


def set_secret(provider: Provider, value: str) -> None:
    if provider not in PROVIDERS:
        raise CredentialError(f"unknown provider {provider!r}")
    value = value.strip()
    if not value:
        raise CredentialError("empty credential rejected")
    require_secure_backend()
    try:
        keyring.set_password(SERVICE, provider, value)
        # Round-trip check without ever printing the value.
        if keyring.get_password(SERVICE, provider) != value:
            raise CredentialError("keyring round-trip verification failed")
    except keyring.errors.KeyringError as e:
        raise CredentialError(f"keyring refused to store the credential: {type(e).__name__}") from e


def remove_secret(provider: Provider) -> bool:
    require_secure_backend()
    try:
        keyring.delete_password(SERVICE, provider)
        return True
    except keyring.errors.PasswordDeleteError:
        return False


def get_secret(provider: Provider) -> tuple[str | None, str]:
    """Return (secret, source). Source is 'env', 'keyring' or 'none'. For provider clients only."""
    env_name = ENV_VARS[provider]
    if env_credentials_enabled() and os.environ.get(env_name):
        value = os.environ[env_name].strip()
        register_secret(value)
        return value, "env"
    st = backend_status()
    if not st.secure:
        return None, "none"
    try:
        stored = keyring.get_password(SERVICE, provider)
    except keyring.errors.KeyringError:
        return None, "none"
    if stored:
        register_secret(stored)
        return stored, "keyring"
    return None, "none"


def credential_status() -> dict[str, dict[str, object]]:
    """Presence/source per provider. Never includes the secret or any part of it."""
    out: dict[str, dict[str, object]] = {}
    for p in PROVIDERS:
        value, source = get_secret(p)
        out[p] = {"configured": value is not None, "source": source}
        if os.environ.get(ENV_VARS[p]) and not env_credentials_enabled():
            out[p]["note"] = f"{ENV_VARS[p]} is set but ignored (set {ENV_OPT_IN}=1 in CI to allow it)"
    return out


LINUX_SETUP_GUIDE = """\
Secure credential storage on Arch Linux / Hyprland (Secret Service via GNOME Keyring):

  1. Install:            sudo pacman -S gnome-keyring libsecret seahorse
  2. Start the daemon with your session. In ~/.config/hypr/hyprland.conf add:
       exec-once = dbus-update-activation-environment --systemd --all
       exec-once = gnome-keyring-daemon --start --components=secrets
     (or enable the user socket: systemctl --user enable --now gnome-keyring-daemon.socket)
  3. Make sure a DBus session bus exists: `echo $DBUS_SESSION_BUS_ADDRESS` must be non-empty.
  4. Create/unlock the default 'login' collection once (seahorse, or the prompt shown on first use).
     For auto-unlock at login, add `auth optional pam_gnome_keyring.so` and
     `session optional pam_gnome_keyring.so auto_start` to the display/login manager's PAM file.
  5. Verify:             mcat doctor   then   mcat auth status

KWallet (kwalletd6) also works. MCAT never modifies these system files for you.
"""
