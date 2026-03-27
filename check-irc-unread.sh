#!/bin/bash
# Stop hook: check if there are unread IRC messages and notify the session.
# Queries the relay's status endpoint via unix socket.
INPUT=$(cat)
STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active')

# Don't recurse
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
    exit 0
fi

# Query relay for status
RESP=$(echo '{"cmd":"status"}' | socat - UNIX-CONNECT:/tmp/claude-chat/relay.sock 2>/dev/null)
if [ -z "$RESP" ]; then
    exit 0  # Relay not running, don't block
fi

# Check for unread messages across all sessions
# Parse the sessions array and look for unread > 0
UNREAD=$(echo "$RESP" | jq -r '.sessions[]? | select(.unread > 0) | "\(.nick): \(.unread) unread"' 2>/dev/null)

if [ -n "$UNREAD" ]; then
    REASON="IRC has unread messages — check get_irc_messages. ${UNREAD}"
    echo "{\"decision\":\"block\",\"reason\":\"${REASON}\"}"
fi

exit 0
