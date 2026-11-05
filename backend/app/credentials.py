"""Credential storage.

An API key never goes into the database and never goes into the browser.
It goes into the operating system's credential store, and the database only
holds a *reference* to it.

On Windows that is the Credential Manager, on macOS the Keychain, on Linux
the Secret Service or the kernel keyring. The `keyring` package is the
maintained library that speaks to all of them.

Two rules shape this module:

**There is no silent fallback.** If no credential store is available, saving
a key fails with a clear error. Storing it in the database instead would
mean the key sat in a file that gets copied into backups, and a user who
believed it was in the credential store would not know. A configuration
without a key is still perfectly usable -- plenty of local endpoints need
none -- so the feature degrades rather than breaks.

**An edit does not destroy a key.** The reference is stored separately from
the secret, so editing a configuration's name, model or base URL leaves the
key alone. Replacing or removing a key is an explicit operation.
"""

from __future__ import annotations

import logging
import uuid
from typing import Protocol

from app.errors import ErrorCode

logger = logging.getLogger("clipforge.credentials")

#: The service name the keys are filed under in the credential store.
CREDENTIAL_SERVICE = "clipforge"


class CredentialStoreError(Exception):
    """The credential store could not be used."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CredentialStore(Protocol):
    """The operations a credential store must support."""

    def available(self) -> bool: ...

    def store(self, reference: str, secret: str) -> None: ...

    def read(self, reference: str) -> str | None: ...

    def delete(self, reference: str) -> None: ...


class KeyringCredentialStore:
    """The operating system's credential store, through `keyring`."""

    def __init__(self, service: str = CREDENTIAL_SERVICE) -> None:
        self.service = service
        self._keyring = None
        self._checked = False
        self._available = False

    def _backend(self):
        """The keyring module, loaded lazily and only once."""
        if not self._checked:
            self._checked = True
            try:
                import keyring

                backend = keyring.get_keyring()
            except Exception as exc:  # noqa: BLE001 - any failure is the same
                logger.warning("the credential store is not available: %s", exc)
                self._available = False
                return None
            # A backend with a negative priority is a "null" or "fail"
            # backend: the keyring module falls back to one when it cannot
            # find a real store, and that must not look like success.
            if backend is None or getattr(backend, "priority", 0) < 0:
                logger.warning("no usable credential store backend was found")
                self._available = False
                return None
            self._available = True
            self._keyring = keyring
        return self._keyring

    def available(self) -> bool:
        """Whether a key can be stored and read back."""
        return self._backend() is not None

    def store(self, reference: str, secret: str) -> None:
        """Save a secret, replacing any previous value."""
        keyring = self._backend()
        if keyring is None:
            raise CredentialStoreError(
                ErrorCode.SECRET_STORE_UNAVAILABLE.value,
                "This machine has no usable credential store, so an API key "
                "cannot be saved. The configuration can still be saved "
                "without a key, which is enough for a local model server.",
            )
        try:
            keyring.set_password(self.service, reference, secret)
        except Exception as exc:  # noqa: BLE001 - the backend decides why
            logger.warning("the credential store refused a key: %s", exc)
            raise CredentialStoreError(
                ErrorCode.SECRET_STORE_UNAVAILABLE.value,
                "The credential store refused to save the key.",
            ) from exc

    def read(self, reference: str) -> str | None:
        """Read a secret, or ``None`` when there is none."""
        keyring = self._backend()
        if keyring is None:
            return None
        try:
            return keyring.get_password(self.service, reference)
        except Exception as exc:  # noqa: BLE001 - the backend decides why
            logger.warning("the credential store could not be read: %s", exc)
            return None

    def delete(self, reference: str) -> None:
        """Remove a secret. Removing one that is not there is not an error."""
        keyring = self._backend()
        if keyring is None:
            return
        try:
            keyring.delete_password(self.service, reference)
        except Exception:  # noqa: BLE001 - it may simply not be there
            return


class MemoryCredentialStore:
    """An in-memory store, for tests and for a machine with no store.

    It is never used by the application itself.
    """

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def available(self) -> bool:
        return True

    def store(self, reference: str, secret: str) -> None:
        self._secrets[reference] = secret

    def read(self, reference: str) -> str | None:
        return self._secrets.get(reference)

    def delete(self, reference: str) -> None:
        self._secrets.pop(reference, None)


#: The store the application uses.
DEFAULT_STORE = KeyringCredentialStore()


def new_reference() -> str:
    """A fresh reference for a secret."""
    return uuid.uuid4().hex


def resolve_secret(
    store: CredentialStore, reference: str | None
) -> str | None:
    """The secret behind a reference, when there is one."""
    if not reference:
        return None
    return store.read(reference)
