"""Private HTTP entry point with bounded concurrent work and typed responses."""
import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import re
from threading import BoundedSemaphore
from urllib.parse import parse_qs, urlsplit

from .clock import Conflict, InputError, Missing
from .desk.cache import PreviewCache
from .desk import routes as desk
from .media import MAX_ASSET
from .rules import canonical
from .service import Slatewatch

JSON_LIMIT = 64 * 1024
PREFIX = '/slatewatch/v1'
LOG = logging.getLogger(__name__)


class HttpError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class Server(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, address, root, slots=2):
        self.app = Slatewatch(root)
        self.preview_cache = PreviewCache()
        self.capacity = BoundedSemaphore(slots)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'Slatewatch/1'

    def setup(self):
        self.request.settimeout(20)
        super().setup()

    def reply(self, status, value, mime='application/json', headers=None):
        data = value if isinstance(value, bytes) else canonical(value)
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if headers is not None:
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            for name, content in headers.items():
                self.send_header(name, content)
        self.end_headers()
        self.wfile.write(data)

    def body(self, limit):
        if self.headers.get('Transfer-Encoding') is not None:
            raise HttpError(400, 'transfer encodings are unsupported')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0]):
            raise HttpError(411, 'one decimal Content-Length is required')
        length = int(lengths[0])
        if not 0 < length <= limit:
            raise HttpError(413, f'body must contain 1–{limit} bytes')
        data = self.rfile.read(length)
        if len(data) != length:
            raise HttpError(400, 'request body ended early')
        return data

    def json_body(self):
        if self.headers.get_content_type() != 'application/json':
            raise HttpError(415, 'expected application/json')
        try:
            value = json.loads(self.body(JSON_LIMIT))
        except (ValueError, RecursionError) as exc:
            raise HttpError(400, 'invalid JSON') from exc
        if not isinstance(value, dict):
            raise HttpError(400, 'expected JSON object')
        return value

    def route_request(self):
        parsed = urlsplit(self.path)
        if not parsed.path.startswith(PREFIX + '/'):
            raise Missing('route does not exist')
        suffix = parsed.path[len(PREFIX) + 1:]
        parts = suffix.split('/')
        app = self.server.app
        if self.command == 'GET' and not parsed.query:
            return self.get(app, parts)
        if self.command == 'POST':
            if parts == ['masters']:
                return self.upload(app, parsed.query)
            if not parsed.query:
                return self.post(app, parts)
        raise Missing('route does not exist')

    def upload(self, app, query):
        if self.headers.get_content_type() != 'application/octet-stream':
            raise HttpError(415, 'upload encoded media as application/octet-stream')
        params = parse_qs(query, keep_blank_values=True, max_num_fields=3)
        if any(key not in {'label', 'picture', 'sound'} or len(value) != 1 for key, value in params.items()):
            raise InputError('invalid intake parameters')
        label = params.get('label', [''])[0]
        picture = _stream_index(params['picture'][0]) if 'picture' in params else None
        sound_value = params.get('sound', ['first'])[0]
        if sound_value == 'none':
            sound = None
        elif sound_value == 'first':
            sound = 'first'
        else:
            sound = _stream_index(sound_value)
        return 201, app.master(self.body(MAX_ASSET), label, picture, sound), 'application/json'

    def get(self, app, parts):
        if parts == ['health']:
            return 200, {'status': 'ready'}, 'application/json'
        if len(parts) == 2:
            group, key = parts
            types = {'masters': 'master', 'policies': 'policy', 'scans': 'scan', 'seals': 'seal'}
            if group in types:
                return 200, app.vault.get(types[group], key), 'application/json'
        if len(parts) == 3:
            group, key, action = parts
            if group == 'scans' and action == 'review':
                return 200, app.review(key), 'application/json'
            if group == 'seals' and action == 'verify':
                return 200, app.verify(key), 'application/json'
            if group == 'seals' and action == 'bundle':
                return 200, app.download(key), 'application/zip'
        raise Missing('route does not exist')

    def post(self, app, parts):
        if len(parts) == 1:
            operations = {'policies': (app.policy, 201), 'scans': (app.scan, 201), 'compare': (app.compare, 200)}
            if parts[0] in operations:
                operation, code = operations[parts[0]]
                return code, operation(self.json_body()), 'application/json'
        if len(parts) == 3 and parts[0] == 'scans':
            _, key, action = parts
            operations = {'intents': app.add_intent, 'rulings': app.add_ruling, 'seals': app.seal}
            if action in operations:
                return 201, operations[action](key, self.json_body()), 'application/json'
            if action == 'evidence':
                return 200, app.evidence(key, self.json_body()), 'application/zip'
            if action == 'regions':
                return 200, app.region(key, self.json_body()), 'application/zip'
        raise Missing('route does not exist')

    def run_request(self):
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self.reply(400, {'error': 'invalid request target'})
            return
        quick_read = parsed.path.startswith('/desk/static/') or (
            parsed.path.startswith(('/desk/previews/', '/desk/regions/')) and parsed.path.count('/') == 4
        )
        if self.command == 'GET' and quick_read:
            try:
                self.reply(*desk.handle(self, parsed))
            except (ConnectionError, TimeoutError):
                pass
            return
        if not self.server.capacity.acquire(blocking=False):
            self.reply(HTTPStatus.SERVICE_UNAVAILABLE, {'error': 'all inspection slots are occupied'})
            return
        try:
            try:
                if parsed.path in ('/', '/desk') or parsed.path.startswith('/desk/'):
                    self.reply(*desk.handle(self, parsed))
                    return
                status, body, mime = self.route_request()
            except HttpError as exc:
                status, body, mime = exc.status, {'error': str(exc)}, 'application/json'
            except Missing as exc:
                status, body, mime = 404, {'error': str(exc)}, 'application/json'
            except Conflict as exc:
                status, body, mime = 409, {'error': str(exc)}, 'application/json'
            except (InputError, ValueError) as exc:
                status, body, mime = 400, {'error': str(exc)}, 'application/json'
            except TimeoutError:
                status, body, mime = 408, {'error': 'request timed out'}, 'application/json'
            except Exception:
                LOG.exception('inspection request failed')
                status, body, mime = 500, {'error': 'inspection request failed'}, 'application/json'
            self.reply(status, body, mime)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            self.server.capacity.release()

    do_GET = run_request
    do_POST = run_request


def _stream_index(value):
    if not re.fullmatch(r'[0-9]{1,4}', value):
        raise InputError('stream index must be a nonnegative integer')
    return int(value)


def main():
    parser = argparse.ArgumentParser(description='Serve the Slatewatch master inspection API')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8974)
    parser.add_argument('--root', default='slatewatch-vault')
    parser.add_argument('--slots', type=int, choices=(1, 2, 3, 4), default=2)
    args = parser.parse_args()
    with Server((args.host, args.port), args.root, args.slots) as server:
        print(f'Slatewatch listening on {server.server_address}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
