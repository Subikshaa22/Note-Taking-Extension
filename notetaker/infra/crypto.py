"""Symmetric encryption for the user's Notion token while it sits in broker messages.

The gateway never stores the token (spec §5); it travels inside the job's queue messages,
which RabbitMQ persists to disk. Encrypting it means a broker dump or the management UI
never shows a usable credential.
"""
from cryptography.fernet import Fernet, InvalidToken


class TokenCipherError(Exception):
    pass


class TokenCipher:
    def __init__(self, key: str):
        if not key:
            raise TokenCipherError(
                "TOKEN_ENCRYPTION_KEY is not set. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise TokenCipherError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key.") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise TokenCipherError("Notion token could not be decrypted (key mismatch?).") from exc
