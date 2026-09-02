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
import time
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

#: Maximum buffered packets per audio stream while we wait to learn which Discord user it belongs to.
DAVE_QUEUE_MAX_PER_SSRC: int = 32

#: Maximum total buffered packets across all audio streams.
DAVE_QUEUE_MAX_TOTAL: int = 256

#: Seconds of failures with no successes before a user counts as cut off.
STUCK_USER_SECONDS: float = 180.0

#: Log a user's first few decrypt failures, then only every Nth, so one cut-off user
#: cannot fill the log with fifty identical lines a second.
DECRYPT_FAILURES_LOGGED_IN_FULL: int = 5
DECRYPT_FAILURE_LOG_INTERVAL: int = 1000


class _DecryptFailures:
    """One user's unbroken run of decrypt failures. Discarded as soon as they decrypt again."""

    __slots__ = ("started_at", "count", "reported")

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.count = 0
        self.reported = False


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
        guild_id: Optional[int] = None,
    ) -> None:
        if not has_dave:
            raise RuntimeError(
                "E2EE voice requires the 'davey' library. "
                "Install it with: pip install davey"
            )
        self._davey_session: _davey.DaveSession = _davey.DaveSession(
            protocol_version, user_id, channel_id
        )
        # recv_audio and _drain_dave_queue can both call decrypt() concurrently.
        self._lock = threading.Lock()
        self._decrypt_ok: dict[int, int] = {}
        self._decrypt_fail: dict[int, int] = {}
        self._current_failures: dict[int, _DecryptFailures] = {}
        self._guild_id = guild_id
        _log.debug(
            "DaveSession created: protocol_version=%d user_id=%d channel_id=%d guild_id=%s",
            protocol_version,
            user_id,
            channel_id,
            guild_id,
        )

    # ── read-only state ──────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """``True`` once the encrypted group is established and audio can be decrypted."""
        return self._davey_session.ready

    @property
    def epoch(self) -> Optional[int]:
        """Current key generation number — increments each time group membership changes. ``None`` before the first group is formed."""
        return self._davey_session.epoch

    @property
    def status_name(self) -> str:
        """Human-readable status string, e.g. ``'SessionStatus.active'``."""
        return str(self._davey_session.status)

    # ── MLS handshake ────────────────────────────────────────────────────────

    def set_external_sender(self, data: bytes) -> None:
        """Register the server's signing authority used to validate key-exchange messages (op 25).

        ``data`` is ``msg[3:]`` — the payload after the 3-byte binary frame header.

        Raises
        ------
        ValueError
            If the data is malformed.
        """
        self._davey_session.set_external_sender(data)
        _log.debug("DAVE: external sender set (%d bytes)", len(data))

    def get_key_package(self) -> bytes:
        """Generate and return a fresh key package to send to the server.

        A key package bundles this client's public cryptographic credentials.
        The server shares it with other group members so they can include us in
        encryption. Each call produces a different package — only call once per handshake.
        """
        key_package = self._davey_session.get_serialized_key_package()
        _log.debug("DAVE: key package generated (%d bytes)", len(key_package))
        return key_package

    def process_proposals(
        self,
        operation_type: int,
        data: bytes,
    ) -> Optional[tuple[bytes, Optional[bytes]]]:
        """Process a membership-change request from the server (op 27).

        The server sends these to add or remove users from the encrypted group.
        If accepted, this returns a commit (and optionally a welcome for any newly
        added users) to send back to the server.

        Parameters
        ----------
        operation_type:
            ``0`` = add user, ``1`` = remove user
            (from ``msg[3]`` in the op-27 binary frame).
        data:
            Raw proposal bytes (``msg[4:]`` in the op-27 binary frame).

        Returns
        -------
        ``(commit, welcome_or_None)`` if the change was accepted, else ``None``.

        Raises
        ------
        ValueError
            If the change cannot be applied (e.g. no group established yet).
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
        """Apply a key-rotation commit broadcast by the server (op 29).

        A commit finalises pending membership changes and rotates the group's
        encryption key. Once applied, ``epoch`` increments and ``ready`` is ``True``.

        Raises
        ------
        ValueError
            If the commit is invalid or cannot be applied.
        """
        self._davey_session.process_commit(commit)
        _log.debug(
            "DAVE: commit processed, epoch=%s ready=%s guild_id=%s",
            self.epoch,
            self.ready,
            self._guild_id,
        )

    def process_welcome(self, welcome: bytes) -> None:
        """Apply a welcome message that adds this client to the encrypted group (op 30).

        A welcome carries the current group encryption key, encrypted specifically for
        this client. Once applied, ``epoch`` is set and ``ready`` is ``True``.

        Raises
        ------
        ValueError
            If the welcome is invalid or cannot be applied.
        """
        self._davey_session.process_welcome(welcome)
        _log.debug(
            "DAVE: welcome processed, epoch=%s ready=%s guild_id=%s",
            self.epoch,
            self.ready,
            self._guild_id,
        )

    # ── media decrypt (recording path) ───────────────────────────────────────

    def decrypt_opus(self, user_id: int, data: bytes) -> Optional[bytes]:
        """Decrypt a DAVE-encrypted opus packet.

        Parameters
        ----------
        user_id:
            Discord user ID — not the RTP stream ID (SSRC). The caller must
            look up ``ssrc_map[ssrc]["user_id"]`` before calling this.
        data:
            Audio bytes after transport decryption (output of the NaCl layer).

        Returns
        -------
        Decrypted opus bytes on success, ``None`` if the packet should be dropped.

        Notes
        -----
        Reasons a packet may be dropped:

        * ``NoDecryptorForUser`` — encryption key not yet established for this user.
        * ``DecryptionFailed`` — wrong key or corrupted audio frame.
        * ``DuplicateNonce`` — packet counter already seen; dropped by anti-replay check.
        """
        with self._lock:
            try:
                result = self._davey_session.decrypt(user_id, _davey.MediaType.audio, data)
                self._decrypt_ok[user_id] = self._decrypt_ok.get(user_id, 0) + 1
                self._current_failures.pop(user_id, None)
                return result
            except ValueError as exc:
                failures = self._record_failure(user_id)
                error_message = str(exc)
                if failures and "NoDecryptorForUser" in error_message:
                    # Expected during key exchange — this user's encryption key isn't set up yet.
                    _log.debug(
                        "DAVE decrypt: no decryptor yet (handshake pending) uid=%d guild_id=%s failure=%d",
                        user_id,
                        self._guild_id,
                        failures,
                    )
                elif failures and "DuplicateNonce" in error_message:
                    # Replay protection: this packet counter was already seen, so drop it.
                    _log.debug(
                        "DAVE decrypt: duplicate nonce uid=%d guild_id=%s failure=%d",
                        user_id,
                        self._guild_id,
                        failures,
                    )
                elif failures and "NoValidCryptorFound" in error_message:
                    # We hold a key for this user but it will not open this frame.
                    _log.debug(
                        "DAVE decrypt: no valid cryptor uid=%d guild_id=%s failure=%d",
                        user_id,
                        self._guild_id,
                        failures,
                    )
                elif failures:
                    # DecryptionFailed or unknown — worth knowing about.
                    _log.warning(
                        "DAVE decrypt failed uid=%d guild_id=%s failure=%d: %s",
                        user_id,
                        self._guild_id,
                        failures,
                        exc,
                    )
                return None
            except Exception as exc:
                # Drop rather than crash the recv thread.
                failures = self._record_failure(user_id)
                if failures:
                    _log.error(
                        "DAVE decrypt unexpected error uid=%d guild_id=%s: %s",
                        user_id,
                        self._guild_id,
                        exc,
                    )
                return None

    def _record_failure(self, user_id: int) -> int:
        """Count a decrypt failure and return its number, or 0 if it should not be logged.

        Also reports a user whose audio has not decrypted at all for a long time, once per
        run of failures. Key rotation never fails for long enough to qualify.
        """
        self._decrypt_fail[user_id] = self._decrypt_fail.get(user_id, 0) + 1
        failures = self._current_failures.get(user_id)
        if failures is None:
            failures = self._current_failures[user_id] = _DecryptFailures()
        failures.count += 1

        if (
            not failures.reported
            and time.monotonic() - failures.started_at >= STUCK_USER_SECONDS
        ):
            failures.reported = True
            _log.warning(
                "DAVE: no audio can be decrypted for this user: %s",
                self._describe(user_id),
            )

        if (
            failures.count <= DECRYPT_FAILURES_LOGGED_IN_FULL
            or failures.count % DECRYPT_FAILURE_LOG_INTERVAL == 0
        ):
            return failures.count
        return 0

    def _describe(self, user_id: int) -> str:
        """Describe why one user's audio will not decrypt.

        ``in_group`` is the useful part: a user missing from the encrypted group was
        never given to us, while one present means our key for them does not work.
        """
        try:
            group_user_ids = [int(uid) for uid in self._davey_session.get_user_ids()]
            in_group = user_id in group_user_ids
        except Exception as exc:
            group_user_ids, in_group = [], f"unknown ({exc})"

        try:
            stats = self._davey_session.get_decryption_stats(
                user_id, _davey.MediaType.audio
            )
            stats_text = (
                f"successes={stats.successes} failures={stats.failures} "
                f"attempts={stats.attempts} passthroughs={stats.passthroughs}"
            )
        except Exception:
            stats_text = "unavailable"

        return (
            f"uid={user_id} guild_id={self._guild_id} in_group={in_group} "
            f"epoch={self.epoch} ready={self.ready} status={self.status_name} "
            f"group_size={len(group_user_ids)} group={group_user_ids} "
            f"davey[{stats_text}]"
        )

    def can_passthrough(self, user_id: int) -> bool:
        """Whether unencrypted audio frames should be accepted for this user.

        ``True`` only during a passthrough window (key rotation or protocol downgrade)
        and only once this user's encryption key has been registered. ``False`` otherwise.
        """
        return self._davey_session.can_passthrough(user_id)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def set_passthrough_mode(self, enabled: bool, expiry_secs: int = 10) -> None:
        """Temporarily allow unencrypted audio through during key rotation or protocol changes.

        Used so audio isn't dropped while new encryption keys are being negotiated.
        ``expiry_secs`` is ignored when ``enabled=False``.
        """
        self._davey_session.set_passthrough_mode(enabled, expiry_secs)
        _log.debug("DAVE: passthrough_mode=%s expiry=%ds", enabled, expiry_secs)

    def reset(self) -> None:
        """Full reset — clears all encryption state and per-user keys."""
        self._davey_session.reset()
        self._decrypt_ok.clear()
        self._decrypt_fail.clear()
        self._current_failures.clear()
        _log.debug("DAVE: session reset")

    def reinit(
        self,
        protocol_version: int,
        user_id: int,
        channel_id: int,
        guild_id: Optional[int] = None,
    ) -> None:
        """Re-initialise in place with new session parameters (equivalent to reset + reconfigure).

        Called when the server signals the encrypted group is being rebuilt from scratch
        (``DAVE_PREPARE_EPOCH`` with ``epoch=1``).
        After this call the session is back to its initial state: ``epoch=None``, ``ready=False``.
        """
        self._davey_session.reinit(protocol_version, user_id, channel_id)
        self._decrypt_ok.clear()
        self._decrypt_fail.clear()
        self._current_failures.clear()
        self._guild_id = guild_id
        _log.debug(
            "DAVE: session reinit protocol_version=%d user_id=%d channel_id=%d guild_id=%s",
            protocol_version,
            user_id,
            channel_id,
            guild_id,
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

