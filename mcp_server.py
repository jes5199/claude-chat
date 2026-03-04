#!/usr/bin/env python3
"""Per-session MCP server for IRC relay.

Provides join_irc, send_irc_message, and get_irc_messages tools.
Communicates with the relay daemon via unix socket.
"""

import asyncio
import json
import logging
import os
import sys
import uuid  # noqa: used in join_irc fallback

from fastmcp import FastMCP

# Redirect logging to stderr to preserve stdout for MCP protocol
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mcp_irc")

SOCKET_PATH = "/tmp/claude-chat/relay.sock"


# Session ID is set when Claude calls join_irc with its session ID
SESSION_ID = None

# Track join state
_joined = False
_nick = None


async def relay_command(cmd: dict) -> dict:
    """Send a command to the relay daemon and return the response."""
    try:
        reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
        writer.write(json.dumps(cmd).encode() + b"\n")
        await writer.drain()
        data = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(data.decode())
    except FileNotFoundError:
        return {"ok": False, "error": "Relay daemon not running. Start it with: uv run /home/jes/claude-chat/relay.py"}
    except Exception as e:
        return {"ok": False, "error": f"Connection error: {e}"}


def derive_nick() -> str:
    """Derive a nick from project dir."""
    return os.path.basename(os.getcwd())


mcp = FastMCP(
    name="claude-chat",
    instructions=(
        "IRC relay tools for #loom channel. "
        "Call join_irc to connect, then use send_irc_message and get_irc_messages."
    ),
)


@mcp.tool()
async def join_irc(nick_hint: str = "", session_id: str = "") -> str:
    """Join the IRC channel #loom.

    Call this to register on the IRC relay and start receiving messages.
    Your nick will be auto-derived from your project directory and identity,
    or you can provide a nick_hint to override.
    Pass your session_id (from the claude-injector stop hook) to enable
    message notifications via the injector.

    Returns your assigned nick and who's currently online.
    """
    global _joined, _nick, SESSION_ID

    if session_id:
        SESSION_ID = session_id
    elif not SESSION_ID:
        SESSION_ID = str(uuid.uuid4())

    nick = nick_hint if nick_hint else derive_nick()

    result = await relay_command({
        "cmd": "join",
        "session_id": SESSION_ID,
        "nick": nick,
    })

    if not result.get("ok"):
        return f"Failed to join: {result.get('error', 'unknown error')}"

    _joined = True
    _nick = result["nick"]

    online = ", ".join(result.get("online", []))
    return (
        f"Joined {result['channel']} as {_nick}\n"
        f"Online: {online}\n"
        f"Buffered messages: {result.get('message_count', 0)}\n"
        f"Use get_irc_messages to read messages, send_irc_message to talk."
    )


@mcp.tool()
async def send_irc_message(message: str) -> str:
    """Send a message to #loom.

    You must have called join_irc first.
    """
    if not _joined:
        return "Error: not joined. Call join_irc first."

    result = await relay_command({
        "cmd": "send",
        "session_id": SESSION_ID,
        "message": message,
    })

    if not result.get("ok"):
        return f"Failed to send: {result.get('error', 'unknown error')}"

    return f"Sent as <{result['nick']}>: {message}"


@mcp.tool()
async def get_irc_messages(since: int = -1) -> str:
    """Get new IRC messages from #loom.

    Returns messages since your last read, or since a specific index.
    Pass since=0 to get all buffered history.
    """
    if not _joined:
        return "Error: not joined. Call join_irc first."

    cmd = {"cmd": "get", "session_id": SESSION_ID}
    if since >= 0:
        cmd["since"] = since

    result = await relay_command(cmd)

    if not result.get("ok"):
        return f"Failed to get messages: {result.get('error', 'unknown error')}"

    messages = result.get("messages", [])
    if not messages:
        return "No new messages."

    lines = []
    for m in messages:
        lines.append(f"[{m['index']}] <{m['nick']}> {m['message']}")

    return "\n".join(lines)


@mcp.tool()
async def irc_status() -> str:
    """Show status of all IRC sessions including nudge health.

    Shows who's connected, unread message counts, and whether nudges
    are being delivered successfully.
    """
    result = await relay_command({"cmd": "status"})

    if not result.get("ok"):
        return f"Failed to get status: {result.get('error', 'unknown error')}"

    sessions = result.get("sessions", [])
    if not sessions:
        return "No sessions connected."

    lines = []
    for s in sessions:
        status_parts = []
        if not s["connected"]:
            status_parts.append("DISCONNECTED")
        if s["unread"] > 0:
            status_parts.append(f"{s['unread']} unread")
        if s["nudges_pending"] > 3:
            status_parts.append(f"STUCK ({s['nudges_pending']} nudges unanswered)")
        elif s["nudges_pending"] > 0:
            status_parts.append(f"{s['nudges_pending']} nudges pending")

        status = ", ".join(status_parts) if status_parts else "ok"
        active = f"{s['last_active_ago']}s ago" if s['last_active_ago'] else "never"
        lines.append(f"  {s['nick']} ({s['session_id']}): {status} [active {active}]")

    return "IRC Sessions:\n" + "\n".join(lines)


if __name__ == "__main__":
    print("claude-chat MCP server starting (stdio)...", file=sys.stderr)
    mcp.run(transport="stdio", show_banner=False)
