"""Turn a typed-once password into a key the app can reconnect with.

A password is never written to disk, which used to mean a password-only host worked
exactly once: the next launch had nothing to log in with and no way to ask. So the
password is spent, once, on installing a key -- the same thing ssh-copy-id does -- and
everything after that is key auth.

The key is one file for the whole app rather than one per host: it stands for "this app
on this machine", it is one thing to revoke, and a host that is removed leaves nothing
behind but a line in its own authorized_keys that revoke_key() takes out.

The private key sits unencrypted under the app's data directory. That is deliberately
better than keeping the password: it is revocable per host by deleting one line, it is
scoped to hosts the user deliberately added, and it never crosses the wire. Encrypting
it would mean holding the passphrase to start servers at launch, which is the same
problem wearing a hat.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from niksnaks_hosting.shared.remote.ssh_client import SshConnection, SshCredentials, SshError
from niksnaks_hosting.shared.utils.constants import DATA_DIR
from niksnaks_hosting.shared.utils.subprocess_utils import hidden_subprocess_kwargs

if TYPE_CHECKING:
    from niksnaks_hosting.shared.remote.host import SshHost

KEY_DIR = DATA_DIR / "ssh"
KEY_PATH = KEY_DIR / "id_ed25519"
PUBLIC_KEY_PATH = KEY_DIR / "id_ed25519.pub"

# Written at the end of the authorized_keys line, so someone reading that file can tell
# what put it there and what deleting it would break.
KEY_COMMENT = "niksnaks-hosting"

ProgressFn = Callable[[str], None]

# Generating and writing the key must not race when two hosts are set up at once.
_key_lock = threading.Lock()


@dataclass
class KeyAuthResult:
    """Whether the app can now log in without a password, and why not if it cannot."""

    installed: bool
    already: bool = False
    detail: str = ""


# --------------------------------------------------------------------- the app's key
def ensure_app_key() -> tuple[str, str]:
    """The app's keypair, generated on first use. Returns (private path, public line)."""
    with _key_lock:
        KEY_DIR.mkdir(parents=True, exist_ok=True)
        _harden_directory(KEY_DIR)

        if KEY_PATH.is_file():
            public_line = _public_line_for(KEY_PATH)
            # A public file lost to a half-finished write is rebuilt from the private one.
            if not PUBLIC_KEY_PATH.is_file():
                PUBLIC_KEY_PATH.write_text(public_line + "\n", encoding="ascii")
            return str(KEY_PATH), public_line

        return _generate_key()


def existing_app_key() -> str:
    """The path to the app's key if it already has one, without making one.

    ensure_app_key() generates on demand, which is right when a key is about to be
    installed and wrong everywhere else -- writing a keypair just to look at a host
    would be a surprising thing for opening a dialog to do.
    """
    return str(KEY_PATH) if KEY_PATH.is_file() else ""


def _generate_key() -> tuple[str, str]:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
        )
    except ImportError as e:  # paramiko pulls this in, so it is here whenever SSH is
        raise SshError(
            _("This build cannot create an SSH key, so a password cannot be saved as one.")
        ) from e

    private = Ed25519PrivateKey.generate()
    # PrivateFormat.OpenSSH is what paramiko's Ed25519Key reads back, which is the
    # loader ssh_client._load_private_key tries first.
    pem = private.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption())
    raw_public = private.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)
    public_line = f"{raw_public.decode('ascii')} {KEY_COMMENT}"

    _write_private(KEY_PATH, pem)
    PUBLIC_KEY_PATH.write_text(public_line + "\n", encoding="ascii")
    return str(KEY_PATH), public_line


def _public_line_for(private_path: Path) -> str:
    """Read the public half back out of the private key file."""
    try:
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
            load_ssh_private_key,
        )

        private = load_ssh_private_key(private_path.read_bytes(), password=None)
        raw = private.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)
        return f"{raw.decode('ascii')} {KEY_COMMENT}"
    except Exception as e:
        raise SshError(
            _("The app's SSH key at {} could not be read: {}").format(private_path, e)
        ) from e


def _write_private(path: Path, data: bytes) -> None:
    """Write the private key so it is never, even briefly, readable by anyone else."""
    path.unlink(missing_ok=True)
    if sys.platform == "win32":
        path.write_bytes(data)
        _harden_windows(path)
        return

    # O_EXCL with the mode set up front, rather than write-then-chmod, which would leave
    # a window where the key is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _harden_directory(path: Path) -> None:
    if sys.platform == "win32":
        _harden_windows(path)
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _harden_windows(path: Path) -> None:
    """Take the inherited ACLs off and leave only this user.

    chmod does nothing to a Windows ACL. This is defence in depth -- the file already
    lives under the user's own AppData -- so a failure is not worth stopping setup for.
    """
    user = os.environ.get("USERNAME") or ""
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            text=True,
            timeout=30,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        pass


