#!/usr/bin/env python3
"""Live opt-in check using one owned iTerm fixture and one owned Codex thread.

Three tiny GPT-6 Luna acknowledgement turns provide real current-task context.
The nine-message counter is seeded only for that fixture to exercise the tenth
message boundary without manufacturing nine extra model calls. No existing
agent receives input, is resumed or is interrupted. Artifacts remain local.
"""
import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import agent_titles
import conversations
import titles
import iterm2
from websockets.asyncio.client import unix_connect


class FixtureRPC:
    async def __aenter__(self):
        self.ws = await unix_connect(
            str(Path.home() / '.codex/app-server-control/app-server-control.sock'),
            uri='ws://localhost', open_timeout=3, close_timeout=1, ping_interval=None,
            max_size=8 * 1024 * 1024)
        self.sequence = 0
        self.owned = None
        await self.call('initialize', {'clientInfo': {'name': 'terminal_tldr_live_test', 'version': '0.4.0'},
                                       'capabilities': {'experimentalApi': True}})
        await self.ws.send(json.dumps({'method': 'initialized'}))
        return self

    async def __aexit__(self, *_):
        try:
            if self.owned:
                from websockets.protocol import State
                if self.ws.state != State.OPEN:
                    owned = self.owned
                    await self.ws.close()
                    await self.__aenter__()
                    self.owned = owned
                await self.call('thread/archive', {'threadId': self.owned})
        finally:
            await self.ws.close()

    async def call(self, method, params):
        if method in {'turn/start', 'thread/name/set', 'thread/archive'}:
            assert self.owned and params['threadId'] == self.owned
        self.sequence += 1
        ident = self.sequence
        await self.ws.send(json.dumps(dict(id=ident, method=method, params=params)))
        async with asyncio.timeout(15):
            while True:
                response = json.loads(await self.ws.recv())
                if response.get('id') == ident:
                    if 'error' in response:
                        raise RuntimeError(response['error'])
                    return response['result']

    async def task(self, text):
        await self.call('turn/start', {'threadId': self.owned,
                                      'input': [{'type': 'text', 'text': text + ' Reply only Acknowledged. Do not use tools.'}]})
        for _ in range(100):
            result = await self.call('thread/read', {'threadId': self.owned, 'includeTurns': False})
            if result['thread']['status']['type'] == 'idle':
                return
            await asyncio.sleep(.3)
        raise RuntimeError('Owned verification turn did not finish')


async def snapshot(app):
    return {s.session_id: await s.async_get_variable('jobPid')
            for w in app.windows for t in w.tabs for s in t.all_sessions}


async def until_title(tab, predicate, timeout=65):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = await tab.async_get_variable('titleOverride') or ''
        if predicate(value):
            return value
        await asyncio.sleep(.5)
    raise AssertionError(f'Expected title not displayed; last title was {value!r}')


