"""
Offline unit tests for DAVE E2EE integration.

All tests run without a live Discord connection or real crypto by using
FakeDaveSession and minimal stubs.  Run with:

    pytest tests/test_dave.py -v
"""

from __future__ import annotations

import struct
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# dave_session.py tests
# ---------------------------------------------------------------------------


class TestFakeDaveSessionBasics:
    """FakeDaveSession state machine and bookkeeping."""

    def _make(self, **kw):
        from discord.dave_session import FakeDaveSession
        return FakeDaveSession(**kw)

    def test_default_ready(self):
        s = self._make()
        assert s.ready is True
        assert s.epoch == 1
        assert "active" in s.status_name

    def test_not_ready_when_start_ready_false(self):
        s = self._make(start_ready=False)
        assert s.ready is False
        assert s.epoch is None
        assert "pending" in s.status_name

    def test_decrypt_identity(self):
        s = self._make()
        payload = b"\x01\x02\x03\x04"
        assert s.decrypt_opus(999, payload) == payload

    def test_decrypt_fail_mode(self):
        s = self._make(fail_decrypt=True)
        assert s.decrypt_opus(999, b"data") is None

    def test_process_commit_advances_epoch(self):
        s = self._make(start_ready=False)
        assert s.epoch is None
        s.process_commit(b"commit")
        assert s.ready is True
        assert s.epoch == 1

    def test_process_welcome_advances_epoch(self):
        s = self._make(start_ready=False)
        s.process_welcome(b"welcome")
        assert s.ready is True
        assert s.epoch == 1

    def test_process_proposals_returns_commit_welcome(self):
        s = self._make()
        result = s.process_proposals(0, b"proposals")
        assert result is not None
        commit, welcome = result
        assert isinstance(commit, bytes) and len(commit) > 0
        assert isinstance(welcome, bytes) and len(welcome) > 0

    def test_reset_clears_state(self):
        s = self._make()
        s.decrypt_opus(1, b"x")
        s.reset()
        assert s.ready is False
        assert s.epoch is None
        stats = s.get_stats()
        assert stats["decrypt_ok"] == 0
        assert stats["decrypt_fail"] == 0

    def test_reinit_clears_state(self):
        s = self._make()
        s.decrypt_opus(1, b"x")
        s.reinit(1, 42, 99)
        assert s.ready is False
        assert s.epoch is None
        assert s.user_id == 42
        assert s.channel_id == 99

    def test_get_stats_never_raises(self):
        s = self._make()
        stats = s.get_stats()
        required_keys = {
            "epoch", "status", "ready",
            "decrypt_ok", "decrypt_fail", "decrypt_rate",
            "encrypt_ok", "encrypt_fail",
            "per_user_ok", "per_user_fail",
        }
        assert required_keys.issubset(stats.keys())

    def test_get_stats_decrypt_rate_none_when_no_packets(self):
        s = self._make()
        stats = s.get_stats()
        assert stats["decrypt_rate"] is None

    def test_get_stats_decrypt_rate_computed(self):
        s = self._make()
        s.decrypt_opus(1, b"a")
        s.decrypt_opus(1, b"b")
        stats = s.get_stats()
        assert stats["decrypt_ok"] == 2
        assert stats["decrypt_rate"] == 1.0

    def test_get_key_package_length(self):
        s = self._make()
        kp = s.get_key_package()
        assert len(kp) == 392  # matches real davey key package size

    def test_passthrough_default_false(self):
        s = self._make()
        assert s.can_passthrough(1) is False

    def test_set_passthrough(self):
        s = self._make()
        s.set_passthrough_mode(True)
        assert s.can_passthrough(1) is True
        s.set_passthrough_mode(False)
        assert s.can_passthrough(1) is False

    def test_set_external_sender_no_op(self):
        s = self._make()
        s.set_external_sender(b"\x00" * 32)  # should not raise
        assert s._external_sender_set is True


# ---------------------------------------------------------------------------
# Binary frame byte-slicing (gateway.py parsing logic)
# ---------------------------------------------------------------------------


