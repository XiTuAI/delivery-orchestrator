#!/usr/bin/env python3
"""Zero-dependency bootstrapper for delivery-orchestrator's bundled agent-chat-bridge.

Installs the bundled MIT-licensed agent-chat-bridge runtime into
~/.local/share/agent-chat-bridge and symlinks ~/.local/bin/agent-chat-bridge
if missing, detects the local macOS Desktop Codex CLI, and initializes a
machine-local config.json (chmod 600) without ever overwriting an existing
config.json or hardcoding any session UUIDs.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import sys

SKILL_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_DIR = SKILL_ROOT / 'resources/agent-chat-bridge'
UUID_RE = re.compile(r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')


def supports_codex_queue(cli_path):
    try:
        proc = subprocess.run(
            [str(cli_path), 'queue', '--help'],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0 and '--thread' in (proc.stdout + proc.stderr)
    except (OSError, subprocess.SubprocessError):
        return False


def detect_codex_cli(home, existing_cfg):
    candidates = []
    if isinstance(existing_cfg.get('codex_cli'), str) and existing_cfg['codex_cli'].strip():
        candidates.append(Path(existing_cfg['codex_cli'].strip()))

    codex_toml = home / '.codex/config.toml'
    if codex_toml.is_file():
        try:
            text = codex_toml.read_text(encoding='utf-8', errors='ignore')
            m = re.search(r'CODEX_CLI_PATH\s*=\s*"([^"]+)"', text)
            if m:
                candidates.append(Path(m.group(1)))
        except OSError:
            pass

    candidates.extend([
        Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'),
        Path('/Applications/Codex.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'),
        Path('/Applications/Codex.app/Contents/Resources/codex'),
        home / 'Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
        home / 'Applications/Codex.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
        home / 'Applications/Codex.app/Contents/Resources/codex',
    ])
    which_codex = shutil.which('codex')
    if which_codex:
        candidates.append(Path(which_codex))

    seen = set()
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if p.is_file() and os.access(p, os.X_OK) and supports_codex_queue(p):
            return str(p)
    return None


def find_local_codex_threads(home, limit=5):
    db_path = home / '.codex/state_5.sqlite'
    if not db_path.is_file():
        return []
    uri = db_path.as_uri() + '?mode=ro'
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as db:
            rows = db.execute(
                'SELECT id, cwd, title FROM threads WHERE archived=0 ORDER BY updated_at DESC LIMIT ?',
                (limit,),
            ).fetchall()
            return [
                {'id': r[0], 'cwd': r[1] or '', 'title': ' '.join((r[2] or '').split())[:60]}
                for r in rows
                if isinstance(r[0], str) and UUID_RE.fullmatch(r[0])
            ]
    except sqlite3.Error:
        return []


def find_local_ag_chats(home, limit=5):
    brain = home / '.gemini/antigravity/brain'
    if not brain.is_dir():
        return []
    dirs = [p for p in brain.iterdir() if p.is_dir() and UUID_RE.fullmatch(p.name)]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in dirs[:limit]]


def load_json(path):
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_secure_json(path, obj):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        os.chmod(tmp, 0o600)
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Read-only check without installing or writing files')
    parser.add_argument('--home', type=Path, default=Path.home(), help='Override home directory (for isolated testing)')
    args = parser.parse_args()

    home = args.home.expanduser().resolve()
    share_dir = home / '.local/share/agent-chat-bridge'
    bin_dir = home / '.local/bin'
    bin_link = bin_dir / 'agent-chat-bridge'
    cfg_path = share_dir / 'config.json'

    issues = []
    actions = []

    if platform.system() != 'Darwin':
        issues.append('agent-chat-bridge 目前仅支持 macOS（依赖本机 Desktop Codex CLI 与 Antigravity）。')
    if sys.version_info < (3, 11):
        issues.append(f'需要 Python 3.11+，当前版本为 {platform.python_version()}。')

    installed = bin_link.is_file() and (share_dir / 'bridge.py').is_file() and (share_dir / 'review_queue.py').is_file()

    if not installed and not args.check:
        missing_bundle = [name for name in ('bridge.py', 'review_queue.py', 'LICENSE') if not (BUNDLE_DIR / name).is_file()]
        if missing_bundle:
            issues.append(f'Skill 内置的 agent-chat-bridge 运行时文件缺失：{", ".join(missing_bundle)}')
        else:
            share_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            for name in ('bridge.py', 'review_queue.py', 'LICENSE'):
                dst = share_dir / name
                shutil.copy2(BUNDLE_DIR / name, dst)
            os.chmod(share_dir / 'bridge.py', 0o700)
            os.chmod(share_dir / 'review_queue.py', 0o600)
            os.chmod(share_dir / 'LICENSE', 0o644)
            bin_dir.mkdir(parents=True, exist_ok=True)
            if bin_link.exists() or bin_link.is_symlink():
                bin_link.unlink()
            bin_link.symlink_to(share_dir / 'bridge.py')
            installed = True
            actions.append(f'已将内置 MIT 版 agent-chat-bridge 安装到 {share_dir} 并建立软链 {bin_link}')

    existing_cfg = load_json(cfg_path)
    codex_cli = detect_codex_cli(home, existing_cfg)
    codex_threads = find_local_codex_threads(home)
    ag_chats = find_local_ag_chats(home)

    if not codex_cli:
        issues.append('未检测到支持 queue 的桌面版 Codex CLI（请先安装并登录 ChatGPT / Codex 桌面端）。')

    config_exists = cfg_path.is_file()
    if not config_exists and not args.check and installed:
        new_cfg = {}
        if codex_threads:
            new_cfg['codex'] = codex_threads[0]['id']
        if ag_chats:
            new_cfg['antigravity'] = ag_chats[0]
        if codex_cli:
            new_cfg['codex_cli'] = codex_cli

        if new_cfg.get('codex') or new_cfg.get('antigravity'):
            save_secure_json(cfg_path, new_cfg)
            config_exists = True
            actions.append(f'已生成本机配置文件 {cfg_path}（权限 600）；正式派单前仍需与用户确认具体目标 Codex 会话')
        else:
            issues.append('本机尚未发现任何 Codex 或 Antigravity 会话记录；请先分别打开并发送一句消息后再运行初始化。')

    if not codex_threads:
        issues.append('本机尚无未归档的 Codex 会话；请先在 ChatGPT/Codex 桌面端新建一个会话并发一句开场白，再派单。')

    path_entries = os.environ.get('PATH', '').split(':')
    in_path = str(bin_dir) in path_entries

    ready = bool(installed and config_exists and codex_cli and codex_threads and not issues)
    result = {
        'ready': ready,
        'installed': installed,
        'bridgeCli': str(bin_link),
        'inPath': in_path,
        'configPath': str(cfg_path),
        'configExists': config_exists,
        'codexCli': codex_cli,
        'codexThreadCount': len(codex_threads),
        'antigravityChatCount': len(ag_chats),
        'actions': actions,
        'issues': issues,
        'pathHint': None if in_path else f'当前 PATH 未包含 {bin_dir}；命令可直接使用 {bin_link}，或将 export PATH="$HOME/.local/bin:$PATH" 加入 ~/.zshrc',
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if ready or (args.check and installed and config_exists and codex_cli) else 1)


if __name__ == '__main__':
    main()
