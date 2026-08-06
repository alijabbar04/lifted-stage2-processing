#!/usr/bin/env python3
r"""
Stage 2 credential helper
=========================
Writes / removes the Anthropic API key in the Windows Credential Manager under
the SAME keyring service + username that Stage2_Processing.pyw reads from:

    KEYRING_SERVICE = "DocReviewAIStation"
    KEYRING_USER    = "anthropic_api_key"

(these MUST stay identical to Stage2_Processing.pyw - see get_api_key() there).

This is built into a tiny stand-alone .exe with PyInstaller so the installer can
set the key on a colleague's machine that has NO Python. It uses the identical
`keyring` library as Stage 2, so the credential it stores is byte-for-byte what
Stage 2's keyring.get_password() expects - no reverse-engineering of Windows
Credential Manager target names.

Usage:
    stage2_credhelper.exe set <keyfile>   read the key text from <keyfile>,
                                          store it, then delete <keyfile>.
    stage2_credhelper.exe del             remove the stored key (uninstall).

Exit code 0 = success. Never prints the key itself.
"""
import os
import sys

# MUST match Stage2_Processing.pyw's KEYRING_SERVICE / KEYRING_USER.
KEYRING_SERVICE = "DocReviewAIStation"
KEYRING_USER = "anthropic_api_key"


def _do_set(keyfile: str) -> int:
    import keyring
    key = ""
    try:
        with open(keyfile, "r", encoding="utf-8") as f:
            key = f.read().strip()
    except Exception as e:
        print(f"could not read key file: {e}", file=sys.stderr)
        return 4
    finally:
        # scrub the temporary key file regardless of what happens next
        try:
            os.remove(keyfile)
        except Exception:
            pass
    if not key:
        print("key file was empty", file=sys.stderr)
        return 5
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
    except Exception as e:
        print(f"could not store key: {e}", file=sys.stderr)
        return 6
    print("Anthropic API key stored in Windows Credential Manager.")
    return 0


def _do_del() -> int:
    try:
        import keyring
        from keyring.errors import PasswordDeleteError
    except Exception as e:
        print(f"keyring unavailable (ignored): {e}", file=sys.stderr)
        return 0
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        print("Anthropic API key removed from Windows Credential Manager.")
    except PasswordDeleteError:
        print("No stored API key to remove.")
    except Exception as e:
        # never fail an uninstall over this
        print(f"could not remove key (ignored): {e}", file=sys.stderr)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: stage2_credhelper set <keyfile> | del", file=sys.stderr)
        return 2
    cmd = sys.argv[1].strip().lower()
    if cmd == "set":
        if len(sys.argv) < 3:
            print("set requires a keyfile path", file=sys.stderr)
            return 2
        return _do_set(sys.argv[2])
    if cmd == "del":
        return _do_del()
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
