"""Local, per-terminal title ownership and review cadence. No conversation text."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import socket
import json
import time

import titles

REVIEW_MESSAGES = 10
REVIEW_SECONDS = 3600
SOCKET_NAME = 'control.sock'


def terminal_id(environ=None):
    env = os.environ if environ is None else environ
    value = env.get('ITERM_SESSION_ID', '')
    # iTerm exports w0t0p0:UUID. Never fall back to the focused tab.
    value = value.rsplit(':', 1)[-1]
    return value if re.fullmatch(r'[A-Za-z0-9-]{1,100}', value) else ''


def request(payload, directory=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1.5)
        client.connect(str((directory or titles.APP_DIR) / SOCKET_NAME))
        client.sendall(json.dumps(payload).encode() + b'\n')
        with client.makefile('rb') as stream:
            raw = stream.readline(titles.MAX_FILE + 1)
        if len(raw) > titles.MAX_FILE:
            raise ValueError('Oversized service response')
        return json.loads(raw)


class AgentTitles:
    def __init__(self, directory=None):
        self.directory = directory or titles.APP_DIR / 'agents-v1'
        self.records = {}

    def path(self, sid):
        return self.directory / (titles.key_for(sid) + '.json')

    def get(self, sid, owner):
        if sid not in self.records:
            self.records[sid] = titles.read_json(self.path(sid))
        record = self.records[sid]
        return record if record.get('owner') == owner else {}

    def save(self, sid, record):
        # Persist first: callers must not receive a successful acknowledgement
        # when their review cannot survive an updater restart.
        titles.atomic_json(self.path(sid), record)
        self.records[sid] = record

    def forget(self, sid):
        self.records.pop(sid, None)
        self.path(sid).unlink(missing_ok=True)

    def prompt(self, sid, owner, thread, turn, now=None):
        now = time.time() if now is None else now
        record = self.get(sid, owner)
        initial = record.get('thread') != thread
        if initial:
            record = dict(owner=owner, thread=thread, prompts=0,
                          reviewedPrompts=0, reviewedAt=0, title={})
        else:
            record = dict(record)
        if turn != record.get('lastTurn'):
            record['prompts'] += 1
            record['lastTurn'] = turn
            self.save(sid, record)
        due = (not record['reviewedAt']
               or record['prompts'] - record['reviewedPrompts'] >= REVIEW_MESSAGES
               or now - record['reviewedAt'] >= REVIEW_SECONDS)
        return dict(ok=True, initial=initial, due=due,
                    title=titles.format_title(record.get('title', {})))

    def review(self, sid, owner, thread, value=None, now=None):
        record = self.get(sid, owner)
        if not record or record.get('thread') != thread:
            return dict(ok=False, error='This conversation does not own this terminal. Submit a new prompt with the title hook enabled.')
        if value is not None and not titles.format_title(value):
            return dict(ok=False, error='Supply three distinct emojis and a short title.')
        record = dict(record)
        if value is not None:
            # Never persist unrecognised fields supplied over the local socket.
            formatted = titles.format_title(value)
            prefix = ' '.join(value['emojis']) + ' '
            record['title'] = dict(emojis=value['emojis'], title=formatted[len(prefix):])
        record.update(reviewedAt=time.time() if now is None else now,
                      reviewedPrompts=record['prompts'])
        self.save(sid, record)
        return dict(ok=True, title=titles.format_title(record.get('title', {})))


def context(thread, executable, current='', due=True):
    command = shlex.join([str(executable), 'set', '--session', thread])
    review = shlex.join([str(executable), 'review', '--session', thread])
    return (
        'Terminal TLDR: only the primary agent may update its own terminal title. '
        'When the user changes the actual task, update it near the start of that turn. '
        'Keep it stable through coding, tests and PR work. Do not delegate title updates. '
        f'Use {command} --title "Short Task Name" --emojis "🔧" "🎯" "✨", choosing three relevant distinct emojis '
        'and at most three title words. This is a local operation with no extra model request. '
        + (f'Review is due now. Current managed title: {current or "(none)"}. '
           f'If it still fits, acknowledge without rewriting it using {review}. ' if due else '')
        + 'Manual user renames win. If the service is unavailable, continue the user task without retry loops.'
    )
