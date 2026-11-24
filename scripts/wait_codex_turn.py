#!/usr/bin/env python3
"""Wait for a dispatched agent-chat-bridge message to finish its Codex turn.

Runs as a lightweight background task inside Antigravity. Reads Codex's local
SQLite metadata and rollout JSONL strictly read-only, tracks the exact
`[agent-bridge:<message_id>]` user turn until `task_complete` / `turn_aborted` /
`task_aborted`, then prints the final assistant reply as JSON and exits 0 so
Antigravity wakes up automatically without user intervention.
"""
import argparse
import contextlib
import json
from pathlib import Path
import sqlite3
import sys
import time

MAX_REPLY_CHARS = 12000


def find_rollout_path(home, codex_chat):
    db_path = home / '.codex/state_5.sqlite'
    if db_path.is_file():
        try:
            uri = db_path.as_uri() + '?mode=ro'
            with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as db:
                row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (codex_chat,)).fetchone()
            if row and row[0]:
                p = Path(row[0])
                if p.is_file():
                    return p
        except (sqlite3.Error, OSError):
            pass

    sessions_root = home / '.codex/sessions'
    if sessions_root.is_dir():
        matches = sorted(
            sessions_root.glob(f'**/*-{codex_chat}.jsonl'),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return matches[0]
    return None


def queue_depth(home, codex_chat):
    db_path = home / '.codex/queue_1.sqlite'
    if not db_path.is_file():
        return None
    try:
        uri = db_path.as_uri() + '?mode=ro'
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as db:
            row = db.execute('SELECT COUNT(*) FROM queued_items WHERE thread_id=?', (codex_chat,)).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return None


def extract_message_text(payload):
    content = payload.get('content', [])
    if not isinstance(content, list):
        return ''
    return '\n'.join(
        item.get('text', '')
        for item in content
        if isinstance(item, dict) and item.get('text')
    )


def scan_rollout_for_turn(path, marker):
    """Scan rollout JSONL for marker user turn and check if its turn finished.

    Returns dict: {
      'pickedUp': bool,
      'terminal': None | 'completed' | 'aborted',
      'terminalKind': None | str,
      'reply': str,
    }
    """
    marker_bytes = marker.encode('utf-8')
    try:
        raw = path.read_bytes()
    except OSError:
        return {'pickedUp': False, 'terminal': None, 'terminalKind': None, 'reply': ''}

    # Find the last occurrence of marker in case of retries or references, then
    # verify it is an actual user message turn.
    search_from = 0
    marker_line_idx = None
    lines = raw.splitlines()
    for idx, line in enumerate(lines):
        if marker_bytes not in line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if row.get('type') != 'response_item':
            continue
        pl = row.get('payload')
        if not isinstance(pl, dict) or pl.get('type') != 'message' or pl.get('role') != 'user':
            continue
        if marker in extract_message_text(pl):
            marker_line_idx = idx
            search_from = idx + 1

    if marker_line_idx is None:
        return {'pickedUp': False, 'terminal': None, 'terminalKind': None, 'reply': ''}

    latest_reply = ''
    for line in lines[search_from:]:
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            # Active writer may still be flushing the final line.
            continue
        rtype = row.get('type')
        pl = row.get('payload')
        if not isinstance(pl, dict):
            continue
        if rtype == 'response_item' and pl.get('type') == 'message':
            if pl.get('role') == 'assistant' and pl.get('channel') in (None, 'final'):
                text = extract_message_text(pl).strip()
                if text:
                    latest_reply = text
        elif rtype == 'event_msg':
            kind = pl.get('type')
            if kind == 'task_complete':
                return {
                    'pickedUp': True,
                    'terminal': 'completed',
                    'terminalKind': kind,
                    'reply': latest_reply,
                }
            if kind in ('turn_aborted', 'task_aborted'):
                return {
                    'pickedUp': True,
                    'terminal': 'aborted',
                    'terminalKind': kind,
                    'reply': latest_reply,
                }

    return {'pickedUp': True, 'terminal': None, 'terminalKind': None, 'reply': latest_reply}


def format_reply(text):
    if len(text) <= MAX_REPLY_CHARS:
        return text, False
    return text[:MAX_REPLY_CHARS] + '\n...[已截断，完整内容请读取交接记录或执行 agent-chat-bridge read]...', True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-chat', required=True, help='Target Codex thread UUID')
    parser.add_argument('--message-id', required=True, help='Bridge message UUID from send receipt')
    parser.add_argument('--handoff', default=None, help='Optional shared handoff file path for reference')
    parser.add_argument('--timeout', type=int, default=7200, help='Max seconds to wait before returning timeout; 0 disables timeout (default 7200 = 2h)')
    parser.add_argument('--poll', type=float, default=5.0, help='Polling interval in seconds (default 5.0)')
    parser.add_argument('--home', type=Path, default=Path.home(), help='Override home directory (for testing)')
    args = parser.parse_args()

    home = args.home.expanduser().resolve()
    marker = f'[agent-bridge:{args.message_id}]'
    started = time.monotonic()
    deadline = None if args.timeout <= 0 else started + args.timeout
    last_state = {'pickedUp': False, 'terminal': None, 'terminalKind': None, 'reply': ''}
    last_sig = None

    while True:
        rollout = find_rollout_path(home, args.codex_chat)
        if rollout is not None:
            try:
                st = rollout.stat()
                sig = (str(rollout), st.st_mtime_ns, st.st_size)
            except OSError:
                sig = None
            if sig is not None and sig != last_sig:
                last_sig = sig
                last_state = scan_rollout_for_turn(rollout, marker)
                if last_state['terminal'] is not None:
                    reply_text, truncated = format_reply(last_state['reply'])
                    out = {
                        'status': last_state['terminal'],
                        'terminalEvent': last_state['terminalKind'],
                        'codexChat': args.codex_chat,
                        'messageId': args.message_id,
                        'pickedUp': True,
                        'elapsedSeconds': round(time.monotonic() - started, 1),
                        'handoffPath': args.handoff,
                        'replyTruncated': truncated,
                        'reply': reply_text,
                    }
                    print(json.dumps(out, ensure_ascii=False, indent=2))
                    return

        if deadline is not None and time.monotonic() >= deadline:
            reply_text, truncated = format_reply(last_state['reply'])
            out = {
                'status': 'timeout',
                'codexChat': args.codex_chat,
                'messageId': args.message_id,
                'pickedUp': last_state['pickedUp'],
                'queued': queue_depth(home, args.codex_chat),
                'elapsedSeconds': round(time.monotonic() - started, 1),
                'handoffPath': args.handoff,
                'replyTruncated': truncated,
                'latestPartialReply': reply_text,
                'nextAction': 'Codex 仍在执行或排队中；请勿重发新消息，可再次启动 wait_codex_turn.py（沿用同一个 --message-id）继续等待。',
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return

        time.sleep(max(0.2, args.poll))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': type(exc).__name__, 'message': str(exc)}, ensure_ascii=False))
        sys.exit(1)
