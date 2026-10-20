"""Bounded Codex task reviews over its local app-server API.

No transcript-file crawling, terminal input, hook-trust changes or agent turns.
Only visible, unambiguously matched main conversations are read. Message text
is held in memory for a title request, never persisted by this module.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import json
import logging
import os
from pathlib import Path
import re
import time

import titles

LOG = logging.getLogger('terminal-tldr')
POLL_SECONDS = 15
REVIEW_MESSAGES = 10
REVIEW_SECONDS = 3600


def normalized(value):
    return titles.native_title(value, allow_identity=True).replace('\ufe0f', '')


def bounded_context(text):
    bounded, remaining = [], 6000
    for message in reversed(text[-8:]):
        if remaining <= 0:
            break
        value = message['text'][-remaining:]
        bounded.append({**message, 'text': value})
        remaining -= len(value)
    return list(reversed(bounded))


class Claude:
    """Use Claude's exact foreground-PID registry, never search old sessions."""
    def __init__(self, home=None):
        self.home = home or Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude'))
        self.cached = {}
        self.bytes_read = 0

    def binding(self, target):
        pid = target['pid']
        if not isinstance(pid, int) or pid <= 0:
            return None
        record = titles.read_json(self.home / 'sessions' / f'{pid}.json')
        tid, cwd = record.get('sessionId', ''), record.get('cwd', '')
        if (record.get('pid') != pid or record.get('kind') != 'interactive'
            or not re.fullmatch(r'[0-9a-fA-F-]{36}', tid)
            or not isinstance(cwd, str) or not cwd.startswith('/')):
            return None
        path = self.home / 'projects' / re.sub(r'[^a-zA-Z0-9]', '-', cwd) / (tid + '.jsonl')
        if not path.is_file() or not path.resolve().is_relative_to((self.home / 'projects').resolve()):
            return None
        return {**target, 'thread': tid, 'transcript': path}

    def recent(self, binding):
        path, tid = binding['transcript'], binding['thread']
        stat = path.stat()
        signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if self.cached.get(tid, {}).get('signature') == signature:
            return self.cached[tid]['result']
        # Exactly one live file, at most 2 MiB. Never walk project/history dirs.
        with path.open('rb') as stream:
            stream.seek(max(0, stat.st_size - 2 * 1024 * 1024))
            raw = stream.read(2 * 1024 * 1024)
        self.bytes_read += len(raw)
        ids, messages = [], []
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (event.get('sessionId') != tid or event.get('isMeta')
                or event.get('isSidechain') or event.get('type') not in {'user', 'assistant'}):
                continue
            content = event.get('message', {}).get('content', [])
            text = content if isinstance(content, str) else ' '.join(
                part.get('text', '') for part in content
                if isinstance(part, dict) and part.get('type') == 'text')
            if not text.strip():
                continue
            if event['type'] == 'user' and event.get('uuid'):
                ids.append(event['uuid'])
            messages.append({'role': event['type'], 'text': text[-2000:]})
        result = (ids[-32:], bounded_context(messages))
        if len(self.cached) >= 32:
            self.cached.pop(next(iter(self.cached)))
        self.cached[tid] = dict(signature=signature, result=result)
        return result


class Codex:
    async def __aenter__(self):
        from websockets.asyncio.client import unix_connect
        home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        self.ws = await unix_connect(
            str(home / 'app-server-control/app-server-control.sock'),
            uri='ws://localhost', open_timeout=2, close_timeout=1,
            max_size=8 * 1024 * 1024)
        self.sequence = 0
        try:
            await self.call('initialize', {
                'clientInfo': {'name': 'terminal_tldr', 'version': '0.4.0'},
                'capabilities': {'experimentalApi': True}})
            await self.ws.send(json.dumps({'method': 'initialized'}))
        except BaseException:
            await self.ws.close()
            raise
        return self

    async def __aexit__(self, *_):
        await self.ws.close()

    async def call(self, method, params):
        # This reader deliberately has no methods that start, steer or interrupt
        # an agent, approve hooks, or change the user's conversation.
        if method not in {'initialize', 'thread/loaded/list', 'thread/read', 'thread/turns/list'}:
            raise ValueError('Unsupported read-only Codex operation')
        self.sequence += 1
        await self.ws.send(json.dumps(dict(id=self.sequence, method=method, params=params)))
        async with asyncio.timeout(4):
            while True:
                response = json.loads(await self.ws.recv())
                if response.get('id') != self.sequence:
                    continue
                if 'error' in response:
                    raise RuntimeError('Codex metadata request failed')
                return response['result']

    async def threads(self):
        result, cursor = [], None
        for _ in range(4):
            page = await self.call('thread/loaded/list', {'limit': 100, 'cursor': cursor})
            for tid in page['data']:
                thread = (await self.call('thread/read', {'threadId': tid, 'includeTurns': False}))['thread']
                if not thread.get('parentThreadId') and not thread.get('ephemeral'):
                    result.append({k: thread.get(k) for k in ('id', 'name', 'updatedAt')})
            cursor = page.get('nextCursor')
            if not cursor:
                break
        return result

    async def recent(self, tid):
        page = await self.call('thread/turns/list', {
            'threadId': tid, 'limit': 16, 'sortDirection': 'desc', 'itemsView': 'summary'})
        ids, text = [], []
        for turn in reversed(page.get('data', [])):
            for item in turn.get('items', []):
                if item.get('type') == 'userMessage':
                    ids.append(item.get('id') or turn['id'])
                    words = ' '.join(part.get('text', '') for part in item.get('content', [])
                                     if part.get('type') == 'text')
                    if words.strip():
                        text.append({'role': 'user', 'text': words[-2000:]})
                elif item.get('type') == 'agentMessage' and item.get('text'):
                    text.append({'role': 'assistant', 'text': item['text'][-1000:]})
        # Recent turns dominate. Paths, tool results and images aren't included.
        return ids[-32:], bounded_context(text)


