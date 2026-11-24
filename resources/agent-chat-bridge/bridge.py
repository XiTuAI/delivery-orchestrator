#!/usr/bin/env python3
"""Local CLI bridge between existing Codex, Antigravity, and Claude Code sessions.

Provides idempotent background delivery, load-aware Codex review pool routing,
single-owner backlog handoff, sanitized receipts, and bounded tail reads without
UI automation or direct database writes.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
import review_queue

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'receipts'
SIDES = ('codex', 'antigravity', 'claude')
CLAUDE_PERMISSION_MODES = ('default', 'plan', 'acceptEdits', 'dontAsk', 'auto', 'bypassPermissions')
UUID = re.compile(r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
MAX_MESSAGE = 16384
MAX_RESPONSE = 32 * 1024 * 1024
PAGE_STEPS = 64
CONTEXT_WARN_TOKENS = 260_000
CLAUDE_RUN_TIMEOUT = 1800
SERVICE = '/exa.language_server_pb.LanguageServerService/'


class ResponseTooLarge(RuntimeError):
    pass


def read_exact(response, size):
    chunks = []; remaining = size
    while remaining:
        chunk = response.read(remaining)
        if not chunk:
            raise RuntimeError('Truncated local app response')
        chunks.append(chunk); remaining -= len(chunk)
    return b''.join(chunks)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Local app redirect refused')


def local_open(request, timeout):
    # Never forward the local app credential through a proxy or redirect.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    return opener.open(request, timeout=timeout)


def run(args, timeout=10):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True).stdout


def save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def config():
    """Load and validate config.json bindings (any configured subset of codex/antigravity/claude)."""
    cfg_path = ROOT / 'config.json'
    if not cfg_path.is_file():
        raise RuntimeError(f'Missing {cfg_path}; copy config.example.json to config.json and fill in your chat UUIDs')
    c = json.loads(cfg_path.read_text(encoding='utf-8'))
    if not isinstance(c, dict):
        raise ValueError('config.json must be a JSON object')
    configured = []
    for side in SIDES:
        if side in c and c[side] is not None:
            if not isinstance(c[side], str) or not UUID.fullmatch(c[side]):
                raise ValueError(f'Invalid bound chat ID for {side}')
            configured.append(side)
    if not configured:
        raise ValueError('Configure at least one chat UUID (codex, antigravity, or claude) in config.json')
    retired = c.get('retired_codex_review_chats', [])
    if not isinstance(retired, list) or any(not isinstance(chat, str) or not UUID.fullmatch(chat) for chat in retired):
        raise ValueError('Retired Codex review chats must be a list of UUIDs')
    if 'codex_review_chats' in c or 'codex' in configured:
        if 'codex' not in configured:
            raise ValueError('codex_review_chats requires a default codex chat UUID')
        reviewers = c.get('codex_review_chats', [c['codex']])
        if (not isinstance(reviewers, list) or c['codex'] not in reviewers
                or any(not isinstance(chat, str) or not UUID.fullmatch(chat) for chat in reviewers)
                or len(set(reviewers)) != len(reviewers) or set(reviewers).intersection(retired)):
            raise ValueError('Codex review chats must be distinct active UUIDs and include the default Codex chat')
    blocked_ag = c.get('blocked_antigravity_chats', [])
    if not isinstance(blocked_ag, list) or any(not isinstance(chat, str) or not UUID.fullmatch(chat) for chat in blocked_ag):
        raise ValueError('Blocked Antigravity chats must be a list of UUIDs')
    perm = c.get('claude_permission_mode')
    if perm is not None and perm not in CLAUDE_PERMISSION_MODES:
        raise ValueError(f'Invalid claude_permission_mode; expected one of {", ".join(CLAUDE_PERMISSION_MODES)}')
    claude_cli = c.get('claude_cli')
    if claude_cli is not None and (not isinstance(claude_cli, str) or not claude_cli.strip()):
        raise ValueError('claude_cli must be a non-empty path string')
    return c


def resolve_chat(c, side, chat=None):
    """Resolve target chat UUID, preferring explicit --chat over config.json default."""
    target = chat if chat is not None else c.get(side)
    if target is None:
        raise ValueError(f'No default {side} chat configured; pass --chat or set "{side}" in config.json')
    if not UUID.fullmatch(str(target)):
        raise ValueError('Target chat ID must be a UUID')
    return target


def resolve_sender_side(c, to, from_chat, from_side=None):
    """Determine reply-to side for from_chat; require explicit --from-side when >2 sides are active."""
    if from_chat is None:
        return None
    if from_side is not None:
        if from_side not in SIDES:
            raise ValueError('Invalid sender side')
        return from_side
    configured = [side for side in SIDES if isinstance(c.get(side), str) and UUID.fullmatch(c[side])]
    if len(configured) == 2 and to in configured:
        return configured[1] if configured[0] == to else configured[0]
    if to in ('codex', 'antigravity') and 'claude' not in configured:
        return 'codex' if to == 'antigravity' else 'antigravity'
    raise ValueError('Specify --from-side when more than two sides are configured')


class Antigravity:
    def __init__(self):
        # Only inspect sockets owned by the installed app's language server.
        processes = run(['ps', '-axo', 'pid=,comm='])
        ids = [line.strip().split(None, 1)[0] for line in processes.splitlines()
               if line.strip().endswith('/Antigravity.app/Contents/Resources/bin/language_server')]
        if not ids:
            raise RuntimeError('Antigravity is not running')
        sockets = run(['lsof', '-nP', '-a', '-p', ','.join(ids), '-iTCP', '-sTCP:LISTEN', '-Fn'])
        ports = sorted(set(re.findall(r'^n127\.0\.0\.1:(\d+)$', sockets, re.M)))
        matches = []
        for port in ports:
            base = 'http://127.0.0.1:' + port
            try:
                with local_open(base + '/', timeout=2) as r:
                    html = r.read(32768).decode('utf-8')
                m = re.search(r'window\.__APP_CONFIG__\s*=\s*(\{[^<]+?\});', html)
                app = json.loads(m[1]) if m else {}
                if app.get('productName') == 'antigravity' and app.get('csrfToken'):
                    matches.append((base, app))
            except (OSError, ValueError):
                continue
        if len(matches) != 1:
            raise RuntimeError('Expected exactly one running Antigravity backend; no UI fallback')
        self.base, app = matches[0]
        self.version = app.get('appVersion')
        self._csrf = app['csrfToken']  # Ephemeral, never persisted or printed.

    def call(self, method, payload):
        req = urllib.request.Request(self.base + SERVICE + method,
              data=json.dumps(payload).encode(), headers={
                  'Content-Type': 'application/json', 'Connect-Protocol-Version': '1',
                  'x-codeium-csrf-token': self._csrf})
        with local_open(req, timeout=15) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ResponseTooLarge('Single app response exceeds safety limit; no full-history fallback')
        self.bytes_read = getattr(self, 'bytes_read', 0) + len(raw)
        return json.loads(raw)

    def page(self, chat, start, end=None):
        bounds = {'startIndex': start}
        if end is not None:
            bounds['endIndexExclusive'] = end
        payload = {'conversationId': chat, 'subscriberId': str(uuid.uuid4()),
                   'initialStepsPageBounds': bounds,
                   'initialGeneratorMetadatasPageBounds': {'startIndex': -1},
                   'initialExecutorMetadatasPageBounds': {'startIndex': 0, 'endIndexExclusive': 0},
                   'trajectoryVerbosity': 2, 'disableRehydration': True}
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.base + SERVICE + 'StreamAgentStateUpdates',
              data=b'\x00' + len(data).to_bytes(4, 'big') + data, headers={
                  'Content-Type': 'application/connect+json', 'Connect-Protocol-Version': '1',
                  'x-codeium-csrf-token': self._csrf})
        # Read the initial bounded UI snapshot, then close the subscription immediately.
        with local_open(req, timeout=15) as response:
            for _ in range(8):
                header = read_exact(response, 5)
                size = int.from_bytes(header[1:], 'big')
                if size > MAX_RESPONSE:
                    raise ResponseTooLarge('Single page exceeds safety limit')
                raw = read_exact(response, size)
                self.bytes_read = getattr(self, 'bytes_read', 0) + size + 5
                if header[0] != 0:
                    raise RuntimeError('Unsupported or ended local app stream; no resend')
                update = json.loads(raw).get('update', {})
                if update.get('conversationId') != chat:
                    raise RuntimeError('Antigravity returned a different chat')
                if 'stepsUpdate' in update.get('mainTrajectoryUpdate', {}):
                    return update
        raise RuntimeError('No initial conversation page received')

    def read(self, chat, last=4, marker=None, require_config=False):
        selected = {}; end = None; width = PAGE_STEPS; meta_count = 0; pages = 0
        status = None; latest_user = None
        while True:
            start = -width if end is None else max(0, end - width)
            try:
                page = self.page(chat, start, end)
            except ResponseTooLarge:
                if width == 1:
                    raise ResponseTooLarge('One conversation step exceeds safety limit; no full-history fallback')
                width = max(1, width // 2)
                continue
            pages += 1
            main = page['mainTrajectoryUpdate']; part = main['stepsUpdate']
            indices = part.get('indices', []); steps = part.get('steps', [])
            total = part.get('totalLength', 0)
            expected_start = max(0, total - width) if end is None else start
            expected_end = total if end is None else min(end, total)
            if indices != list(range(expected_start, expected_end)) or len(steps) != len(indices):
                raise RuntimeError('Incomplete or shifted conversation page; retry read without resending')
            if status is None:
                status = page.get('status')
                meta_count = main.get('generatorMetadatasUpdate', {}).get('totalLength', 0)
            for index, step in zip(indices, steps):
                if 'userInput' in step:
                    v = step['userInput']
                    selected[index] = {'userInput': {k: v[k] for k in ('items', 'userResponse') if k in v}}
                    if latest_user is None or index > latest_user:
                        latest_user = index
                elif step.get('plannerResponse', {}).get('response'):
                    selected[index] = {'plannerResponse': {'response': step['plannerResponse']['response']}}
            ordered = sorted(selected)
            snapshot = {'status': status, '_stepIndices': ordered,
                        'trajectory': {'cascadeId': chat, 'steps': [selected[i] for i in ordered]}}
            msgs = ag_messages(snapshot)
            found = any(m['role'] == 'user' and marker in m['text'] for m in msgs) if marker else len(msgs) >= last
            if (found and (not require_config or latest_user is not None)) or expected_start == 0:
                break
            if marker:
                selected.clear()  # Older receipt scans retain only the current page.
            end = expected_start
        if require_config:
            if latest_user is None:
                raise RuntimeError('No user turn exists; refusing to invent a configuration')
            turns = self.call('GetTrajectoryTurns', {'cascadeId': chat,
                              'stepIndices': [latest_user], 'trajectoryVerbosity': 3}).get('turns', [])
            exact = [t for t in turns if t.get('startStepIndex', 0) == latest_user]
            if len(exact) != 1 or not exact[0].get('firstStep', {}).get('userInput', {}).get('userConfig'):
                raise RuntimeError('Cannot preserve latest user configuration; refusing to send')
            v = exact[0]['firstStep']['userInput']
            selected[latest_user]['userInput'].update({k: v[k] for k in ('userConfig', 'userAgentSpec') if k in v})
        if meta_count:
            metadata = self.call('GetCascadeTrajectoryGeneratorMetadata', {
                'cascadeId': chat, 'generatorMetadataOffset': max(0, meta_count - 4), 'includeMessages': False})
            snapshot['trajectory']['generatorMetadata'] = metadata.get('generatorMetadata', [])
        snapshot['_readStats'] = {'pages': pages, 'bytes': getattr(self, 'bytes_read', 0),
                                  'totalSteps': total, 'retainedMessages': len(selected)}
        return snapshot

    def send(self, chat, text, snapshot):
        inputs = [s['userInput'] for s in snapshot['trajectory'].get('steps', []) if 'userInput' in s]
        if not inputs or not inputs[-1].get('userConfig'):
            raise RuntimeError('Cannot preserve existing chat configuration; refusing to send')
        latest = inputs[-1]
        payload = {'cascadeId': chat, 'items': [{'text': text}], 'cascadeConfig': latest['userConfig']}
        if latest.get('userAgentSpec'):
            payload['customAgentSpec'] = latest['userAgentSpec']
        return self.call('SendUserCascadeMessage', payload)


def ag_messages(snapshot):
    out = []
    for local_index, step in enumerate(snapshot['trajectory'].get('steps', [])):
        index = snapshot.get('_stepIndices', range(len(snapshot['trajectory'].get('steps', []))))[local_index]
        if 'userInput' in step:
            v = step['userInput']
            text = v.get('userResponse') or '\n'.join(x.get('text', '') for x in v.get('items', []))
            out.append({'role': 'user', 'text': text, 'step': index})
        elif 'plannerResponse' in step:
            text = step['plannerResponse'].get('response')
            if text:
                out.append({'role': 'assistant', 'text': text, 'step': index})
    return out


def model_evidence(snapshot):
    rows = snapshot['trajectory'].get('generatorMetadata', [])
    for row in reversed(rows):
        m = row.get('chatModel', {})
        if m.get('responseModel'):
            return {'modelEnum': m.get('model'), 'responseModel': m['responseModel'],
                    'displayName': m.get('modelDisplayName'), 'executionId': row.get('executionId'),
                    'source': 'GetCascadeTrajectoryGeneratorMetadata.generatorMetadata.chatModel'}
    return {'source': 'GetCascadeTrajectoryGeneratorMetadata', 'responseModel': None}


def context_usage(snapshot):
    """Extract allowlisted prompt token count from latest generatorMetadata usage."""
    rows = snapshot['trajectory'].get('generatorMetadata', [])
    for row in reversed(rows):
        usage = row.get('chatModel', {}).get('usage') or {}
        raw_in = usage.get('inputTokens')
        raw_cache = usage.get('cacheReadTokens')
        if raw_in is None and raw_cache is None:
            continue
        try:
            prompt_tokens = max(0, int(raw_in or 0)) + max(0, int(raw_cache or 0))
        except (TypeError, ValueError):
            continue
        return {
            'promptTokens': prompt_tokens,
            'threshold': CONTEXT_WARN_TOKENS,
            'warning': prompt_tokens >= CONTEXT_WARN_TOKENS,
        }
    return None


def reverse_lines(path):
    # Read only the necessary tail, without copying or caching the transcript.
    with path.open('rb') as f:
        f.seek(0, os.SEEK_END); position = f.tell(); pending = b''
        while position:
            size = min(65536, position); position -= size; f.seek(position)
            parts = (f.read(size) + pending).split(b'\n')
            pending = parts[0]
            for line in reversed(parts[1:]):
                if line:
                    if len(line) > MAX_RESPONSE:
                        raise ResponseTooLarge('One transcript record exceeds safety limit')
                    yield line
            if len(pending) > MAX_RESPONSE:
                raise ResponseTooLarge('One transcript record exceeds safety limit')
        if pending:
            yield pending


def codex_messages(chat, last=4, marker=None):
    root = Path.home() / '.codex/sessions'
    active = review_queue.current_transcript(chat)
    paths = [active] if active else list(root.glob('**/*-' + chat + '.jsonl'))
    if not paths or not active and len(paths) != 1:
        raise RuntimeError('Expected a local Codex transcript')
    if marker:
        older = list(root.glob('**/*-' + chat + '_*.jsonl')) + list(root.glob('**/*-' + chat + '.jsonl'))
        paths.extend(p for p in older if p not in paths)
    for path in paths:
        result = transcript_messages(path, last, marker)
        if result or not marker:
            return result
    return []


def transcript_messages(path, last, marker):
    out = []
    for index, line in enumerate(reverse_lines(path)):
        try:
            d = json.loads(line)
        except (ValueError, UnicodeError):
            if index == 0:  # An active writer may not have finished the final record yet.
                continue
            raise RuntimeError('Invalid transcript record')
        p = d.get('payload', {})
        if d.get('type') != 'response_item' or p.get('type') != 'message':
            continue
        role = p.get('role')
        if role not in ['user', 'assistant'] or (role == 'assistant' and p.get('channel') not in [None, 'final']):
            continue
        text = '\n'.join(x.get('text', '') for x in p.get('content', []))
        if marker:
            if role == 'user' and marker in text:
                return [{'role': role, 'text': text}]
        else:
            out.append({'role': role, 'text': text})
            if len(out) >= last:
                break
    return list(reversed(out))


def resolve_claude_cli(c):
    cli = c.get('claude_cli') or shutil.which('claude')
    if not cli or not Path(cli).is_file():
        raise RuntimeError('Claude Code CLI not found; install claude or set "claude_cli" in config.json')
    return cli


def claude_transcript(chat):
    root = Path.home() / '.claude/projects'
    paths = [p for p in root.glob(f'*/{chat}.jsonl') if p.is_file()]
    if len(paths) != 1:
        raise RuntimeError('Expected a single local Claude Code transcript')
    return paths[0]


def _claude_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text' and b.get('text'))
    return ''


def claude_messages(chat, last=4, marker=None):
    path = claude_transcript(chat)
    out = []
    for index, line in enumerate(reverse_lines(path)):
        try:
            d = json.loads(line)
        except (ValueError, UnicodeError):
            if index == 0:
                continue
            raise RuntimeError('Invalid Claude transcript record')
        if d.get('isMeta') or d.get('type') not in ('user', 'assistant'):
            continue
        m = d.get('message')
        if not isinstance(m, dict):
            continue
        role = m.get('role')
        if role != d.get('type'):
            continue
        text = _claude_text(m.get('content'))
        if not text:
            continue
        if marker:
            if role == 'user' and marker in text:
                return [{'role': role, 'text': text}]
        else:
            out.append({'role': role, 'text': text})
            if len(out) >= last:
                break
    return list(reversed(out))


def claude_session_info(chat):
    """Read project cwd and latest assistant model from a Claude Code transcript."""
    path = claude_transcript(chat)
    cwd = None
    model = None
    for index, line in enumerate(reverse_lines(path)):
        try:
            d = json.loads(line)
        except (ValueError, UnicodeError):
            if index == 0:
                continue
            raise RuntimeError('Invalid Claude transcript record')
        if cwd is None and isinstance(d.get('cwd'), str) and d['cwd'].strip():
            cwd = d['cwd']
        if model is None and d.get('type') == 'assistant':
            m = d.get('message')
            if isinstance(m, dict) and isinstance(m.get('model'), str):
                model = m['model']
        if cwd and model:
            break
    if not cwd or not Path(cwd).is_dir():
        raise RuntimeError('Cannot determine existing project cwd for Claude session')
    return {'cwd': cwd, 'model': model}


def claude_running_in_ps(chat):
    try:
        out = run(['ps', '-axo', 'pid=,comm=,args='])
    except (OSError, subprocess.SubprocessError):
        return False
    own = {str(os.getpid()), str(os.getppid())}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or parts[0] in own:
            continue
        comm, args = Path(parts[1]).name, parts[2]
        if comm in ('claude', 'node') and 'claude' in args and chat in args and '_claude-run' not in args and 'bridge.py' not in args:
            return True
    return False


def claude_busy(chat):
    lock_path = STATE / f'claude-{chat}.lock'
    if lock_path.exists():
        fd = os.open(lock_path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
    return claude_running_in_ps(chat)


def dispatch_claude(cli, chat, cwd, text, permission_mode=None, lock_fd=None):
    # Pass prompt via an unlinked 0600 file so message text never appears in ps argv or on disk.
    cmd = [cli, '--resume', chat, '-p', '--output-format', 'json']
    if permission_mode:
        cmd.extend(['--permission-mode', permission_mode])
    with tempfile.TemporaryFile() as stdin_file:
        stdin_file.write(text.encode('utf-8'))
        stdin_file.flush()
        stdin_file.seek(0)
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '_claude-run', str(lock_fd), cwd, *cmd],
            stdin=stdin_file, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, pass_fds=(lock_fd,),
        )
    return proc.pid


def _claude_run(argv):
    """Hold per-session flock without leaking it into claude or its tool subprocesses."""
    lock_fd = int(argv[0])
    cwd = argv[1]
    cmd = argv[2:]
    os.set_inheritable(lock_fd, False)
    try:
        subprocess.run(cmd, cwd=cwd, stdin=sys.stdin.buffer,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=CLAUDE_RUN_TIMEOUT, check=False)
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        os.close(lock_fd)


def next_review_chat(reviewers):
    """Pick the least-loaded active reviewer under send_lock, round-robin on ties."""
    cursor = STATE / 'last-review-chat'
    previous = json.loads(cursor.read_text()) if cursor.exists() else None
    start = reviewers.index(previous) + 1 if previous in reviewers else 0
    loads = review_queue.snapshot(reviewers)
    candidates = []
    for candidate in reviewers[start:] + reviewers[:start]:
        try:
            if not loads[candidate]['archived'] and codex_messages(candidate, last=1):
                candidates.append(candidate)
        except (RuntimeError, OSError):
            continue
    if not candidates:
        raise RuntimeError('No configured Codex reviewer has a readable local transcript')
    keys = [key for key in ('queued', 'active') if all(loads[c][key] is not None for c in candidates)]
    return min(candidates, key=lambda c: sum(loads[c][key] for key in keys))


def review_header(message_id, author, author_side='antigravity'):
    return (f'[agent-bridge:{message_id}] [reply-to:{author_side}:{author}]\n'
            f'[review-bridge] Before starting, run `agent-chat-bridge review-start {message_id}` (proceed only if it returns `start`). '
            f'After replying, run `agent-chat-bridge review-finish {message_id}` to pull any queued review.')


def delivery_digest(side, chat, message, sender, sender_side=None):
    items = [side, chat, message] + ([sender] if sender else [])
    legacy_sender = 'codex' if side == 'antigravity' else 'antigravity'
    if sender and sender_side and sender_side != legacy_sender:
        items.append(sender_side)
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode()).hexdigest()


@contextmanager
def send_lock(timeout, error):
    STATE.mkdir(mode=0o700, exist_ok=True)
    with open(STATE / 'send.lock', 'a') as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(error)
                time.sleep(0.05)
        yield


def send(c, to, message, message_id, ag_factory=Antigravity, command=run, chat=None, from_chat=None, review=False,
         from_side=None, claude_dispatch=dispatch_claude):
    """Idempotently deliver a message to an existing session and record a sanitized receipt."""
    if to not in SIDES:
        raise ValueError('Target side must be codex, antigravity, or claude')
    if not UUID.fullmatch(message_id):
        raise ValueError('Message ID must be a UUID')
    target_chat = resolve_chat(c, to, chat)
    retired_target = to == 'codex' and target_chat in c.get('retired_codex_review_chats', [])
    if retired_target:
        target_chat = resolve_chat(c, 'codex')
    if from_chat is not None and not UUID.fullmatch(str(from_chat)):
        raise ValueError('Sender chat ID must be a UUID')
    sender_side = resolve_sender_side(c, to, from_chat, from_side)
    blocked_ag = set(c.get('blocked_antigravity_chats', []))
    if to == 'antigravity' and target_chat in blocked_ag:
        raise ValueError(f'Target Antigravity chat {target_chat} is blocked; refusing to communicate')
    if sender_side == 'antigravity' and from_chat in blocked_ag:
        raise ValueError(f'Sender Antigravity chat {from_chat} is blocked; refusing to communicate')
    if not message.strip() or len(message.encode()) > MAX_MESSAGE:
        raise ValueError('Message must contain 1..16384 UTF-8 bytes; hand off large reports by path')
    STATE.mkdir(mode=0o700, exist_ok=True)
    path = STATE / (message_id + '.json')
    marker = '[agent-bridge:' + message_id + ']'
    header = marker + (f' [reply-to:{sender_side}:{from_chat}]' if from_chat else '')
    reviewers = c.get('codex_review_chats', [c['codex']] if 'codex' in c else [])
    pooled = to == 'codex' and len(reviewers) > 1 and (chat is None or retired_target)
    with send_lock(3 if pooled else 0, f'Another bridge send is in progress; retry with the SAME ID {message_id}'):
        prior = json.loads(path.read_text()) if path.exists() else None
        if prior and prior.get('routing') == 'review_pool' and to == 'codex' and chat == prior.get('requestedChat'):
            target_chat = prior['chat']
        elif not prior and pooled:
            target_chat = next_review_chat(reviewers)
        digest = delivery_digest(to, target_chat, message, from_chat, sender_side)
        if prior:
            if prior['digest'] != digest:
                raise ValueError('Message ID already belongs to different content or destination')
            return prior  # Unknown outcomes never trigger automatic resends.
        conventional_cr = (re.search(r'\b[0-9a-f]{40}\b', message) and
                           re.search(r'(?:请|申请|请求|please).{0,180}?(?:审查|复审|续审|终审|\bCR\b|review)', message[:600], re.I))
        transferable = pooled and from_chat is not None and (review or conventional_cr)
        text = (review_header(message_id, from_chat, sender_side) if transferable else header) + '\n' + message
        # 1. Preflight before marking this ID as dispatched: refuse nonexistent/empty/busy chats.
        ag = ag_factory() if to == 'antigravity' else None
        snapshot = ag.read(target_chat, require_config=True) if ag else None
        if ag and any(marker in m['text'] for m in ag_messages(snapshot)):
            raise RuntimeError('Marker already exists remotely; use receipt instead of resending')
        claude_cli = None
        claude_info = None
        claude_lock_fd = None
        try:
            if to == 'codex':
                if not c.get('codex_cli') or not Path(c['codex_cli']).is_file():
                    raise RuntimeError('Desktop Codex runtime CLI not found')
                if not pooled and chat is not None and target_chat != c.get('codex') and not codex_messages(target_chat, last=1):
                    raise RuntimeError('Target Codex chat has no existing local transcript; refusing to invent a chat')
            elif to == 'claude':
                claude_cli = resolve_claude_cli(c)
                claude_info = claude_session_info(target_chat)
                if claude_running_in_ps(target_chat):
                    raise RuntimeError(f'Claude session {target_chat} appears active in another process; refusing concurrent --resume')
                claude_lock_fd = os.open(STATE / f'claude-{target_chat}.lock', os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(claude_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError(f'Claude session {target_chat} is already running a bridge turn; retry with the SAME ID {message_id}')
            receipt = {'id': message_id, 'to': to, 'chat': target_chat, 'digest': digest,
                       'status': 'dispatching', 'createdAt': time.time()}
            if chat is not None and chat != target_chat:
                receipt['requestedChat'] = chat
            if from_chat:
                receipt['replyTo'] = {'side': sender_side, 'chat': from_chat}
            if pooled:
                receipt['routing'] = 'review_pool'
                save(STATE / 'last-review-chat', target_chat)
            if transferable:
                receipt['reviewOwner'] = target_chat
            save(path, receipt)
            try:
                if ag:
                    ag.send(target_chat, text, snapshot)
                    receipt['status'] = 'accepted'
                    # 2. Confirm independently in the server's user-message history.
                    after = ag.read(target_chat)
                    matches = [m for m in ag_messages(after) if m['role'] == 'user' and marker in m['text']]
                    if matches:
                        receipt.update(status='confirmed', step=matches[-1]['step'])
                    receipt['modelEvidence'] = model_evidence(after)
                    ctx = context_usage(after)
                    if ctx and ctx['warning']:
                        receipt['contextWarning'] = {
                            'promptTokens': ctx['promptTokens'],
                            'threshold': ctx['threshold'],
                            'hint': 'Target Antigravity chat context is large; consider /compact or rotating to a fresh session to avoid costly cache misses.',
                        }
                elif to == 'codex':
                    output = command([c['codex_cli'], 'queue', '--thread', target_chat, '--message', text, '--resume'], timeout=25)
                    receipt.update(status='queued', resumeWarning='warning' in output.lower())
                else:
                    pid = claude_dispatch(claude_cli, target_chat, claude_info['cwd'], text,
                                          permission_mode=c.get('claude_permission_mode'), lock_fd=claude_lock_fd)
                    receipt.update(status='dispatched', pid=pid)
            except Exception as error:
                receipt['status'] = 'unknown'
                receipt['errorType'] = type(error).__name__
                # Never persist upstream bodies, prompts, config or credentials.
            save(path, receipt)
            return receipt
        finally:
            if claude_lock_fd is not None:
                os.close(claude_lock_fd)


def check_receipt(c, message_id):
    if not UUID.fullmatch(message_id):
        raise ValueError('Message ID must be a UUID')
    STATE.mkdir(mode=0o700, exist_ok=True)
    with open(STATE / 'send.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Bridge receipt update in progress; retry later')
        path = STATE / (message_id + '.json')
        if not path.is_file():
            raise RuntimeError(f'Receipt not found for {message_id}')
        r = json.loads(path.read_text())
        marker = '[agent-bridge:' + message_id + ']'
        recipient = r.get('reviewOwner', r['chat'])
        if r['to'] == 'antigravity':
            msgs = ag_messages(Antigravity().read(r['chat'], marker=marker))
        elif r['to'] == 'claude':
            msgs = claude_messages(r['chat'], marker=marker)
        else:
            msgs = codex_messages(recipient, marker=marker)
        found = [m for m in msgs if m['role'] == 'user' and marker in m['text']]
        if found:
            r['status'] = 'confirmed'
            if r.get('reviewOffer'):
                r['confirmedChat'] = recipient
            save(path, r)
        return r


def pull_review(c, dest):
    """Called under the send lock by an available reviewer. The native destination is durable before source removal."""
    pool = c['codex_review_chats']
    loads = review_queue.snapshot(pool)
    if loads[dest]['archived'] or loads[dest]['queued'] != 0:
        return {'action': 'local_queue_or_unknown'}
    peers = [chat for chat in pool if chat != dest and not loads[chat]['archived']
             and loads[chat]['queued'] and loads[chat]['queued'] + (loads[chat]['active'] or 0) > 1]
    for source, item, text in review_queue.pending(peers) if peers else ():
        match = re.match(r'^\[agent-bridge:([a-f0-9-]{36})\]', text)
        if not match or not UUID.fullmatch(match[1]):
            continue
        mid = match[1]
        path = STATE / (mid + '.json')
        if not path.exists():
            continue
        r = json.loads(path.read_text())
        reply_to = r.get('replyTo', {})
        author = reply_to.get('chat')
        author_side = reply_to.get('side', 'antigravity')
        offer = r.get('reviewOffer', {})
        if (r.get('to') != 'codex' or r.get('routing') != 'review_pool' or not author
                or (author_side == 'antigravity' and author in c.get('blocked_antigravity_chats', []))
                or r.get('id') != mid or r.get('chat') != source or r.get('reviewOwner') != source
                or r.get('reviewStarted') or (offer and (offer['to'] != dest or 'sourceRemoved' in offer))
                or r.get('status') not in ('queued', 'confirmed')):
            continue
        prefix = review_header(mid, author, author_side) + '\n'
        if not text.startswith(prefix) or delivery_digest('codex', source, text[len(prefix):], author, author_side) != r['digest']:
            continue
        # Indeterminate add/crash keeps the original as owner; copied messages must pass review-start.
        r['reviewOffer'] = {'to': dest, 'sourceItem': item}
        save(path, r)
        review_queue.add_copy(c['codex_cli'], dest, mid, text)
        previous_status = r['status']
        r['reviewOwner'] = dest
        r['status'] = 'queued'
        save(path, r)
        deleted = review_queue.remove_original(c['codex_cli'], source, item)
        if not deleted:
            # Source was already dequeued. The target copy will skip at review-start.
            r['reviewOwner'] = source
            r['status'] = previous_status
        r['reviewOffer']['sourceRemoved'] = deleted
        save(path, r)
        return {'action': 'pulled' if deleted else 'source_dequeued', 'id': mid, 'from': source, 'to': dest}
    return {'action': 'no_work'}


def review_action(c, action, message_id, chat):
    if chat not in c.get('codex_review_chats', []):
        raise ValueError('Run this command inside a configured Codex reviewer chat')
    with send_lock(5, 'Bridge busy; retry the same review operation'):
        if action == 'review-pull':
            return pull_review(c, chat)
        if not UUID.fullmatch(message_id):
            raise ValueError('Invalid review message ID')
        path = STATE / (message_id + '.json')
        r = json.loads(path.read_text())
        if r.get('reviewOwner') != chat or r.get('reviewFinished'):
            return {'action': 'skip', 'owner': r.get('reviewOwner')}
        if action == 'review-start':
            if r.get('reviewStarted'):
                return {'action': 'already_started', 'owner': chat}
            offer = r.get('reviewOffer', {})
            if offer.get('to') == chat and 'sourceRemoved' not in offer:
                # The destination queue and owner were committed before a crash or lost delete response.
                # The source also checks this owner, so a repeated/missing deletion cannot start two reviews.
                try:
                    offer['sourceRemoved'] = review_queue.remove_original(c['codex_cli'], r['chat'], offer['sourceItem'])
                except (TimeoutError, OSError, RuntimeError, ValueError, KeyError):
                    offer['cleanup'] = 'unconfirmed'
            r['reviewStarted'] = time.time()
            save(path, r)
            return {'action': 'start', 'owner': chat}
        if not r.get('reviewStarted'):
            raise ValueError('This review has not started')
        r['reviewFinished'] = time.time()
        save(path, r)
        return {'action': 'finished', 'next': pull_review(c, chat)}


def main():
    """Parse CLI subcommands and print JSON results."""
    if len(sys.argv) >= 5 and sys.argv[1] == '_claude-run':
        _claude_run(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    doc = sub.add_parser('doctor')
    doc.add_argument('--ag-chat', default=None)
    doc.add_argument('--codex-chat', default=None)
    doc.add_argument('--claude-chat', default=None)
    mdl = sub.add_parser('model'); mdl.add_argument('--chat', default=None)
    rd = sub.add_parser('read'); rd.add_argument('--side', choices=SIDES, required=True)
    rd.add_argument('--chat', default=None); rd.add_argument('--last', type=int, default=4)
    sd = sub.add_parser('send'); sd.add_argument('--to', choices=SIDES, required=True)
    sd.add_argument('--chat', default=None); sd.add_argument('--from-chat', default=None)
    sd.add_argument('--from-side', choices=SIDES, default=None)
    sd.add_argument('--review', action='store_true', help='Mark a pooled handoff as a transferable code review')
    body = sd.add_mutually_exclusive_group(required=True); body.add_argument('--message'); body.add_argument('--file', type=Path)
    sd.add_argument('--id', default=None)
    rc = sub.add_parser('receipt'); rc.add_argument('id')
    for name in ('review-start', 'review-finish', 'review-pull'):
        cmd = sub.add_parser(name)
        if name != 'review-pull':
            cmd.add_argument('id')
    args = parser.parse_args(); c = config()
    if args.action == 'doctor':
        result = {'uiAutomation': False}
        if args.ag_chat is not None or 'antigravity' in c:
            ag_chat = resolve_chat(c, 'antigravity', args.ag_chat)
            ag = Antigravity(); s = ag.read(ag_chat, last=1)
            result['antigravity'] = {'version': ag.version, 'chat': ag_chat, 'status': s.get('status'),
                                     'contextUsage': context_usage(s)}
            result['modelEvidence'] = model_evidence(s)
            result['readStats'] = s.get('_readStats')
        if args.codex_chat is not None or 'codex' in c:
            if not c.get('codex_cli') or not Path(c['codex_cli']).is_file():
                raise RuntimeError('Desktop Codex runtime CLI not found')
            cx_chat = resolve_chat(c, 'codex', args.codex_chat)
            help_text = run([c['codex_cli'], 'queue', '--help'])
            if args.codex_chat is not None and cx_chat != c.get('codex'):
                codex_messages(cx_chat, last=1)
            reviewers = c.get('codex_review_chats', [c['codex']] if 'codex' in c else [cx_chat])
            result['codex'] = {'chat': cx_chat, 'queueSupported': '--thread' in help_text,
                               'reviewChats': reviewers, 'reviewLoad': review_queue.snapshot(reviewers)}
        if args.claude_chat is not None or 'claude' in c:
            cl_chat = resolve_chat(c, 'claude', args.claude_chat)
            cli = resolve_claude_cli(c)
            info = claude_session_info(cl_chat)
            result['claude'] = {'chat': cl_chat, 'cli': cli, 'cwd': info['cwd'],
                                'model': info['model'], 'busy': claude_busy(cl_chat)}
    elif args.action == 'model':
        result = model_evidence(Antigravity().read(resolve_chat(c, 'antigravity', args.chat), last=1))
    elif args.action == 'read':
        if not 1 <= args.last <= 20:
            raise ValueError('last must be 1..20')
        target_chat = resolve_chat(c, args.side, args.chat)
        if args.side == 'antigravity':
            msgs = ag_messages(Antigravity().read(target_chat, last=args.last))
        elif args.side == 'claude':
            msgs = claude_messages(target_chat, last=args.last)
        else:
            msgs = codex_messages(target_chat, last=args.last)
        result = msgs[-args.last:]
    elif args.action == 'receipt':
        result = check_receipt(c, args.id)
    elif args.action.startswith('review-'):
        result = review_action(c, args.action, getattr(args, 'id', None), os.environ.get('CODEX_THREAD_ID'))
    else:
        text = args.file.read_text() if args.file else args.message
        result = send(c, args.to, text, args.id or str(uuid.uuid4()), chat=args.chat,
                      from_chat=args.from_chat, review=args.review, from_side=args.from_side)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(json.dumps({'error': type(e).__name__, 'message': str(e) if isinstance(e, (ValueError, RuntimeError)) else 'Operation failed; no automatic resend'}, ensure_ascii=False))
        sys.exit(1)