# ------------------------------------------------------------------ installing it
def ensure_key_auth(
    host: SshHost, password: str, progress: ProgressFn | None = None
) -> KeyAuthResult:
    """Put the app's key on *host* so later launches need no password.

    The password is only ever used to open the connection that installs the key, and is
    cleared from the credentials once a key-only login has been proven to work. Nothing
    is cleared before that proof, so a failure leaves the host exactly as usable as it
    was.
    """

    def step(text: str) -> None:
        if progress:
            progress(text)

    key_path, public_line = ensure_app_key()
    credentials = host.credentials

    # The connection this runs over is the one the password opened, but it may have to
    # be reopened partway through, so the credentials must actually carry it.
    if password and not credentials.password:
        credentials.password = password

    # Already set up: prove it still works rather than writing the file again.
    if credentials.key_path == key_path and _key_login_works(host, key_path):
        return KeyAuthResult(installed=True, already=True)

    try:
        step(_("Setting up key login on {}").format(host.name))
        host.ops.append_authorized_key(host, public_line)
    except SshError as e:
        return KeyAuthResult(installed=False, detail=str(e))

    if not _key_login_works(host, key_path):
        # Windows keeps administrators' keys somewhere else entirely, and ignores the
        # profile file for those accounts. Which case this is cannot be predicted from
        # the sshd config without reading it, so the answer is: install, test, and only
        # then try the other place.
        moved = False
        try:
            moved = host.ops.append_admin_authorized_key(host, public_line)
        except SshError:
            moved = False

        if not moved or not _key_login_works(host, key_path):
            return KeyAuthResult(
                installed=False,
                detail=_(
                    "{} accepted the key file but would not log in with it. Its SSH server "
                    "may have key logins turned off."
                ).format(host.name),
            )

    # Proven. Only now does the password stop being needed.
    credentials.key_path = key_path
    credentials.password = ""
    credentials.key_passphrase = ""

    # Drop the password-authenticated connection so everything after this goes over a
    # key-authenticated one. SshConnection reopens on next use, so failing to reopen it
    # eagerly is not failing to install -- the key was proven to work a step ago, and
    # saying otherwise here would leave the caller restoring a password it no longer needs.
    try:
        host.close()
        host.connect()
    except SshError:
        pass

    return KeyAuthResult(installed=True)


def _key_login_works(host: SshHost, key_path: str) -> bool:
    """Open a throwaway connection that has nothing but the key to offer.

    SshConnection turns the agent and ~/.ssh off as soon as a key path is set, so a
    success here really is the key working and not something else being picked up.
    """
    credentials = host.credentials
    probe = SshCredentials(
        hostname=credentials.hostname,
        port=credentials.port,
        username=credentials.username,
        password="",
        key_path=key_path,
        known_host_key=host.connection.trusted_key or credentials.known_host_key,
    )
    connection = SshConnection(probe)
    try:
        connection.connect()
        return connection.run("printf ok", timeout=20).stdout.strip() == "ok"
    except Exception:
        return False
    finally:
        connection.close()


def authorized_key_script(public_line: str) -> str:
    """The shell that adds one key to authorized_keys, exactly once.

    Kept apart from the call so it can be run against a scratch $HOME and checked, which
    is the only practical way to prove the awkward cases: a file whose last line has no
    newline, and a second run that must not add a duplicate.

    Raw string: every backslash below belongs to the shell, not to Python.
    """
    quoted = shlex.quote(public_line)
    return rf"""
set -e
ak="$HOME/.ssh/authorized_keys"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$ak"
chmod 600 "$ak"
# A file whose last line has no newline would otherwise get the key glued onto it.
if [ -s "$ak" ] && [ "$(tail -c1 "$ak" | wc -l)" -eq 0 ]; then
    printf '\n' >> "$ak"
fi
if ! grep -qxF {quoted} "$ak"; then
    printf '%s\n' {quoted} >> "$ak"
fi
"""


def revoke_key_script(public_line: str) -> str:
    """The shell that takes this app's key back out of authorized_keys."""
    quoted = shlex.quote(public_line)
    return rf"""
ak="$HOME/.ssh/authorized_keys"
[ -f "$ak" ] || exit 0
# grep -v exits 1 when it prints nothing, which is exactly the case where this app's
# key was the only line in the file -- so its status is not the thing to test.
grep -vxF {quoted} "$ak" > "$ak.niksnaks-tmp" || true
mv "$ak.niksnaks-tmp" "$ak"
chmod 600 "$ak"
"""


def revoke_key(host: SshHost) -> bool:
    """Take the app's key back off *host*. True when the file no longer lists it."""
    # No key file means none was ever installed anywhere, and ensure_app_key() would
    # otherwise generate one purely in order to go and remove it.
    if not KEY_PATH.is_file():
        return True

    try:
        _key_path, public_line = ensure_app_key()
    except SshError:
        return False
    try:
        return host.ops.revoke_authorized_key(host, public_line)
    except SshError:
        return False
