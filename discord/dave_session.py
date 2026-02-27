"""
DAVE (Discord Audio/Video E2EE) session adapter.

Wraps the ``davey`` library behind a stable interface so no other module ever
imports ``davey`` directly.  Swapping the backend later means changing only
this file.

Protocol reference: Discord DAVE protocol specification
Library docs: davey 0.1.3 type stubs (davey/__init__.pyi)
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional import — keep non-DAVE voice working if davey isn't installed.
# ---------------------------------------------------------------------------

has_dave: bool

try:
    import davey as _davey

    has_dave = True
    MAX_DAVE_PROTOCOL_VERSION: int = _davey.DAVE_PROTOCOL_VERSION  # = 1
except ImportError:  # pragma: no cover
    _davey = None  # type: ignore[assignment]
    has_dave = False
    MAX_DAVE_PROTOCOL_VERSION = 0

# ---------------------------------------------------------------------------
# Queue cap constants (used by VoiceClient._dave_raw_queue).
# Exposed here so tests and voice_client.py share a single source of truth.
# ---------------------------------------------------------------------------

#: Maximum un-drained packets held per SSRC while waiting for ssrc→user_id.
DAVE_QUEUE_MAX_PER_SSRC: int = 32

#: Maximum total packets across all SSRCs in the raw queue.
DAVE_QUEUE_MAX_TOTAL: int = 256


# ---------------------------------------------------------------------------
# Live adapter
# ---------------------------------------------------------------------------


class DaveSession:
    """Thin adapter over :class:`davey.DaveSession`.

    All interaction with the ``davey`` library is confined to this class.
    """

    def __init__(
        self,
        protocol_version: int,
        user_id: int,
        channel_id: int,
    ) -> None:
        if not has_dave:
            raise RuntimeError(
                "E2EE voice requires the 'davey' library. "
                "Install it with: pip install davey"
            )
        self._davey_session: _davey.DaveSession = _davey.DaveSession(
            protocol_version, user_id, channel_id
        )
        # Protects decrypt_opus from concurrent calls: recv_audio thread and
        # the event-loop thread (_drain_dave_queue) can both call decrypt().
        self._lock = threading.Lock()
        # Track decrypt outcomes ourselves — get_decryption_stats() raises
        # ValueError for unknown users, so we can't use it safely in a loop.
        self._decrypt_ok: dict[int, int] = {}
        self._decrypt_fail: dict[int, int] = {}
        _log.debug(
            "DaveSession created: protocol_version=%d user_id=%d channel_id=%d",
            protocol_version,
            user_id,
            channel_id,
        )

    # ── read-only state ──────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """``True`` after MLS welcome or own commit is processed (session active)."""
        return self._davey_session.ready

    @property
    def epoch(self) -> Optional[int]:
        """Current MLS epoch, or ``None`` before the first group is established."""
        return self._davey_session.epoch

    @property
    def status_name(self) -> str:
        """Human-readable status string, e.g. ``'SessionStatus.active'``."""
        return str(self._davey_session.status)

    # ── MLS handshake ────────────────────────────────────────────────────────

    def set_external_sender(self, data: bytes) -> None:
        """Process the voice-gateway external sender package (op 25 payload).

        ``data`` is ``msg[3:]`` — everything after the 2-byte seq + 1-byte op.

        Raises
        ------
        ValueError
            If the external sender data is malformed.
        """
        self._davey_session.set_external_sender(data)
        _log.debug("DAVE: external sender set (%d bytes)", len(data))

    def get_key_package(self) -> bytes:
        """Generate and return a fresh serialised MLS key package.

        Each call produces a new key package (unique nonce per MLS spec).
        Do not call more than once per handshake phase.
        """
        key_package = self._davey_session.get_serialized_key_package()
        _log.debug("DAVE: key package generated (%d bytes)", len(key_package))
        return key_package

    def process_proposals(
        self,
        operation_type: int,
        data: bytes,
    ) -> Optional[tuple[bytes, Optional[bytes]]]:
        """Process an MLS proposals message (op 27 payload after op-type byte).

        Parameters
        ----------
        operation_type:
            ``0`` = append, ``1`` = revoke
            (from ``msg[3]`` in op-27 binary frame).
        data:
            Raw proposal bytes (``msg[4:]`` in op-27 binary frame).

        Returns
        -------
        ``(commit, welcome_or_None)`` if a commit was produced, else ``None``.

        Raises
        ------
        ValueError
            If proposals cannot be processed (e.g. no group yet).
        """
        proposals_operation = (
            _davey.ProposalsOperationType.append
            if operation_type == 0
            else _davey.ProposalsOperationType.revoke
        )
        result = self._davey_session.process_proposals(proposals_operation, data)
        if isinstance(result, _davey.CommitWelcome):
            _log.debug(
                "DAVE: proposals processed → commit produced (welcome=%s)",
                result.welcome is not None,
            )
            return result.commit, result.welcome
        _log.debug("DAVE: proposals processed → no commit")
        return None

    def process_commit(self, commit: bytes) -> None:
        """Process a broadcast MLS commit (op 29 payload at ``msg[5:]``).

        Raises
        ------
        ValueError
            If the commit is invalid or cannot be applied.
        """
        self._davey_session.process_commit(commit)
        _log.debug(
            "DAVE: commit processed, epoch=%s ready=%s", self.epoch, self.ready
        )

    def process_welcome(self, welcome: bytes) -> None:
        """Process an MLS welcome (op 30 payload at ``msg[5:]``).

        Raises
        ------
        ValueError
            If the welcome is invalid or cannot be applied.
        """
        self._davey_session.process_welcome(welcome)
        _log.debug(
            "DAVE: welcome processed, epoch=%s ready=%s", self.epoch, self.ready
        )

    # ── media decrypt (recording path) ───────────────────────────────────────

    def decrypt_opus(self, user_id: int, data: bytes) -> Optional[bytes]:
        """Decrypt a DAVE-encrypted opus packet.

        Parameters
        ----------
        user_id:
            Discord snowflake integer — **not** the RTP SSRC.
            The caller must resolve ``ssrc → user_id`` before calling this.
        data:
            Transport-decrypted bytes (output of the nacl layer).

        Returns
        -------
        Decrypted opus bytes on success, ``None`` on failure (packet should
        be dropped silently).

        Notes
        -----
        Possible ``ValueError`` reasons from davey:

        * ``NoDecryptorForUser`` — session not yet active for this user.
        * ``DecryptionFailed`` — wrong key / corrupted frame.
        * ``DuplicateNonce`` — replay protection triggered.
        """
        with self._lock:
            try:
                result = self._davey_session.decrypt(user_id, _davey.MediaType.audio, data)
                self._decrypt_ok[user_id] = self._decrypt_ok.get(user_id, 0) + 1
                return result
            except ValueError as exc:
                self._decrypt_fail[user_id] = self._decrypt_fail.get(user_id, 0) + 1
                error_message = str(exc)
                if "NoDecryptorForUser" in error_message:
                    # Normal during MLS handshake — new member not yet welcomed.
                    _log.debug("DAVE decrypt: no decryptor yet uid=%d (handshake pending)", user_id)
                elif "DuplicateNonce" in error_message:
                    # Replay protection — benign, discard silently.
                    _log.debug("DAVE decrypt: duplicate nonce uid=%d", user_id)
                elif "NoValidCryptorFound" in error_message:
                    # Expected during epoch-transition window: remote is still
                    # encrypting with the previous epoch's keys.  Benign drop.
                    _log.debug("DAVE decrypt: no valid cryptor uid=%d (epoch transition)", user_id)
                else:
                    # DecryptionFailed or unknown — worth knowing about.
                    _log.warning("DAVE decrypt failed uid=%d: %s", user_id, exc)
                return None
            except Exception as exc:
                # Unexpected error from the native library — drop the packet
                # rather than crashing the recv_audio thread.
                self._decrypt_fail[user_id] = self._decrypt_fail.get(user_id, 0) + 1
                _log.error("DAVE decrypt unexpected error uid=%d: %s", user_id, exc)
                return None

    def can_passthrough(self, user_id: int) -> bool:
        """Whether unencrypted frames are allowed through for this user.

        Returns ``False`` before a decryptor is registered (i.e. before
        welcome completes).  ``True`` only if passthrough mode is active
        *and* a decryptor exists.
        """
        return self._davey_session.can_passthrough(user_id)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def set_passthrough_mode(self, enabled: bool, expiry_secs: int = 10) -> None:
        """Enable or disable passthrough mode on all decryptors.

        ``expiry_secs`` is ignored when ``enabled=False``.
        """
        self._davey_session.set_passthrough_mode(enabled, expiry_secs)
        _log.debug("DAVE: passthrough_mode=%s expiry=%ds", enabled, expiry_secs)

    def reset(self) -> None:
        """Full reset — clears MLS group state and all decryptors."""
        self._davey_session.reset()
        self._decrypt_ok.clear()
        self._decrypt_fail.clear()
        _log.debug("DAVE: session reset")

    def reinit(
        self,
        protocol_version: int,
        user_id: int,
        channel_id: int,
    ) -> None:
        """Re-initialise in place (equivalent to reset + new session params).

        Called on ``DAVE_PREPARE_EPOCH(epoch=1)`` to handle group recreation.
        After this call: ``epoch=None``, ``ready=False``.
        """
        self._davey_session.reinit(protocol_version, user_id, channel_id)
        self._decrypt_ok.clear()
        self._decrypt_fail.clear()
        _log.debug(
            "DAVE: session reinit protocol_version=%d user_id=%d channel_id=%d",
            protocol_version,
            user_id,
            channel_id,
        )

    # ── metrics ──────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return a snapshot of session metrics suitable for logging or testing.

        Never raises — safe to call at any point in the session lifecycle.
        """
        # get_encryption_stats() always returns an object (never raises).
        encryption_stats = self._davey_session.get_encryption_stats()
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
            "encrypt_ok": encryption_stats.successes,
            "encrypt_fail": encryption_stats.failures,
            "per_user_ok": dict(self._decrypt_ok),
            "per_user_fail": dict(self._decrypt_fail),
        }


# ---------------------------------------------------------------------------
# Fake session for offline testing
# ---------------------------------------------------------------------------


class FakeDaveSession:
    """Drop-in replacement for :class:`DaveSession` that requires no real crypto.

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
        # Simulate producing a commit + welcome so callers exercise that path.
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
