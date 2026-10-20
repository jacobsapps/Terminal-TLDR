"""Explicit user-initiated installation. Never copy account files or credentials."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import titles

VERSION = '0.5.0'
LABEL = 'com.jacobbartlett.terminal-tldr-iterm'
MARKER = '# Terminal TLDR managed launcher'


def launcher(home=None):
    return (home or Path.home()) / '.local/bin/terminal-tldr'


def codex_binary():
    candidates = [os.environ.get('TERMINAL_TLDR_CODEX'), shutil.which('codex'),
                  str(Path.home() / '.local/bin/codex'),
                  '/opt/homebrew/bin/codex', '/usr/local/bin/codex',
                  str(Path.home() / '.volta/bin/codex'),
                  str(Path.home() / '.npm-global/bin/codex')]
    candidates += [str(p) for p in sorted(
        (Path.home() / '.nvm/versions/node').glob('*/bin/codex'),
        key=lambda p: p.stat().st_mtime, reverse=True)]
    for value in candidates:
        if value and Path(value).is_file() and os.access(value, os.X_OK):
            # Keep the installer's stable shim and its sibling node executable.
            return str(Path(value).absolute())
    return None


def service_status():
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(str(titles.APP_DIR / 'status.sock'))
            return json.loads(client.recv(titles.MAX_FILE))
    except (OSError, ValueError):
        return None


def hook_command(home=None):
    return shlex.join([str(launcher(home)), 'agent-hook'])


def hook_paths(home=None):
    home = home or Path.home()
    return [Path(os.environ.get('CODEX_HOME', home / '.codex')) / 'hooks.json',
            Path(os.environ.get('CLAUDE_CONFIG_DIR', home / '.claude')) / 'settings.json']


def edit_hooks(path, install, home=None):
    """Merge only our exact handler, preserving all unrelated settings/hooks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / '.terminal-tldr-install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = path.read_bytes() if path.exists() else b'{}'
        value = json.loads(original)
        if not isinstance(value, dict) or not isinstance(value.get('hooks', {}), dict):
            raise ValueError(f'Invalid hook configuration: {path}')
        hooks = value.setdefault('hooks', {})
        command = hook_command(home)
        for event in ('SessionStart', 'UserPromptSubmit'):
            groups = hooks.get(event, [])
            if not isinstance(groups, list):
                raise ValueError(f'Invalid {event} hooks: {path}')
            updated = []
            for group in groups:
                handlers = group.get('hooks', [])
                remaining = [h for h in handlers if h.get('command') != command]
                if remaining or not handlers:
                    updated.append({**group, 'hooks': remaining})
            if install:
                updated.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 3}]})
            if updated:
                hooks[event] = updated
            else:
                hooks.pop(event, None)
        if value == json.loads(original):
            return
        if path.exists():
            backup = titles.APP_DIR / 'config-backups' / (path.parent.name + '-' + path.name + '-' + uuid.uuid4().hex)
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup.write_bytes(original)
            backup.chmod(0o600)
        titles.atomic_json(path, value)


def hooks_installed():
    command = hook_command()
    result = {}
    for name, path in zip(('codex', 'claude'), hook_paths()):
        try:
            value = json.loads(path.read_text())
            result[name] = all(any(h.get('command') == command
                                  for group in value.get('hooks', {}).get(event, [])
                                  for h in group.get('hooks', []))
                               for event in ('SessionStart', 'UserPromptSubmit'))
        except (OSError, ValueError, AttributeError, TypeError):
            result[name] = False
    return result


def doctor():
    binary = codex_binary()
    auth = 'Codex not found'
    if binary:
        try:
            env = os.environ.copy()
            env['PATH'] = str(Path(binary).parent) + os.pathsep + env.get('PATH', '')
            result = subprocess.run([binary, 'login', 'status'], capture_output=True, text=True, timeout=5, env=env)
            output = (result.stdout + result.stderr).lower()
            auth = ('Signed in with ChatGPT' if result.returncode == 0 and 'chatgpt' in output
                    else 'Signed in with an API key' if result.returncode == 0 and 'api' in output
                    else 'Sign in to Codex')
        except (OSError, subprocess.TimeoutExpired):
            auth = 'Could not check Codex sign-in'
    plist = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    loaded = launchctl('print', f'gui/{os.getuid()}/{LABEL}', check=False).returncode == 0
    return dict(version=VERSION, service=service_status(), installed=plist.exists(), daemonLoaded=loaded,
                iterm=any((base / 'iTerm.app').exists() for base in (Path('/Applications'), Path.home() / 'Applications')),
                codex=bool(binary), authentication=auth, hooks=hooks_installed(),
                **titles.preferences(), models=titles.model_options(),
                reasoningOptions=[dict(id=key, name=name) for key, name in titles.EFFORTS.items()],
                runtime=str(titles.APP_DIR))


