"""Local Codex review queue helper.

Reads queue depth and active transcript status read-only; mutations use the
desktop Codex CLI's native stdio app-server RPC.
"""
import asyncio
import contextlib
import json
from pathlib import Path
import sqlite3
import uuid


def read_db(name):
    uri = (Path.home() / '.codex' / name).as_uri() + '?mode=ro'
    return sqlite3.connect(uri, uri=True, timeout=0.2)


def snapshot(chats):
    """Read queue depths and active-turn state for chats; unreadable fields stay None."""
    out = {chat: {'queued': None, 'active': None, 'archived': False} for chat in chats}
    slots = ','.join('?' for _ in chats)
    try:
        with contextlib.closing(read_db('queue_1.sqlite')) as db:
            counts = dict(db.execute(f'SELECT thread_id,COUNT(*) FROM queued_items WHERE thread_id IN ({slots}) GROUP BY thread_id', chats))
        for chat in chats:
            out[chat]['queued'] = counts.get(chat, 0)
    except sqlite3.Error:
        pass
    try:
        with contextlib.closing(read_db('state_5.sqlite')) as db:
            rows = db.execute(f'SELECT id, rollout_path, archived FROM threads WHERE id IN ({slots})', chats).fetchall()
        for chat, filename, archived in rows:
            out[chat]['archived'] = bool(archived)
            if not archived and filename:
                out[chat]['active'] = active_turn(Path(filename))
    except sqlite3.Error:
        pass
    return out


def active_turn(path):
    # End markers can be many records back in a long turn. Bound this best-effort read.
    try:
        with path.open('rb') as f:
            size = f.seek(0, 2)
            start = max(0, size - 2 * 1024 * 1024)
            f.seek(start)
            lines = f.read().splitlines()
            for line in reversed(lines[1:] if start else lines):
                if b'"event_msg"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if row.get('type') != 'event_msg':
                    continue
                kind = row['payload'].get('type')
                if kind in ('task_complete', 'turn_aborted', 'task_aborted'):
                    return 0
                if kind == 'task_started':
                    return 1
                # Emitted only while executing a turn, after its own start marker.
                if kind in ('agent_reasoning', 'agent_message', 'exec_command_begin', 'exec_command_end'):
                    return 1
    except OSError:
        pass
    return None


def pending(chats):
    """Read pending plain-text queue entries from pooled reviewer threads."""
    result = []
    with contextlib.closing(read_db('queue_1.sqlite')) as db:
        for chat in chats:
            rows = db.execute('''SELECT id,payload_json FROM (
                    SELECT id,payload_json FROM queued_items WHERE thread_id=? ORDER BY queue_order LIMIT 32
                ) WHERE length(payload_json) <= 110000''', (chat,))
            for item, raw in rows:
                # Restrict to one plain text message produced by this bridge.
                content = json.loads(raw).get('UserInput', {}).get('content', [])
                if len(content) == 1 and content[0].get('type') == 'text':
                    result.append((chat, item, content[0].get('text', '')))
    return result


def current_transcript(chat):
    try:
        with contextlib.closing(read_db('state_5.sqlite')) as db:
            row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (chat,)).fetchone()
        if row and row[0]:
            path = Path(row[0]).resolve()
            if path.is_relative_to((Path.home() / '.codex/sessions').resolve()) and path.is_file():
                return path
    except (sqlite3.Error, OSError):
        pass
    return None


async def _native(cli, method, params):
    proc = await asyncio.create_subprocess_exec(cli, 'app-server', '--listen', 'stdio://',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=2 * 1024 * 1024)
    def write(obj):
        proc.stdin.write(json.dumps(obj, ensure_ascii=False).encode() + b'\n')
    async def rpc(i, name, body):
        write({'id': i, 'method': name, 'params': body})
        await proc.stdin.drain()
        while line := await proc.stdout.readline():
            response = json.loads(line)
            if response.get('id') == i:
                if 'error' in response:
                    raise RuntimeError('Codex rejected ' + name)
                return response['result']
        raise RuntimeError('Codex closed the local RPC connection')
    try:
        await rpc(1, 'initialize', {'clientInfo': {'name': 'agent-chat-bridge', 'version': '1'}, 'capabilities': {'experimentalApi': True}})
        write({'method': 'initialized'})
        return await rpc(2, method, params)
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()


def add_copy(cli, dest, mid, text):
    client_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'agent-chat-bridge/{mid}/{dest}'))
    params = {'threadId': dest, 'clientUserMessageId': client_id, 'input': [{'type': 'text', 'text': text, 'text_elements': []}]}
    result = asyncio.run(asyncio.wait_for(_native(cli, 'thread/queue/add', params), 3))
    item = result['queuedSubmission']
    if not item.get('id') or item.get('clientUserMessageId') != client_id:
        raise RuntimeError('Codex did not confirm the review copy')


def remove_original(cli, source, item):
    result = asyncio.run(asyncio.wait_for(_native(cli, 'thread/queue/delete', {'threadId': source, 'queuedSubmissionId': item}), 3))
    if not isinstance(result.get('deleted'), bool):
        raise RuntimeError('Codex did not confirm the queue removal')
    return result['deleted']