async def main(connection):
    app = await iterm2.async_get_app(connection)
    original = await snapshot(app)
    windows = {w.window_id for w in app.windows}
    token = uuid.uuid4().hex[:8]
    name = 'TLDR Verification ' + token
    cache = titles.CACHE_DIR / (titles.key_for(name) + '.json')
    assert not cache.exists()
    titles.atomic_json(cache, dict(emojis=['🧪', '⏳', '🏷️'], title='Previous Fixture Task'))
    fixture = None
    command_fd = None
    thread = None
    evidence = {'protectedSessionsBefore': original, 'checks': []}
    try:
        async with FixtureRPC() as rpc:
            result = await rpc.call('thread/start', {
                'model': 'gpt-6-luna', 'approvalPolicy': 'never', 'sandbox': 'read-only',
                'cwd': str(ROOT / '.local'), 'ephemeral': False,
                'developerInstructions': 'Isolated title-updater verification. Never use tools. Reply only Acknowledged.',
                'config': {'features.hooks': False, 'features.shell_tool': False,
                           'features.unified_exec': False, 'web_search': 'disabled'}})
            thread = rpc.owned = result['thread']['id']
            evidence['fixtureThread'] = thread
            await rpc.call('thread/name/set', {'threadId': thread, 'name': name})
            with tempfile.TemporaryDirectory(prefix='terminal-tldr-auto-fixture-') as directory:
                binary, fifo = Path(directory) / 'codex', Path(directory) / 'commands'
                os.mkfifo(fifo, 0o600)
                command_fd = os.open(fifo, os.O_RDWR)
                subprocess.run(['clang', '-Os', str(ROOT / 'tests/fixtures/native_title.c'), '-o', str(binary)], check=True)
                focused = app.current_window
                fixture = await iterm2.Window.async_create(connection, command=shlex.join([str(binary), str(fifo)]))
                assert fixture and fixture.window_id not in windows
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
                                    return "Minimized the owned verification window"
                                end if
                            end repeat
                        end repeat
                    end repeat
                    error "Owned fixture not found"
                end tell'''], check=True)
                os.write(command_fd, (name + ' | Working\n').encode())
                await rpc.task('The current task is Redis cache migration.')
                first = await until_title(tab, lambda text: 'redis' in text.lower() or 'cache' in text.lower())
                evidence['initialTitle'] = first
                evidence['checks'].append('Real current conversation context replaced the unrelated fixture title')
                print('Initial automatic title:', first, flush=True)

                cadence = conversations.Cadence()
                # The iTerm write precedes the worker's persisted completion.
                # Seed the boundary only after that completion, not while the
                # first review is still reconciling terminal state.
                for _ in range(100):
                    completed = cadence.get(thread)
                    owner = titles.read_json(agent_titles.AgentTitles().path(session.session_id))
                    if completed.get('reviewedAt', 0) > owner.get('reviewedAt', 0):
                        break
                    await asyncio.sleep(.1)
                else:
                    raise AssertionError('Initial title review did not persist completion')
                record = cadence.get(thread)
                assert record.get('reviewedAt'), 'No completed automatic review'
                record['total'] = record['reviewedTotal'] + 9
                cadence.save(thread, record)
                evidence['seededCadence'] = record
                await rpc.task('Redis migration is finished. Switch to PostgreSQL query optimization.')
                metadata = await rpc.call('thread/read', {'threadId': thread, 'includeTurns': False})
                evidence['nativeNameAfterSecondTurn'] = metadata['thread'].get('name')
                print('Fixture identity still matched:', metadata['thread'].get('name') == name, flush=True)
                second = await until_title(tab, lambda text: text != first and
                                           any(word in text.lower() for word in ('postgres', 'query', 'sql')))
                evidence['automaticTitle'] = second
                evidence['checks'].append('Tenth-message boundary changed the displayed title automatically without a refresh or trusted hook')
                print('Automatic task change:', second, flush=True)

                await rpc.task('PostgreSQL optimization is finished. The new task is S3 backup recovery.')
                await asyncio.sleep(17)
                assert await tab.async_get_variable('titleOverride') == second, 'Early update before the configured cadence'
                subprocess.run(['open', '-a', '/Applications/Terminal TLDR.app'], check=True)
                await asyncio.sleep(1)
                subprocess.run(['osascript', '-e', '''tell application id "com.jacobsapps.TerminalTLDR" to activate
                    tell application "System Events"
                        tell process "TerminalTLDR"
                            set elements to entire contents of window "Terminal TLDR"
                            repeat with el in elements
                                if class of el is button then
                                    if value of attribute "AXIdentifier" of el is "refresh-titles" then
                                        repeat 20 times
                                            if enabled of el then exit repeat
                                            delay 0.25
                                        end repeat
                                        if not enabled of el then error "Refresh titles button disabled"
                                        click el
                                        return "Clicked Re-title windows"
                                    end if
                                end if
                            end repeat
                            error "Re-title button missing"
                        end tell
                    end tell'''], check=True)
                third = await until_title(tab, lambda text: text != second and
                                          any(word in text.lower() for word in ('backup', 'recovery', 's3')), timeout=120)
                evidence['manualTitle'] = third
                evidence['checks'].append('Actual Mac-app Re-title windows button immediately used the new task context')
                print('Mac-app manual refresh:', third, flush=True)
                after = await snapshot(app)
                assert all(after.get(sid) == pid for sid, pid in original.items()), 'An existing session changed identity'
                evidence['checks'].append('All existing terminal session IDs and foreground PIDs were preserved')
                evidence['protectedSessionsAfter'] = {sid: after[sid] for sid in original}
                evidence['fixtureThread'] = thread
    finally:
        if fixture:
            assert fixture.window_id not in windows
            assert all(s.session_id not in original for t in fixture.tabs for s in t.all_sessions)
            await fixture.async_close(force=True)
        if command_fd is not None:
            os.close(command_fd)
        cache.unlink(missing_ok=True)
        if thread:
            evidence['finalCadence'] = conversations.Cadence().get(thread)
            (titles.APP_DIR / 'conversation-reviews-v1' / (titles.key_for(thread) + '.json')).unlink(missing_ok=True)
        out = ROOT / '.local/Verification/live-conversation-refresh.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        titles.atomic_json(out, evidence)
        print('Local evidence:', out, flush=True)


if __name__ == '__main__':
    iterm2.run_until_complete(main)
