#!/usr/bin/env python3
"""Manage the operator positions of one local Slatewatch review room.

Reads deploy/positions.json and runs one real ``python -m slatewatch.http``
process per position against the same vault. Positions are tracked by pidfile
under the configured run directory, so the configuration can be started,
inspected and stopped repeatedly.

Usage: python3 deploy/roomctl.py [--config PATH] start|stop|restart|status [desk-a|desk-b|all]
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

DEPLOY = Path(__file__).resolve().parent
REPO = DEPLOY.parent
HEALTH_PATH = '/slatewatch/v1/health'
PROCESSES = {}


class Position:
    def __init__(self, name, spec):
        self.name = name
        self.host = spec.get('host', '127.0.0.1')
        self.port = int(spec.get('port', 8974))
        self.slots = int(spec.get('slots', 2))
        self.origin = f'http://{self.host}:{self.port}'

    def pidfile(self, room):
        return room.run_dir / f'{self.name}.pid'

    def logfile(self, room):
        return room.run_dir / f'{self.name}.log'


class Room:
    def __init__(self, config_path):
        data = json.loads(Path(config_path).read_text(encoding='utf-8'))

        def resolve(value):
            path = Path(value)
            return path if path.is_absolute() else (REPO / path).resolve()

        self.root = resolve(data.get('room_root', 'slatewatch-vault'))
        self.run_dir = resolve(data.get('run_dir', 'deploy/run'))
        self.media_dir = resolve(data.get('media_dir', 'deploy/media'))
        self.positions = {name: Position(name, spec) for name, spec in data['positions'].items()}


def health(position, timeout=2.0):
    """Return the HTTP status of the position's health route, or None if unreachable."""
    try:
        with urlopen(position.origin + HEALTH_PATH, timeout=timeout) as response:
            return response.status
    except HTTPError as exc:
        return exc.code
    except URLError:
        return None


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    cmdline = Path(f'/proc/{pid}/cmdline')
    if cmdline.exists():
        return b'slatewatch.http' in cmdline.read_bytes()
    return True


def read_pid(pidfile):
    try:
        pid = int(pidfile.read_text(encoding='ascii').strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 and alive(pid) else None


def start(room, position):
    room.run_dir.mkdir(parents=True, exist_ok=True)
    room.root.mkdir(parents=True, exist_ok=True)
    pidfile, logfile = position.pidfile(room), position.logfile(room)
    pid = read_pid(pidfile)
    if pid:
        print(f'{position.name}: already running (pid {pid})', flush=True)
        return True
    pidfile.unlink(missing_ok=True)
    if health(position) is not None:
        print(f'{position.name}: {position.origin} already answers without a pidfile; refusing to start',
              flush=True)
        return False
    log = logfile.open('ab')
    process = subprocess.Popen(
        [sys.executable, '-m', 'slatewatch.http', '--root', str(room.root),
         '--host', position.host, '--port', str(position.port), '--slots', str(position.slots)],
        cwd=REPO, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True)
    log.close()
    PROCESSES[position.name] = process
    pidfile.write_text(f'{process.pid}\n', encoding='ascii')
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(f'{position.name}: exited during startup; see {logfile}', flush=True)
            return False
        if health(position) == 200:
            print(f'{position.name}: serving {position.origin} '
                  f'(pid {process.pid}, slots {position.slots}, vault {room.root})', flush=True)
            return True
        time.sleep(0.1)
    print(f'{position.name}: did not become ready; see {logfile}', flush=True)
    return False


def stop(room, position):
    pidfile = position.pidfile(room)
    pid = read_pid(pidfile)
    if not pid:
        pidfile.unlink(missing_ok=True)
        state = health(position)
        if state is not None:
            print(f'{position.name}: no pidfile but {position.origin} answers (HTTP {state}); leaving it alone',
                  flush=True)
            return False
        print(f'{position.name}: not running', flush=True)
        return True
    process = PROCESSES.pop(position.name, None)
    os.kill(pid, signal.SIGTERM)
    if process is not None:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    else:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and alive(pid):
            time.sleep(0.1)
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.2)
    pidfile.unlink(missing_ok=True)
    print(f'{position.name}: stopped (pid {pid})', flush=True)
    return True


def status(room, position):
    pid = read_pid(position.pidfile(room))
    state = health(position)
    ready = state == 200
    detail = f'pid {pid}' if pid else 'no pidfile'
    print(f'{position.name}: {"ready" if ready else "not ready"} ({detail}, health {state})', flush=True)
    return ready


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=DEPLOY / 'positions.json')
    parser.add_argument('action', choices=('start', 'stop', 'restart', 'status'))
    parser.add_argument('target', nargs='?', default='all')
    args = parser.parse_args()
    room = Room(args.config)
    names = list(room.positions) if args.target == 'all' else [args.target]
    unknown = [name for name in names if name not in room.positions]
    if unknown:
        print(f'unknown position(s): {", ".join(unknown)} (configured: {", ".join(room.positions)})')
        return 2
    ok = True
    for name in names:
        position = room.positions[name]
        if args.action in ('stop', 'restart'):
            ok = stop(room, position) and ok
        if args.action in ('start', 'restart'):
            ok = start(room, position) and ok
        if args.action == 'status':
            ok = status(room, position) and ok
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