class Cadence:
    def __init__(self, directory=None):
        self.directory = directory or titles.APP_DIR / 'conversation-reviews-v1'

    def get(self, thread):
        return titles.read_json(self.directory / (titles.key_for(thread) + '.json'))

    def save(self, thread, record):
        titles.atomic_json(self.directory / (titles.key_for(thread) + '.json'), record)

    def observe(self, thread, ids, now=None, acknowledged_at=0):
        now = time.time() if now is None else now
        record = self.get(thread)
        first = not record
        new = [item for item in ids if item not in record.get('seen', [])]
        total = record.get('total', 0) + len(new)
        record.update(total=total, seen=ids[-32:])
        if acknowledged_at > record.get('reviewedAt', 0):
            record.update(reviewedAt=acknowledged_at, reviewedTotal=total, attempts=0)
        due = bool(ids) and (
            (first and not record.get('reviewedAt'))
            or (bool(new) and (total - record.get('reviewedTotal', 0) >= REVIEW_MESSAGES
                              or now - record.get('reviewedAt', 0) >= REVIEW_SECONDS)))
        # Retry failed requests only following activity, not in an idle loop.
        due = due and now >= record.get('retryAt', 0)
        if new or first or acknowledged_at > self.get(thread).get('reviewedAt', 0):
            self.save(thread, record)
        return record, due

    def reviewed(self, thread, total, now=None):
        record = self.get(thread)
        record.update(reviewedTotal=total, reviewedAt=time.time() if now is None else now,
                      attempts=0, retryAt=0)
        self.save(thread, record)

    def failed(self, thread):
        record = self.get(thread)
        attempts = min(record.get('attempts', 0) + 1, 3)
        record.update(attempts=attempts, retryAt=time.time() + 300 * 2 ** (attempts - 1))
        self.save(thread, record)


def match_targets(targets, threads):
    """No focused-tab or shared-process environment fallback. Ambiguity loses."""
    by_name = {}
    for thread in threads:
        for key in {normalized(thread.get('name') or ''), thread['id']} - {''}:
            by_name.setdefault(key, []).append(thread)
    counts = Counter(target['native'] for target in targets)
    result = {}
    for target in targets:
        options = by_name.get(target['native'], [])
        if len(options) == 1 and counts[target['native']] == 1:
            result[target['sid']] = {**target, 'thread': options[0]['id']}
    return result