def launchctl(*args, check=True):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True, check=check)


def pause():
    target = f'gui/{os.getuid()}/{LABEL}'
    launchctl('disable', target)
    launchctl('bootout', target, check=False)
    # bootout can return while the old job is still being removed. Starting
    # immediately can mistake that dying job for the newly installed service.
    deadline = time.monotonic() + 10
    while launchctl('print', target, check=False).returncode == 0:
        if time.monotonic() >= deadline:
            raise RuntimeError('The previous helper is still stopping. Wait a moment, then Resume.')
        time.sleep(0.1)


def resume():
    domain = f'gui/{os.getuid()}'
    plist = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    if not plist.exists():
        raise RuntimeError('Install Terminal TLDR first.')
    launchctl('enable', f'{domain}/{LABEL}')
    if launchctl('print', f'{domain}/{LABEL}', check=False).returncode:
        launchctl('bootstrap', domain, str(plist))


def install(root, with_agents=True):
    titles.APP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    titles.APP_DIR.chmod(0o700)
    destination = titles.APP_DIR / 'runtime' / (VERSION + '-' + uuid.uuid4().hex[:8])
    destination.parent.mkdir(parents=True, exist_ok=True)
    if getattr(sys, 'frozen', False):
        shutil.copytree(Path(sys.executable).parent, destination)
        command = [str(destination / Path(sys.executable).name)]
    else:
        destination.mkdir()
        for folder in ('bin', 'lib', 'iterm2'):
            shutil.copytree(root / folder, destination / folder,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
        python = titles.APP_DIR / 'venv/bin/python'
        if not python.exists():
            subprocess.run([sys.executable, '-m', 'venv', str(python.parent.parent)], check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', 'iterm2==2.20'], check=True)
        command = [str(python), str(destination / 'bin/terminal-tldr')]
    shim = launcher()
    shim.parent.mkdir(parents=True, exist_ok=True)
    if shim.exists() and MARKER not in shim.read_text():
        raise RuntimeError(f'Refusing to replace an unrelated command: {shim}')
    with tempfile.NamedTemporaryFile(mode='w', dir=shim.parent, delete=False) as file:
        file.write('#!/bin/sh\n' + MARKER + '\nexec ' + shlex.join(command) + ' "$@"\n')
        temporary = Path(file.name)
    temporary.chmod(0o755)
    os.replace(temporary, shim)
    if with_agents:
        for path in hook_paths():
            edit_hooks(path, True)
    binary = codex_binary()
    path_parts = [str(Path(binary).parent)] if binary else []
    path_parts += ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin']
    settings = titles.preferences()
    env = {'PATH': os.pathsep.join(path_parts), 'TERMINAL_TLDR_MODEL': settings['model'],
           'TERMINAL_TLDR_REASONING': settings['reasoning'], 'TERMINAL_TLDR_HOME': str(titles.APP_DIR)}
    if binary:
        env['TERMINAL_TLDR_CODEX'] = binary
    if os.environ.get('CODEX_HOME'):
        env['CODEX_HOME'] = os.environ['CODEX_HOME']
    plist = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    plist.parent.mkdir(parents=True, exist_ok=True)
    contents = plistlib.dumps({
        'Label': LABEL, 'ProgramArguments': command + ['serve'],
        'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10,
        'EnvironmentVariables': env, 'Nice': 10,
        'StandardOutPath': str(titles.APP_DIR / 'service.log'),
        'StandardErrorPath': str(titles.APP_DIR / 'service.log'),
        'WorkingDirectory': str(destination),
    })
    pause()
    plist.write_bytes(contents)
    resume()
    return dict(ok=True, message='Installed. Enable the iTerm Python API for automatic titles. Optional agent-written updates require approving the Codex hooks with /hooks.')


def uninstall():
    pause()
    (Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')).unlink(missing_ok=True)
    for path in hook_paths():
        if path.exists():
            edit_hooks(path, False)
    shim = launcher()
    if shim.exists() and MARKER in shim.read_text():
        shim.unlink()
    return dict(ok=True, message='Background service and title hooks removed. Local caches and config backups were kept.')