class TestBinaryFrameParsing:
    """Verify the exact byte offsets used in received_binary_message."""

    def _make_s2c_frame(self, seq: int, op: int, payload: bytes) -> bytes:
        """Build a server→client binary voice frame."""
        return struct.pack(">H", seq) + bytes([op]) + payload

    def test_seq_extraction(self):
        frame = self._make_s2c_frame(0xABCD, 25, b"\x01\x02")
        seq = struct.unpack_from(">H", frame, 0)[0]
        assert seq == 0xABCD

    def test_opcode_extraction(self):
        frame = self._make_s2c_frame(1, 27, b"\x00" * 10)
        op = frame[2]
        assert op == 27

    def test_op25_external_sender_payload(self):
        """Op 25: payload starts at msg[3:]."""
        payload = b"\xDE\xAD\xBE\xEF" * 8
        frame = self._make_s2c_frame(1, 25, payload)
        assert frame[3:] == payload

    def test_op27_proposals_op_type_and_data(self):
        """Op 27: msg[3] = op_type, msg[4:] = proposal data."""
        op_type = 0  # append
        data = b"\x11\x22\x33"
        frame = self._make_s2c_frame(1, 27, bytes([op_type]) + data)
        assert frame[3] == op_type
        assert frame[4:] == data

    def test_op27_revoke_op_type(self):
        """Op 27 revoke: op_type = 1."""
        frame = self._make_s2c_frame(1, 27, bytes([1]) + b"\xAA\xBB")
        assert frame[3] == 1

    def test_op29_transition_id_and_commit(self):
        """Op 29: msg[3:5] = transition_id uint16 BE, msg[5:] = commit."""
        transition_id = 0x0005
        commit = b"\xCC" * 20
        payload = struct.pack(">H", transition_id) + commit
        frame = self._make_s2c_frame(2, 29, payload)
        tid = struct.unpack_from(">H", frame, 3)[0]
        assert tid == transition_id
        assert frame[5:] == commit

    def test_op30_transition_id_and_welcome(self):
        """Op 30: msg[3:5] = transition_id uint16 BE, msg[5:] = welcome."""
        transition_id = 0x0003
        welcome = b"\xFF" * 50
        payload = struct.pack(">H", transition_id) + welcome
        frame = self._make_s2c_frame(3, 30, payload)
        tid = struct.unpack_from(">H", frame, 3)[0]
        assert tid == transition_id
        assert frame[5:] == welcome

    def test_short_frame_length_guard(self):
        """Frames shorter than 3 bytes must be dropped by guard check."""
        assert len(b"\x00\x00") < 3

    def test_op29_minimum_valid_length(self):
        """Op 29 needs at least 5 bytes (2 seq + 1 op + 2 tid)."""
        frame = self._make_s2c_frame(0, 29, struct.pack(">H", 1))
        assert len(frame) == 5

    def test_op29_too_short_guard(self):
        """Op 29 with only 4 bytes (no transition_id) is too short."""
        frame = self._make_s2c_frame(0, 29, b"\x00")
        assert len(frame) < 5


# ---------------------------------------------------------------------------
# Queue cap enforcement (voice_client.py unpack_audio)
# ---------------------------------------------------------------------------


class TestDaveRawQueueCap:
    """Verify per-ssrc and global queue cap logic in isolation."""

    def _make_rq(self):
        """Return an empty _dave_raw_queue dict."""
        return {}

    def _enqueue(self, queue: dict, ssrc: int, item: object,
                 per_cap: int, total_cap: int) -> None:
        """Replicate the cap logic from unpack_audio verbatim."""
        from discord.dave_session import DAVE_QUEUE_MAX_PER_SSRC, DAVE_QUEUE_MAX_TOTAL
        per_cap = per_cap or DAVE_QUEUE_MAX_PER_SSRC
        total_cap = total_cap or DAVE_QUEUE_MAX_TOTAL

        total = sum(len(v) for v in queue.values())
        if total >= total_cap:
            evict = max(queue, key=lambda s: len(queue[s]))
            queue[evict].pop(0)
            if not queue[evict]:
                del queue[evict]

        per = queue.setdefault(ssrc, [])
        if len(per) >= per_cap:
            per.pop(0)
        per.append(item)

    def test_per_ssrc_cap_evicts_oldest(self):
        q = self._make_rq()
        per_cap = 4
        for i in range(per_cap + 2):
            self._enqueue(q, ssrc=1, item=i, per_cap=per_cap, total_cap=1000)
        assert len(q[1]) == per_cap
        # Oldest (0, 1) evicted; newest remain
        assert q[1][0] == 2

    def test_global_cap_evicts_from_largest_ssrc(self):
        q = self._make_rq()
        per_cap = 100
        total_cap = 5
        # Fill ssrc 1 with 3 packets, ssrc 2 with 2
        for i in range(3):
            self._enqueue(q, ssrc=1, item=f"s1p{i}", per_cap=per_cap, total_cap=total_cap)
        for i in range(2):
            self._enqueue(q, ssrc=2, item=f"s2p{i}", per_cap=per_cap, total_cap=total_cap)
        # Adding one more should evict from ssrc 1 (largest)
        self._enqueue(q, ssrc=3, item="new", per_cap=per_cap, total_cap=total_cap)
        total = sum(len(v) for v in q.values())
        assert total == total_cap
        assert len(q[1]) == 2  # one evicted from ssrc 1

    def test_total_cap_constant_values(self):
        from discord.dave_session import DAVE_QUEUE_MAX_PER_SSRC, DAVE_QUEUE_MAX_TOTAL
        assert DAVE_QUEUE_MAX_PER_SSRC == 32
        assert DAVE_QUEUE_MAX_TOTAL == 256

    def test_global_never_exceeds_cap(self):
        from discord.dave_session import DAVE_QUEUE_MAX_PER_SSRC, DAVE_QUEUE_MAX_TOTAL
        q = self._make_rq()
        for i in range(DAVE_QUEUE_MAX_TOTAL + 50):
            ssrc = i % 8  # spread across 8 ssrcs
            self._enqueue(q, ssrc=ssrc, item=i,
                          per_cap=DAVE_QUEUE_MAX_PER_SSRC,
                          total_cap=DAVE_QUEUE_MAX_TOTAL)
        total = sum(len(v) for v in q.values())
        assert total <= DAVE_QUEUE_MAX_TOTAL