class AutomaticTitles:
    def __init__(self, service, cadence=None, client=Codex):
        self.service = service
        self.cadence = cadence or Cadence()
        self.client = client
        self.claude = Claude()
        self.bindings = {}
        self.connected = False
        self.wake = asyncio.Event()
        self.queue = asyncio.Queue()
        self.pending = {}
        self.forced = False
        self.refresh_running = False
        self.refresh_done = self.refresh_changed = self.refresh_failed = self.refresh_skipped = 0
        self.last_error = ''
        self.current = ''
        self.last_scan = 0
        self.scanning = False

    def resolve(self, thread):
        matches = [b for b in self.bindings.values() if b['thread'] == thread
                   and self.service.target_valid(b)]
        return matches[0]['sid'] if len(matches) == 1 else ''

    def request_refresh(self):
        if self.refresh_running:
            return dict(ok=True, message='Already reviewing your tabs.')
        self.refresh_running = self.forced = True
        self.refresh_done = self.refresh_changed = self.refresh_failed = self.refresh_skipped = 0
        self.last_error = ''
        self.wake.set()
        return dict(ok=True, message='Reviewing current tasks…')

    def status(self):
        return dict(automaticConnected=self.connected, automaticTabs=len(self.bindings),
                    automaticLastScan=self.last_scan, automaticError=self.last_error,
                    refreshRunning=self.refresh_running, refreshDone=self.refresh_done,
                    refreshChanged=self.refresh_changed,
                    refreshFailed=self.refresh_failed, refreshSkipped=self.refresh_skipped,
                    conversationQueued=len(self.pending), conversationInFlight=bool(self.current),
                    transcriptBytesRead=self.claude.bytes_read)

    def finish_refresh(self):
        if not self.scanning and not self.forced and not any(job.get('manual') for job in self.pending.values()):
            self.refresh_running = False

    async def run(self):
        while True:
            self.wake.clear()
            manual = self.forced
            self.forced = False
            self.scanning = True
            self.last_error = ''
            try:
                await self.scan(manual)
            except Exception as error:
                self.connected = False
                self.bindings = {}
                self.last_error = 'Cannot read current agent tasks. Check that your agents are running.'
                LOG.warning('Conversation connection unavailable: %s', type(error).__name__)
                if manual:
                    self.refresh_failed += 1
            finally:
                self.scanning = False
                self.finish_refresh()
            try:
                await asyncio.wait_for(self.wake.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def scan(self, manual=False):
        targets = self.service.conversation_targets()
        if not targets:
            self.bindings = {}
            self.connected = False
            return
        bindings, inputs = {}, {}
        codex_targets = [t for t in targets if t['provider'] == 'codex']
        if codex_targets:
            try:
                async with self.client() as codex:
                    threads = await codex.threads()
                    found = match_targets(codex_targets, threads)
                    for sid, binding in found.items():
                        inputs[sid] = await codex.recent(binding['thread'])
                    bindings.update(found)
            except Exception:
                self.last_error = 'Some Codex tasks could not be read.'
        for target in targets:
            if target['provider'] != 'claude':
                continue
            binding = self.claude.binding(target)
            if binding:
                try:
                    inputs[binding['sid']] = self.claude.recent(binding)
                    bindings[binding['sid']] = binding
                except (OSError, ValueError, TypeError):
                    self.last_error = 'A Claude task could not be read.'
        self.bindings = bindings
        self.connected = bool(bindings)
        self.last_scan = time.time()
        if manual:
            self.refresh_skipped = len(targets) - len(bindings)
        for binding in bindings.values():
            if not self.service.target_valid(binding):
                continue
            tid, sid = binding['thread'], binding['sid']
            ids, context = inputs[sid]
            prior = self.service.agents.get(sid, binding['owner'])
            acknowledged = prior.get('reviewedAt', 0) if prior.get('thread') == tid else 0
            record, due = self.cadence.observe(tid, ids, acknowledged_at=acknowledged)
            if not context:
                if manual:
                    self.refresh_skipped += 1
                continue
            if manual or due or (context and not record.get('reviewedAt') and not record.get('attempts')):
                job = {**binding, 'context': context, 'total': record['total'],
                       'manual': manual or self.pending.get(sid, {}).get('manual', False)}
                if sid not in self.pending:
                    self.queue.put_nowait(sid)
                    self.pending[sid] = job
                else:
                    self.pending[sid]['manual'] |= manual

    async def worker(self):
        while True:
            sid = await self.queue.get()
            job = self.pending.get(sid)
            try:
                if not job or not self.service.target_valid(job):
                    if job and job.get('manual'):
                        self.refresh_skipped += 1
                    continue
                self.current = sid
                current = self.service.agents.get(sid, job['owner']).get('title', {})
                async with self.service.model_lock:
                    self.service.stats['modelCalls'] += 1
                    self.service.stats['conversationReviews'] += 1
                    value = await titles.generate(job['native'], context=job['context'], current=current)
                # Recheck ownership and manual pins after the asynchronous request.
                if not self.service.target_valid(job):
                    if job.get('manual'):
                        self.refresh_skipped += 1
                    continue
                async with self.service.mutation_lock:
                    changed = self.service.tabs[job['tab']].override != titles.format_title(value)
                    self.service.agents.prompt(sid, job['owner'], job['thread'], f"review-{job['total']}")
                    result = self.service.agents.review(sid, job['owner'], job['thread'], value)
                    if not result['ok']:
                        raise RuntimeError('Title was not accepted')
                    self.service.stats['agentUpdates'] += 1
                    self.service.dirty.set()
                await self.service.reconcile()
                self.cadence.reviewed(job['thread'], job['total'])
                if job.get('manual'):
                    self.refresh_done += 1
                    self.refresh_changed += int(changed)
            except Exception as error:
                self.service.stats['modelFailures'] += 1
                if job:
                    self.cadence.failed(job['thread'])
                    if job.get('manual'):
                        self.refresh_failed += 1
                LOG.warning('Conversation title review failed: %s', type(error).__name__)
            finally:
                self.current = ''
                self.pending.pop(sid, None)
                self.queue.task_done()
                self.finish_refresh()
