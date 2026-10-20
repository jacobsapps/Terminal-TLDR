#!/usr/bin/env python3
"""Opt-in live test. Opens/closes only one guarded disposable iTerm window.

Run with the iTerm Python venv while the title service is running. Real agent
windows are never sent input or closed. Fixture cache entries bypass the model;
real title generation is verified separately on actual agent tabs.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import titles
import agent_titles
import iterm2

OUT = titles.APP_DIR / 'performance' / time.strftime('%Y-%m-%d') / 'v3/live-behavior.json'


def status():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect(str(titles.APP_DIR / 'status.sock'))
        return json.loads(s.recv(8192))


async def eventually(read, expected, label, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        actual = await read()
        if expected(actual) if callable(expected) else actual == expected:
            return actual
        await asyncio.sleep(0.2)
    raise AssertionError(f'{label}: expected {expected!r}, got {actual!r}')


async def snapshot(app):
    return {s.session_id: await s.async_get_variable('jobPid')
            for w in app.windows for t in w.tabs for s in t.all_sessions}


async def main(connection):
    app = await iterm2.async_get_app(connection)
    original = await snapshot(app)
    protected_windows = {w.window_id for w in app.windows}
    fixture = None
    command_fd = None
    focused = app.current_window
    records = []
    token = uuid.uuid4().hex[:8]
    names = [f'TLDR Fixture {token} Alpha', f'TLDR Fixture {token} Beta']
    paths = [titles.CACHE_DIR / (titles.key_for(name) + '.json') for name in names]
    live_name = f'Verify event-driven terminal titles {token}'
    live_path = titles.CACHE_DIR / (titles.key_for(live_name) + '.json')
    assert not live_path.exists()
    values = [{'emojis': ['🧪', '🏷️', '✅'], 'title': label} for label in ('Fixture Alpha', 'Fixture Beta')]
    for path, value in zip(paths, values):
        assert not path.exists(), 'Never overwrite an existing cache entry'
        titles.atomic_json(path, value)
    result = {'fixtureCacheSeeded': True, 'protectedSessionsBefore': original, 'checks': records}
    try:
        with tempfile.TemporaryDirectory(prefix='tldr-fixture-') as directory:
            binary = Path(directory) / 'codex'
            fifo = Path(directory) / 'commands'
            os.mkfifo(fifo, 0o600)
            command_fd = os.open(fifo, os.O_RDWR)
            subprocess.run(['clang', '-Os', str(ROOT / 'tests/fixtures/native_title.c'), '-o', str(binary)], check=True)
            fixture = await iterm2.Window.async_create(connection, command=f'{shlex.quote(str(binary))} {shlex.quote(str(fifo))}')
            assert fixture and fixture.window_id not in protected_windows
            tab, session = fixture.current_tab, fixture.current_tab.current_session
            assert session.session_id not in original
            if focused:
                await focused.async_activate()
            subprocess.run(['osascript', '-e', f'''tell application "iTerm2"
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            if id of s is "{session.session_id}" then
                                set miniaturized of w to true
                                return "Minimized only the guarded fixture"
                            end if
                        end repeat
                    end repeat
                end repeat
                error "Fixture session not found; no window minimized"
            end tell'''], check=True)
            result['fixtureWindow'] = fixture.window_id
            result['fixtureSession'] = session.session_id
            await eventually(lambda: session.async_get_variable('jobName'), 'codex', 'fixture foreground job')
            await asyncio.sleep(4)
            before = status()
            os.write(command_fd, b'01a11195-26bb-7661-bdaa-83fc3e51e206 | Starting\n')
            await asyncio.sleep(3)
            assert status()['modelCalls'] == before['modelCalls'], 'UUID startup placeholder caused a model call'
            records.append('UUID startup placeholder ignored')

            async def send(name):
                assert session.session_id not in original
                os.write(command_fd, (name + '\n').encode())
            await send(names[0] + ' | Working')
            await eventually(lambda: tab.async_get_variable('titleOverride'), titles.format_title(values[0]), 'initial cached title')
            records.append('New native task name updates the actual iTerm title')
            baseline = status()
            for i in range(30):
                value = ('⠴ ' + names[0] + ' | Working' if i % 3 == 0 else
                         ('[ . ] Action Required | ' if i % 3 == 1 else '[ ! ] Action Required | ') + names[0])
                await send(value)
                await asyncio.sleep(0.04)
            await asyncio.sleep(3)
            after_noise = status()
            for key in ('modelCalls', 'titleWrites', 'cacheReads', 'cacheWrites'):
                assert after_noise[key] == baseline[key], (key, baseline, after_noise)
            records.append('30 spinner/action-required changes: zero model calls, title writes or cache I/O')
            await send(names[1] + ' | Ready')
            await eventually(lambda: tab.async_get_variable('titleOverride'), titles.format_title(values[1]), 'changed task')
            records.append('A genuinely different native task gets its own title')
            assert status()['modelCalls'] == before['modelCalls'], 'Cached fixture unexpectedly generated a title'
            await send(live_name + ' | Working')
            def generated(actual):
                expected = titles.format_title(titles.read_json(live_path))
                return bool(expected and actual == expected)
            actual = await eventually(lambda: tab.async_get_variable('titleOverride'), generated, 'real model-generated title', 110)
            assert status()['modelCalls'] == before['modelCalls'] + 1
            result['realGeneratedTitle'] = actual
            result['afterRealGeneration'] = status()
            records.append('Uncached task: one real model request, valid result displayed in iTerm')
            assert titles.read_json(live_path)['model'] == 'gpt-6-luna'
            thread = 'fixture-' + token
            def control(action, **extra):
                return agent_titles.request(dict(action=action, terminal=session.session_id, thread=thread, **extra))
            assert control('prompt', turn='0')['due']
            agent_value = {'emojis': ['🧭', '🏷️', '✅'], 'title': 'Changed Agent Task'}
            assert control('set', value=agent_value)['ok']
            await eventually(lambda: tab.async_get_variable('titleOverride'), titles.format_title(agent_value), 'agent-written title')
            await send(names[0] + ' | Working')
            await asyncio.sleep(3)
            assert await tab.async_get_variable('titleOverride') == titles.format_title(agent_value)
            records.append('Agent-written task change displays without a model request and wins over native-name changes')
            writes = status()['titleWrites']
            assert control('review')['ok']
            for n in range(1, 10):
                assert not control('prompt', turn=str(n))['due']
            assert control('prompt', turn='10')['due']
            assert control('review')['ok']
            await asyncio.sleep(2)
            assert status()['titleWrites'] == writes
            assert status()['modelCalls'] == before['modelCalls'] + 1
            records.append('Tenth new user message triggers a review; unchanged review writes no title and calls no model')
            subprocess.run(['launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/com.jacobbartlett.terminal-tldr-iterm'], check=True)
            await asyncio.sleep(6)
            assert await tab.async_get_variable('titleOverride') == titles.format_title(agent_value)
            assert status()['modelCalls'] == 0
            assert not control('prompt', turn='11')['due']
            records.append('Agent title and review cadence survive a helper restart')
            await tab.async_set_title('Manual Fixture Title')
            await asyncio.sleep(2)
            await send(names[0] + ' | Ready')
            await asyncio.sleep(3)
            assert await tab.async_get_variable('titleOverride') == 'Manual Fixture Title'
            assert control('set', value=agent_value).get('manual')
            records.append('Manual rename survives subsequent native task changes')
            assert status()['modelCalls'] == 0, 'Manual rename triggered additional generation'
            # Restart only the updater; verify the fixture and all original jobs persist.
            subprocess.run(['launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/com.jacobbartlett.terminal-tldr-iterm'], check=True)
            await asyncio.sleep(6)
            restarted = status()
            assert restarted['modelCalls'] == 0, restarted
            assert restarted['titleWrites'] == 0, restarted
            assert await tab.async_get_variable('titleOverride') == 'Manual Fixture Title'
            records.append('Updater restart: zero model calls/writes; manual title preserved')
            result['restartStatus'] = restarted
            current = await snapshot(app)
            result['protectedSessionsAfter'] = {sid: current.get(sid) for sid in original}
            assert all(current.get(sid) == pid for sid, pid in original.items()), 'A working session changed during verification; inspect before claiming preservation'
            records.append('All pre-existing session IDs and foreground PIDs unchanged')
    finally:
        if fixture:
            assert fixture.window_id not in protected_windows
            await fixture.async_close(force=True)
            result['fixtureClosed'] = True
        if command_fd is not None:
            os.close(command_fd)
        for path in paths:
            path.unlink(missing_ok=True)
        live_path.unlink(missing_ok=True)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))


iterm2.run_until_complete(main)
