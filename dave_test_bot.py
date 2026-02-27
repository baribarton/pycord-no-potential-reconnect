"""
DAVE E2EE audio verification bot

-----------------------------------------------
/record start [seconds]   Join your channel, record, upload per-user WAV files, show stats.
/record stop              Stop an in-progress recording early.
/stats                    Print DAVE session stats for the current connection.
/dave                     Quick check: is DAVE E2EE active?
/fault set <type>         Inject a runtime fault into the current voice connection.
/fault clear              Remove all active injected faults.

Fault types
-----------
    commit_error    process_commit raises ValueError on the next op 29.
    ip_timeout      discover_ip raises TimeoutError; WS is closed to trigger reconnect.
    ws_reconnect    Close the voice WS with code 4015 (simulates a server crash/resume).

Setup
-----
1.  pip install -e ".[voice]"   (or ensure the forked pycord is on the path)
2.  set DISCORD_TOKEN=your_bot_token_here
3.  Invite the bot with scopes: applications.commands bot
4.  Run the test

Stats colors
-------------------
    Green  >= 95 %   target met
    Yellow 50–94 %   partial (common if a user joined mid-session)
    Red    < 50 %    something is wrong
    Grey   n/a       channel is not E2EE, or davey is not installed
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord

# DAVE adapter at DEBUG so session events are visible; pycord stays at WARNING.
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)-8s %(name)s: %(message)s",
)
logging.getLogger("discord.dave_session").setLevel(logging.DEBUG)
logging.getLogger("discord.voice_client").setLevel(logging.DEBUG)

TOKEN: str = os.environ.get("DISCORD_TOKEN", "PASTE_TOKEN_HERE")
MAX_RECORD_SECONDS: int = 120

_fault_log = logging.getLogger("dave_test_bot")


def build_stats_embed(vc: discord.VoiceClient) -> discord.Embed:
    """Build an Embed summarising the current DAVE session."""
    if vc.dave_session is None:
        return discord.Embed(
            title="DAVE E2EE — not active",
            description=(
                "This channel is not using DAVE E2EE, "
                "or `davey` is not installed."
            ),
            color=discord.Color.greyple(),
        ).add_field(name="dave_protocol_version", value="0")

    stats = vc.dave_session.get_stats()
    rate = stats["decrypt_rate"]

    if rate is None:
        color = discord.Color.blue()
    elif rate >= 0.95:
        color = discord.Color.green()
    elif rate >= 0.50:
        color = discord.Color.yellow()
    else:
        color = discord.Color.red()

    embed = discord.Embed(title="DAVE E2EE — active", color=color)
    embed.add_field(name="protocol_version", value=str(vc.dave_protocol_version))
    embed.add_field(name="epoch", value=str(stats["epoch"]))
    embed.add_field(name="status", value=stats["status"])
    embed.add_field(name="ready", value=str(stats["ready"]))
    embed.add_field(name="decrypt_ok", value=str(stats["decrypt_ok"]))
    embed.add_field(name="decrypt_fail", value=str(stats["decrypt_fail"]))
    if rate is not None:
        embed.add_field(
            name="decrypt_rate",
            value=f"{rate:.1%}" + (" ✓" if rate >= 0.95 else " ✗ (target 95%)"),
        )
    else:
        embed.add_field(name="decrypt_rate", value="— (no packets yet)")

    per_ok = stats["per_user_ok"]
    if per_ok:
        embed.add_field(
            name="per_user decrypt_ok",
            value="\n".join(f"<@{uid}>: {n}" for uid, n in per_ok.items()),
            inline=False,
        )
    return embed


intents = discord.Intents.default()
intents.voice_states = True

bot = discord.Bot(intents=intents)

_recording: dict[int, bool] = {}  # guild_id → True while recording
_fault_sessions: dict[int, "FaultInjectingSession"] = {}  # guild_id → active wrapper
_original_discover_ip = None  # saved discover_ip; None when ip_timeout is not active


@bot.event
async def on_ready():
    from discord.dave_session import MAX_DAVE_PROTOCOL_VERSION, has_dave
    print(f"[dave-test] Logged in as {bot.user}")
    print(f"[dave-test] davey installed          : {has_dave}")
    print(f"[dave-test] MAX_DAVE_PROTOCOL_VERSION: {MAX_DAVE_PROTOCOL_VERSION}")


record_group = discord.SlashCommandGroup("record", "DAVE audio recording commands")


@record_group.command(name="start", description="Record, play back, and show DAVE stats")
async def record_start(
        ctx: discord.ApplicationContext,
        seconds: int = 90,
):
    if ctx.author.voice is None:
        return await ctx.respond("You need to be in a voice channel first.", ephemeral=True)

    if ctx.guild_id in _recording:
        return await ctx.respond(
            "A recording is already in progress — use `/record stop` to end it.",
            ephemeral=True,
        )

    channel = ctx.author.voice.channel

    await ctx.defer()

    vc: discord.VoiceClient
    if ctx.voice_client is None:
        vc = await channel.connect()
    else:
        if ctx.voice_client.channel != channel:
            await ctx.voice_client.move_to(channel)
        vc = ctx.voice_client

    if vc.recording:
        return await ctx.followup.send(
            "Already recording on this connection.", ephemeral=True
        )

    _recording[ctx.guild_id] = True
    await ctx.followup.send(
        f"🔴 Recording in **{channel.name}** for up to **{seconds}s**.\n"
        f"Say something!  (use `/record stop` to end early)"
    )

    sink = discord.sinks.WaveSink()

    async def on_done(sink: discord.sinks.WaveSink, vc: discord.VoiceClient):
        _recording.pop(ctx.guild_id, None)

        print(f"[dave-test] ── recording finished ──────────────────────────")
        print(f"[dave-test] dave_protocol_version : {vc.dave_protocol_version}")
        if vc.dave_session is not None:
            for k, v in vc.dave_session.get_stats().items():
                print(f"[dave-test]   {k:<24}: {v}")
        else:
            print(f"[dave-test]   dave_session        : None (not an E2EE channel)")
        print(f"[dave-test] ─────────────────────────────────────────────────")

        files = []
        for uid, audio in sink.audio_data.items():
            audio.file.seek(0)
            files.append(discord.File(audio.file, filename=f"user_{uid}.wav"))

        if not sink.audio_data:
            await ctx.followup.send("Recording finished — **no audio captured**.")
            await vc.disconnect()
            return

        await ctx.followup.send(
            f"✅ Done. Captured **{len(sink.audio_data)}** user(s).",
            embed=build_stats_embed(vc),
            files=files,
        )
        await vc.disconnect()

    vc.start_recording(sink, on_done, vc)

    for _ in range(seconds):  # 1s ticks so /record stop can interrupt
        if ctx.guild_id not in _recording:
            break
        await asyncio.sleep(1)

    if vc.recording:
        vc.stop_recording()


@record_group.command(name="stop", description="Stop the current recording early")
async def record_stop(ctx: discord.ApplicationContext):
    vc = ctx.voice_client
    if vc is None or not vc.recording:
        return await ctx.respond("Nothing is being recorded right now.", ephemeral=True)
    _recording.pop(ctx.guild_id, None)
    vc.stop_recording()
    await ctx.respond("⏹️ Recording stopped.")


bot.add_application_command(record_group)


@bot.slash_command(name="stats", description="Show DAVE session stats for this connection")
async def slash_stats(ctx: discord.ApplicationContext):
    if ctx.voice_client is None:
        return await ctx.respond("Not connected to a voice channel.", ephemeral=True)
    vc = ctx.voice_client
    print(f"[dave-test] ── /stats ───────────────────────────────────────────")
    print(f"[dave-test] dave_protocol_version : {vc.dave_protocol_version}")
    if vc.dave_session is not None:
        for k, v in vc.dave_session.get_stats().items():
            print(f"[dave-test]   {k:<24}: {v}")
    else:
        print(f"[dave-test]   dave_session        : None")
    print(f"[dave-test] ─────────────────────────────────────────────────────")
    await ctx.respond(embed=build_stats_embed(vc))


@bot.slash_command(name="dave", description="Check whether DAVE E2EE is active")
async def slash_dave(ctx: discord.ApplicationContext):
    from discord.dave_session import MAX_DAVE_PROTOCOL_VERSION, has_dave

    lines = [
        f"**davey installed:** {has_dave}",
        f"**MAX_DAVE_PROTOCOL_VERSION:** {MAX_DAVE_PROTOCOL_VERSION}",
    ]

    vc = ctx.voice_client
    if vc is not None:
        lines.append(f"**Connected to:** {vc.channel.name}")
        lines.append(f"**dave_protocol_version:** {vc.dave_protocol_version}")
        if vc.dave_session is not None:
            s = vc.dave_session
            lines += [
                f"**session.ready:** {s.ready}",
                f"**session.epoch:** {s.epoch}",
                f"**session.status:** {s.status_name}",
            ]
        else:
            lines.append("**dave_session:** None (channel is not E2EE or davey missing)")
    else:
        lines.append("*(not connected to voice)*")

    await ctx.respond("\n".join(lines), ephemeral=True)


class FaultInjectingSession:
    """Wraps a live DaveSession to inject a single configurable fault at runtime.

    Installed on ``vc.dave_session`` by ``/fault set``.  All attribute access not
    explicitly overridden is forwarded to the real session via ``__getattr__``,
    so the rest of the library sees a fully functional session object.

    Parameters
    ----------
    real_session:
        The real DaveSession to wrap.
    fault_type:
        Which fault to inject (commit_error).
    one_shot:
        If True, the fault fires exactly once and then self-removes (vc.dave_session
        is restored to the real session automatically).  Use this to simulate a
        transient failure and test the recovery path.  If False (default), the
        fault fires on every eligible call until /fault clear is used.
    vc:
        Required when one_shot=True so the wrapper can restore vc.dave_session.
    """

    def __init__(self, real_session, fault_type: str, *, one_shot: bool = False, vc=None) -> None:
        self._real = real_session
        self._fault_type = fault_type
        self._one_shot = one_shot
        self._vc = vc
        self._fired = False

    def __getattr__(self, name: str):
        # Forward everything not defined here to the real session.
        return getattr(self._real, name)

    def _maybe_self_remove(self) -> None:
        """If one_shot mode, restore the real session on vc after firing."""
        if self._one_shot and self._vc is not None:
            self._vc.dave_session = self._real
            _fault_log.info("FAULT: one-shot fired — wrapper removed, real session restored")

    def process_commit(self, commit: bytes) -> None:
        if self._fault_type == "commit_error" and not self._fired:
            self._fired = True
            self._maybe_self_remove()
            _fault_log.warning("FAULT FIRED: commit_error — raising ValueError in process_commit")
            raise ValueError("Injected fault: process_commit failure")
        return self._real.process_commit(commit)

    def process_welcome(self, welcome: bytes) -> None:
        return self._real.process_welcome(welcome)


fault_group = discord.SlashCommandGroup("fault", "Runtime fault injection for DAVE testing")


@fault_group.command(name="set", description="Inject a fault into the current voice connection")
async def fault_set(
        ctx: discord.ApplicationContext,
        fault_type: str = discord.Option(
            description="Fault to inject",
            choices=[
                discord.OptionChoice(name="commit_error", value="commit_error"),
                discord.OptionChoice(name="ip_timeout", value="ip_timeout"),
                discord.OptionChoice(name="ws_reconnect", value="ws_reconnect"),
            ],
        ),
        one_shot: bool = discord.Option(
            default=False,
            description="Fire exactly once then self-clear (realistic transient failure). Default: persistent.",
        ),
):
    """Inject a runtime fault into the active voice connection.

    ws_reconnect: closes the voice WS with code 4015. Triggers RESUME; falls back to
    full reconnect + DAVE re-handshake if RESUME fails. One-shot, no /fault clear needed.

    ip_timeout: patches discover_ip to always raise TimeoutError, then closes the WS.
    Every reconnect attempt fails until /fault clear restores the original method.

    commit_error: wraps dave_session so process_commit raises ValueError on the next op 29.
    Trigger by having a user leave and rejoin to force a new epoch. Expects op 31 sent,
    session reset, and Discord restarting the DAVE handshake.
    """
    vc = ctx.voice_client
    if vc is None:
        return await ctx.respond("Not connected to a voice channel.", ephemeral=True)

    if fault_type == "ws_reconnect":
        print("[dave-test] FAULT: ws_reconnect — closing voice WS with code 4015")
        await vc.ws.close(4015)
        return await ctx.respond(
            "Voice WS closed with code 4015 (server crash simulation).\n"
            "Expected: RESUME attempt → full reconnect + DAVE re-handshake if resume fails.\n"
            "Watch for: `Voice RESUME succeeded` or backoff reconnect logs.",
            ephemeral=True,
        )

    if fault_type == "ip_timeout":
        from discord.gateway import DiscordVoiceWebSocket
        global _original_discover_ip
        if _original_discover_ip is None:
            _original_discover_ip = DiscordVoiceWebSocket.discover_ip

            async def _patched_discover_ip(self, state, max_attempts=2, base_timeout=3.0):
                _fault_log.warning("FAULT FIRED: ip_timeout — raising TimeoutError in discover_ip")
                raise asyncio.TimeoutError()

            DiscordVoiceWebSocket.discover_ip = _patched_discover_ip

        print("[dave-test] FAULT: ip_timeout — discover_ip patched, closing voice WS with code 4015")
        await vc.ws.close(4015)
        return await ctx.respond(
            "discover_ip patched to timeout. WS closed → all reconnect attempts will fail.\n"
            "Run `/fault clear` to restore discover_ip and allow reconnection.",
            ephemeral=True,
        )

    if vc.dave_session is None:
        return await ctx.respond(
            "No DAVE session active — channel may not be E2EE or davey is not installed.",
            ephemeral=True,
        )

    real_session = (  # always wrap the real session, not an existing wrapper
        vc.dave_session._real
        if isinstance(vc.dave_session, FaultInjectingSession)
        else vc.dave_session
    )
    vc.dave_session = FaultInjectingSession(real_session, fault_type, one_shot=one_shot, vc=vc)
    _fault_sessions[ctx.guild_id] = vc.dave_session
    print(f"[dave-test] FAULT: {fault_type} installed (epoch={real_session.epoch}, ready={real_session.ready}, one_shot={one_shot})")

    msg = (
        "commit_error installed on dave_session.\n"
        "Trigger: have a user leave and rejoin the channel to force a new epoch.\n"
        "Expected: op 31 sent, session reset, Discord restarts DAVE handshake."
    )

    await ctx.respond(msg, ephemeral=True)


@fault_group.command(name="clear", description="Remove all injected faults and restore originals")
async def fault_clear(ctx: discord.ApplicationContext):
    vc = ctx.voice_client
    cleared = []

    # Restore discover_ip if ip_timeout patch is active.
    global _original_discover_ip
    if _original_discover_ip is not None:
        from discord.gateway import DiscordVoiceWebSocket
        DiscordVoiceWebSocket.discover_ip = _original_discover_ip
        _original_discover_ip = None
        cleared.append("ip_timeout (discover_ip restored)")

    # Remove the session wrapper if one is installed.
    if vc is not None and isinstance(vc.dave_session, FaultInjectingSession):
        fault_type = vc.dave_session._fault_type
        vc.dave_session = vc.dave_session._real
        cleared.append(f"{fault_type} (session wrapper removed)")

    _fault_sessions.pop(ctx.guild_id, None)

    if cleared:
        print(f"[dave-test] FAULT: cleared — {', '.join(cleared)}")
        await ctx.respond(f"Cleared: {', '.join(cleared)}", ephemeral=True)
    else:
        await ctx.respond("No active faults to clear.", ephemeral=True)


bot.add_application_command(fault_group)

if __name__ == "__main__":
    if TOKEN == "PASTE_TOKEN_HERE":
        sys.exit(
            "Set the DISCORD_TOKEN environment variable, "
            "or paste your token into the TOKEN constant at the top of this file."
        )
    bot.run(TOKEN)
