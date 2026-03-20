#!/usr/bin/env python3
"""Singleton IRC relay daemon.

Each Claude session gets its own IRC connection with a unique nick.
The relay manages connections, buffers messages, and nudges sessions
via claude-injector watchfiles.
"""

import asyncio
import json
import logging
import os
import time

import irc.client_aio
import irc.connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("relay")

IRC_SERVER = "localhost"
IRC_PORT = 6667
IRC_CHANNEL = "#loom"
RELAY_NICK = "relay"

SOCKET_PATH = "/tmp/claude-chat/relay.sock"
INJECTOR_DIR = "/tmp/claude-injector"

MAX_MESSAGES = 500  # ring buffer size


class Session:
    """One Claude session = one IRC connection."""

    def __init__(self, session_id: str, nick: str, state: "RelayState"):
        self.session_id = session_id
        self.nick = nick
        self.state = state
        self.last_read = state.next_index
        self.joined_at = time.time()
        self.last_active = time.time()
        self.conn = None
        self.reactor = None
        self.connected = False
        self.channel_joined = False
        self.nudge_count = 0
        self.last_nudge_time = 0.0
        self.last_get_time = 0.0
        self.reconnect_delay = 5  # exponential backoff: 5, 10, 20, 40, ... max 300

    async def connect(self, loop: asyncio.AbstractEventLoop):
        """Open a dedicated IRC connection for this session."""
        self.reactor = irc.client_aio.AioReactor(loop=loop)
        self.conn = self.reactor.server()
        state = self.state
        session = self

        def on_connect(connection, event):
            log.info("[%s] Connected, joining %s", session.nick, IRC_CHANNEL)
            connection.join(IRC_CHANNEL)
            session.connected = True
            session.reconnect_delay = 5  # reset backoff on successful connect

        def on_join(connection, event):
            if event.source.nick == session.nick:
                log.info("[%s] Joined %s", session.nick, IRC_CHANNEL)
                session.channel_joined = True

        def on_pubmsg(connection, event):
            nick = event.source.nick
            message = event.arguments[0]
            # Only the relay connection buffers messages to avoid duplicates
            if session.session_id == "_relay":
                log.info("[IRC] <%s> %s", nick, message)
                state.add_message(nick, message)
                # Nudge all registered sessions except the sender
                for sid, sess in list(state.sessions.items()):
                    if sid != "_relay" and sess.nick != nick:
                        nudge_session(sid)

        def on_nicknameinuse(connection, event):
            # Nick collision — append or increment a suffix
            old = session.nick
            base = old.rstrip("0123456789-")
            suffix = old[len(base):]
            if suffix and suffix.startswith("-"):
                try:
                    n = int(suffix[1:])
                    session.nick = f"{base}-{n + 1}"
                except ValueError:
                    session.nick = f"{old}-2"
            else:
                session.nick = f"{base}-2"
            log.warning("[%s] Nick in use, trying %s", old, session.nick)
            connection.nick(session.nick)

        def on_disconnect(connection, event):
            session.connected = False
            session.channel_joined = False
            if session.session_id != "_relay":
                # Only reconnect if we're still in the sessions dict
                # (if reaped by cleanup, don't create orphan connections)
                if session.session_id not in session.state.sessions:
                    log.info("[%s] Session was removed, not reconnecting", session.nick)
                    return
                delay = session.reconnect_delay
                log.warning("[%s] Disconnected from IRC, reconnecting in %ds...", session.nick, delay)
                loop.call_later(delay, lambda: loop.create_task(session.connect(loop)))
                session.reconnect_delay = min(session.reconnect_delay * 2, 300)
            else:
                log.warning("[relay] Disconnected, reconnecting in 5s...")
                loop.call_later(5, lambda: loop.create_task(session.connect(loop)))

        def on_all(connection, event):
            log.debug("[%s] IRC event: %s %s %s",
                      session.nick, event.type, event.source, event.arguments)

        self.reactor.add_global_handler("all_events", on_all)
        self.reactor.add_global_handler("welcome", on_connect)
        self.reactor.add_global_handler("join", on_join)
        self.reactor.add_global_handler("pubmsg", on_pubmsg)
        self.reactor.add_global_handler("nicknameinuse", on_nicknameinuse)
        self.reactor.add_global_handler("disconnect", on_disconnect)

        log.info("[%s] Connecting to %s:%d...", self.nick, IRC_SERVER, IRC_PORT)
        await self.conn.connect(IRC_SERVER, IRC_PORT, self.nick)

    def _send_chunks(self, message: str) -> int:
        """Send message to IRC, chunking if needed. Returns chunk count.
        Raises on socket error."""
        max_chunk = 400
        encoded = message.encode("utf-8")
        if len(encoded) <= max_chunk:
            self.conn.privmsg(IRC_CHANNEL, message)
            return 1
        # Split on whitespace boundaries
        chunks_sent = 0
        words = message.split(" ")
        chunk = ""
        for word in words:
            test = (chunk + " " + word).strip()
            if len(test.encode("utf-8")) > max_chunk:
                if chunk:
                    self.conn.privmsg(IRC_CHANNEL, chunk)
                    chunks_sent += 1
                chunk = word
            else:
                chunk = test
        if chunk:
            self.conn.privmsg(IRC_CHANNEL, chunk)
            chunks_sent += 1
        return chunks_sent

    async def send(self, message: str) -> dict:
        """Send a message to IRC and verify delivery via echo.

        After writing to the socket, waits up to 2s for the message to
        appear in the relay's buffer (echoed back by the IRC server).
        If no echo arrives, warns that delivery is unconfirmed.
        """
        if not (self.conn and self.connected):
            return {"sent": False, "error": "not connected to IRC"}

        pre_index = self.state.next_index

        try:
            chunks_sent = self._send_chunks(message)
        except Exception as e:
            self.connected = False
            self.channel_joined = False
            return {"sent": False, "error": f"send failed (connection dead): {e}"}

        # Wait for echo: the relay connection sees our message and buffers it
        for _ in range(20):  # 2 seconds, checking every 0.1s
            await asyncio.sleep(0.1)
            recent = self.state.get_messages_since(pre_index)
            if any(m["nick"] == self.nick for m in recent):
                return {"sent": True, "chunks": chunks_sent, "confirmed": True}

        return {
            "sent": True,
            "chunks": chunks_sent,
            "confirmed": False,
            "warning": "no echo received — message may not have been delivered, connection may be dead",
        }

    def is_session_alive(self) -> bool:
        """Check if the Claude Code session is still alive via injector lockfile PID."""
        lockpath = os.path.join(INJECTOR_DIR, f"{self.session_id}.lock")
        try:
            with open(lockpath, "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # check if process exists
            return True
        except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
            return False

    def disconnect(self):
        if self.conn and self.connected:
            try:
                self.conn.quit("Leaving")
            except Exception:
                pass


class RelayState:
    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.messages: list[dict] = []
        self.next_index = 0
        self.pending_nudge: set[str] = set()

    def add_message(self, nick: str, message: str):
        entry = {
            "index": self.next_index,
            "nick": nick,
            "message": message,
            "timestamp": time.time(),
        }
        self.messages.append(entry)
        self.next_index += 1
        if len(self.messages) > MAX_MESSAGES:
            self.messages = self.messages[-MAX_MESSAGES:]

    def get_messages_since(self, since: int) -> list[dict]:
        return [m for m in self.messages if m["index"] >= since]

    def get_online_nicks(self) -> list[str]:
        return [s.nick for s in self.sessions.values() if s.session_id != "_relay"]

    def remove_session(self, session_id: str):
        session = self.sessions.get(session_id)
        if session:
            session.disconnect()
            del self.sessions[session_id]
        self.pending_nudge.discard(session_id)


state = RelayState()


# --- Injector Bridge ---


def nudge_session(session_id: str):
    """Write a nudge to the session's injector watchfile if available."""
    if session_id in state.pending_nudge:
        # Re-nudge if enough time has passed (watcher may have consumed the last nudge)
        session = state.sessions.get(session_id)
        if session and session.last_nudge_time and (time.time() - session.last_nudge_time < 30):
            return  # Too soon, don't spam
        state.pending_nudge.discard(session_id)

    # Only nudge if there are actually unread messages for this session
    session = state.sessions.get(session_id)
    if session and not state.get_messages_since(session.last_read):
        return

    watchfile = os.path.join(INJECTOR_DIR, session_id)
    lockfile = os.path.join(INJECTOR_DIR, f"{session_id}.lock")

    # Watchfile may not exist if watcher cleaned up; check lockfile as proof of session
    if not os.path.exists(watchfile):
        if os.path.exists(lockfile):
            # Recreate watchfile so nudge can be delivered when watcher restarts
            try:
                open(watchfile, "a").close()
            except OSError:
                return
        else:
            return

    try:
        if os.path.getsize(watchfile) == 0:
            with open(watchfile, "w") as f:
                f.write(
                    "[IRC] New messages in #loom. "
                    "Use get_irc_messages tool to read them.\n"
                )
            state.pending_nudge.add(session_id)
            if session:
                session.nudge_count += 1
                session.last_nudge_time = time.time()
            log.info("Nudged session %s", session_id[:8])
    except OSError:
        pass


# --- Unix Socket Server ---


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        data = await reader.readline()
        if not data:
            return

        request = json.loads(data.decode())
        cmd = request.get("cmd")
        response = {"ok": False, "error": "unknown command"}
        loop = asyncio.get_event_loop()

        if cmd == "ping":
            response = {"ok": True}

        elif cmd == "join":
            session_id = request["session_id"]
            nick = request["nick"]

            # Validate session ID: must have a matching lockfile in injector dir
            # (proves this is a real Claude Code session, not a random UUID)
            if session_id not in state.sessions:
                lockfile = os.path.join(INJECTOR_DIR, f"{session_id}.lock")
                if not os.path.exists(lockfile):
                    response = {
                        "ok": False,
                        "error": (
                            f"Invalid session_id: no lockfile found at {lockfile}. "
                            "Pass your real session_id from the stop hook output."
                        ),
                    }
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    return

            # If already joined, just update nick if needed
            if session_id in state.sessions:
                session = state.sessions[session_id]
                session.last_active = time.time()
            else:
                # Create a new IRC connection for this session
                session = Session(session_id, nick, state)
                state.sessions[session_id] = session
                try:
                    await session.connect(loop)
                    # Wait briefly for channel join
                    for _ in range(20):
                        if session.channel_joined:
                            break
                        await asyncio.sleep(0.1)
                except Exception as e:
                    del state.sessions[session_id]
                    response = {"ok": False, "error": f"IRC connect failed: {e}"}
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    return

            state.pending_nudge.discard(session_id)
            response = {
                "ok": True,
                "nick": session.nick,
                "channel": IRC_CHANNEL,
                "online": state.get_online_nicks(),
                "message_count": len(state.messages),
            }
            log.info("Session %s joined as %s", session_id[:8], nick)

        elif cmd == "send":
            session_id = request["session_id"]
            message = request["message"]
            session = state.sessions.get(session_id)
            if not session:
                response = {"ok": False, "error": "not joined"}
            else:
                session.last_active = time.time()
                result = await session.send(message)
                if result["sent"]:
                    response = {
                        "ok": True,
                        "nick": session.nick,
                        "chunks": result["chunks"],
                        "confirmed": result.get("confirmed", False),
                    }
                    if not result.get("confirmed"):
                        response["warning"] = result.get("warning", "delivery unconfirmed")
                        log.warning("[SEND] <%s> UNCONFIRMED: %s", session.nick, message[:100])
                    else:
                        log.info("[SEND] <%s> %s (%d chunk(s))", session.nick, message[:100], result["chunks"])
                else:
                    response = {"ok": False, "error": result.get("error", "not connected to IRC")}

        elif cmd == "get":
            session_id = request["session_id"]
            since = request.get("since")
            session = state.sessions.get(session_id)
            if not session:
                response = {"ok": False, "error": "not joined"}
            else:
                session.last_active = time.time()
                session.last_get_time = time.time()
                session.nudge_count = 0  # reset on successful get
                if since is None:
                    since = session.last_read
                messages = state.get_messages_since(since)
                if messages:
                    session.last_read = messages[-1]["index"] + 1
                state.pending_nudge.discard(session_id)
                response = {
                    "ok": True,
                    "messages": messages,
                    "next_index": state.next_index,
                }

        elif cmd == "nick":
            session_id = request["session_id"]
            new_nick = request["nick"]
            session = state.sessions.get(session_id)
            if not session:
                response = {"ok": False, "error": "not joined"}
            elif not session.conn or not session.connected:
                response = {"ok": False, "error": "not connected to IRC"}
            else:
                session.last_active = time.time()
                old_nick = session.nick
                session.nick = new_nick
                session.conn.nick(new_nick)
                response = {"ok": True, "old_nick": old_nick, "new_nick": new_nick}
                log.info("Session %s nick change: %s -> %s", session_id[:8], old_nick, new_nick)

        elif cmd == "status":
            now = time.time()
            sessions_info = []
            for sid, s in state.sessions.items():
                if sid == "_relay":
                    continue
                unread = len(state.get_messages_since(s.last_read))
                sessions_info.append({
                    "nick": s.nick,
                    "session_id": sid[:8],
                    "connected": s.connected,
                    "channel_joined": s.channel_joined,
                    "unread": unread,
                    "nudges_pending": s.nudge_count,
                    "last_active_ago": round(now - s.last_active),
                    "last_get_ago": round(now - s.last_get_time) if s.last_get_time else None,
                    "last_nudge_ago": round(now - s.last_nudge_time) if s.last_nudge_time else None,
                })
            response = {"ok": True, "sessions": sessions_info}

        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()
    except Exception as e:
        log.error("Socket handler error: %s", e)
        try:
            writer.write(json.dumps({"ok": False, "error": str(e)}).encode() + b"\n")
            await writer.drain()
        except Exception:
            pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_socket_server():
    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = await asyncio.start_unix_server(handle_client, SOCKET_PATH)
    log.info("Unix socket server listening at %s", SOCKET_PATH)
    return server


# --- Session cleanup ---


async def cleanup_loop():
    """Periodically remove stale or dead sessions."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for session_id in list(state.sessions):
            if session_id == "_relay":
                continue
            session = state.sessions[session_id]
            # Only reap sessions inactive for over 1 hour.
            # We do NOT check watcher PID liveness because watchers are one-shot
            # (exit after each nudge delivery) — a dead PID just means the watcher
            # finished, not that the Claude session is gone.
            if now - session.last_active > 3600:
                log.info("Removing stale session %s (%s) — inactive >1hr", session_id[:8], session.nick)
                state.remove_session(session_id)


# --- Auto-invite ---


INVITE_DIR = "/tmp/claude-chat/invites"


async def invite_loop():
    """Periodically scan for active Claude sessions and drop IRC invite files.

    The stop hook checks for invite files and blocks Claude with instructions
    to join IRC. This works even for idle sessions on their next turn.
    """
    invited: set[str] = set()
    os.makedirs(INVITE_DIR, exist_ok=True)
    while True:
        await asyncio.sleep(30)
        try:
            if not os.path.isdir(INJECTOR_DIR):
                continue
            for entry in os.listdir(INJECTOR_DIR):
                # Only look at lockfiles to find active sessions
                if not entry.endswith(".lock"):
                    continue
                session_id = entry[:-5]  # strip .lock
                # Skip if already joined or already invited
                if session_id in state.sessions or session_id in invited:
                    continue
                # Check if watcher PID is alive (lockfile contains PID as text)
                lockfile = os.path.join(INJECTOR_DIR, entry)
                try:
                    with open(lockfile, "r") as f:
                        pid = int(f.read().strip())
                    os.kill(pid, 0)  # check if process exists
                except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
                    continue  # No watcher running, skip
                # Active watcher found — invite via both channels:
                # 1. Invite file (picked up by stop hook on next turn)
                # 2. Watchfile (picked up by watcher immediately, wakes idle sessions)
                invite_msg = (
                    f"ACTION REQUIRED: Join IRC channel #loom now. "
                    f"Call join_irc with session_id=\"{session_id}\". "
                    f"Even if you joined before, the relay may have restarted — you MUST call join_irc again."
                )
                invite_file = os.path.join(INVITE_DIR, session_id)
                watchfile = os.path.join(INJECTOR_DIR, session_id)
                try:
                    with open(invite_file, "w") as f:
                        f.write(invite_msg)
                    # Also write to watchfile to wake idle sessions immediately
                    if os.path.exists(watchfile):
                        with open(watchfile, "w") as f:
                            f.write(invite_msg)
                    invited.add(session_id)
                    log.info("Invited session %s to join IRC (invite + watchfile)", session_id[:8])
                except OSError:
                    pass
        except Exception as e:
            log.error("Invite loop error: %s", e)


# --- Main ---


async def main():
    loop = asyncio.get_event_loop()

    # Start the relay's own IRC connection (for listening to messages)
    relay_session = Session("_relay", RELAY_NICK, state)
    state.sessions["_relay"] = relay_session
    await relay_session.connect(loop)

    # Start unix socket server
    await start_socket_server()

    # Start cleanup and invite loops
    asyncio.create_task(cleanup_loop())
    asyncio.create_task(invite_loop())

    log.info("Relay daemon running.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