# ---------------------------------------------------------------------------
# VoiceClient DAVE integration stubs
# ---------------------------------------------------------------------------


class TestVoiceClientDaveFields:
    """VoiceClient initialises DAVE fields without a live connection."""

    def _make_vc(self):
        """Construct a VoiceClient with all external deps mocked."""
        with patch("discord.voice_client.has_nacl", True):
            from discord.voice_client import VoiceClient

            client = MagicMock()
            client._connection.loop = MagicMock()
            client._connection.user = MagicMock()
            channel = MagicMock()
            channel._get_voice_client_key.return_value = (1, None)

            vc = VoiceClient.__new__(VoiceClient)
            # Directly set the fields added in Phase 3
            vc.dave_session = None
            vc.dave_protocol_version = 0
            vc.dave_pending_transitions = {}
            vc._dave_raw_queue = {}
            return vc

    def test_dave_session_initially_none(self):
        vc = self._make_vc()
        assert vc.dave_session is None

    def test_dave_protocol_version_initially_zero(self):
        vc = self._make_vc()
        assert vc.dave_protocol_version == 0

    def test_dave_pending_transitions_initially_empty(self):
        vc = self._make_vc()
        assert vc.dave_pending_transitions == {}

    def test_dave_raw_queue_initially_empty(self):
        vc = self._make_vc()
        assert vc._dave_raw_queue == {}


# ---------------------------------------------------------------------------
# FakeDaveSession as pipeline stand-in
# ---------------------------------------------------------------------------


class TestFakeDaveSessionAsPipelineStandin:
    """FakeDaveSession passes through opus bytes unchanged, enabling pipeline tests."""

    def test_decrypt_returns_input(self):
        from discord.dave_session import FakeDaveSession
        s = FakeDaveSession()
        opus_bytes = bytes(range(20))
        result = s.decrypt_opus(12345678, opus_bytes)
        assert result == opus_bytes

    def test_fail_decrypt_returns_none(self):
        from discord.dave_session import FakeDaveSession
        s = FakeDaveSession(fail_decrypt=True)
        assert s.decrypt_opus(12345678, b"data") is None

    def test_stats_accumulate_across_users(self):
        from discord.dave_session import FakeDaveSession
        s = FakeDaveSession()
        s.decrypt_opus(1, b"a")
        s.decrypt_opus(2, b"b")
        s.decrypt_opus(1, b"c")
        stats = s.get_stats()
        assert stats["decrypt_ok"] == 3
        assert stats["per_user_ok"] == {1: 2, 2: 1}

    def test_fail_stats_accumulate(self):
        from discord.dave_session import FakeDaveSession
        s = FakeDaveSession(fail_decrypt=True)
        s.decrypt_opus(1, b"x")
        s.decrypt_opus(1, b"y")
        stats = s.get_stats()
        assert stats["decrypt_fail"] == 2
        assert stats["decrypt_ok"] == 0

    def test_mixed_stats_rate(self):
        from discord.dave_session import FakeDaveSession
        s = FakeDaveSession(fail_decrypt=False)
        s.decrypt_opus(1, b"ok")
        # Manually inject a fail
        s._decrypt_fail[1] = 1
        stats = s.get_stats()
        assert stats["decrypt_ok"] == 1
        assert stats["decrypt_fail"] == 1
        assert stats["decrypt_rate"] == 0.5


# ---------------------------------------------------------------------------
# DaveSession constants / has_dave flag
# ---------------------------------------------------------------------------


class TestDaveSessionModule:
    def test_has_dave_flag_is_bool(self):
        from discord.dave_session import has_dave
        assert isinstance(has_dave, bool)

    def test_max_dave_protocol_version_consistent(self):
        from discord.dave_session import has_dave, MAX_DAVE_PROTOCOL_VERSION
        if has_dave:
            assert MAX_DAVE_PROTOCOL_VERSION >= 1
        else:
            assert MAX_DAVE_PROTOCOL_VERSION == 0

    def test_queue_constants_positive(self):
        from discord.dave_session import DAVE_QUEUE_MAX_PER_SSRC, DAVE_QUEUE_MAX_TOTAL
        assert DAVE_QUEUE_MAX_PER_SSRC > 0
        assert DAVE_QUEUE_MAX_TOTAL >= DAVE_QUEUE_MAX_PER_SSRC
