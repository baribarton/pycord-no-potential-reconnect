"""
Test doubles for offline DAVE unit tests.

Import from here instead of discord.dave_session so fake implementations
stay out of the production package.
"""

from __future__ import annotations

from typing import Optional


class FakeDaveSession:
    """Drop-in replacement for :class:`discord.dave_session.DaveSession` that requires no real crypto.

    * ``decrypt_opus`` returns the input unchanged (identity transform).
    * All MLS handshake methods are no-ops that update internal state flags
      so callers can exercise the recording pipeline without a live Discord
      connection.
    * Optionally set ``fail_decrypt = True`` to simulate decrypt failures.
    """

    def __init__(
        self,
        protocol_version: int = 1,
        user_id: int = 0,
        channel_id: int = 0,
        *,
        start_ready: bool = True,
        fail_decrypt: bool = False,
    ) -> None:
        self.protocol_version = protocol_version
        self.user_id = user_id
        self.channel_id = channel_id
        self._ready = start_ready
        self.fail_decrypt = fail_decrypt
        self._epoch: Optional[int] = 1 if start_ready else None
        self._passthrough: bool = False
        self._decrypt_ok: dict[int, int] = {}
        self._decrypt_fail: dict[int, int] = {}
        self._external_sender_set: bool = False
        self._key_package_calls: int = 0
        self._commit_calls: int = 0
        self._welcome_calls: int = 0

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def epoch(self) -> Optional[int]:
        return self._epoch

    @property
    def status_name(self) -> str:
        return "SessionStatus.active" if self._ready else "SessionStatus.pending"

    def set_external_sender(self, data: bytes) -> None:
        self._external_sender_set = True

    def get_key_package(self) -> bytes:
        self._key_package_calls += 1
        return b"\x00\x01\x00\x02" + bytes(388)  # 392 bytes, matches real length

    def process_proposals(
        self,
        operation_type: int,
        data: bytes,
    ) -> Optional[tuple[bytes, Optional[bytes]]]:
        return (b"fake_commit", b"fake_welcome")

    def process_commit(self, commit: bytes) -> None:
        self._commit_calls += 1
        self._ready = True
        self._epoch = (self._epoch or 0) + 1

    def process_welcome(self, welcome: bytes) -> None:
        self._welcome_calls += 1
        self._ready = True
        self._epoch = (self._epoch or 0) + 1

    def decrypt_opus(self, user_id: int, data: bytes) -> Optional[bytes]:
        if self.fail_decrypt:
            self._decrypt_fail[user_id] = self._decrypt_fail.get(user_id, 0) + 1
            return None
        self._decrypt_ok[user_id] = self._decrypt_ok.get(user_id, 0) + 1
        return data  # identity — pass through unchanged

    def can_passthrough(self, user_id: int) -> bool:
        return self._passthrough

    def set_passthrough_mode(self, enabled: bool, expiry_secs: int = 10) -> None:
        self._passthrough = enabled

    def reset(self) -> None:
        self._ready = False
        self._epoch = None
        self._passthrough = False
        self._decrypt_ok.clear()
        self._decrypt_fail.clear()

    def reinit(
        self,
        protocol_version: int,
        user_id: int,
        channel_id: int,
    ) -> None:
        self.protocol_version = protocol_version
        self.user_id = user_id
        self.channel_id = channel_id
        self._ready = False
        self._epoch = None
        self._decrypt_ok.clear()
        self._decrypt_fail.clear()

    def get_stats(self) -> dict:
        total_ok = sum(self._decrypt_ok.values())
        total_fail = sum(self._decrypt_fail.values())
        total = total_ok + total_fail
        return {
            "epoch": self.epoch,
            "status": self.status_name,
            "ready": self.ready,
            "decrypt_ok": total_ok,
            "decrypt_fail": total_fail,
            "decrypt_rate": (total_ok / total) if total else None,
            "encrypt_ok": 0,
            "encrypt_fail": 0,
            "per_user_ok": dict(self._decrypt_ok),
            "per_user_fail": dict(self._decrypt_fail),
        }
