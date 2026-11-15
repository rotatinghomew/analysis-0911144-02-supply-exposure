import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .domain import records, summarize
from .exposure import analyze
from .repository import Conflict


def make_server(repository, host='127.0.0.1', port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *arguments):
            return

        def send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                parsed = urlsplit(self.path)
                if parsed.path == '/health':
                    return self.send_json(200, {'status': 'ok'})
                if parsed.path == '/revisions':
                    return self.send_json(200, {'revisions': repository.history()})
                if parsed.path not in ('/records', '/summary'):
                    return self.send_json(404, {'error': '接口不存在'})
                query = parse_qs(parsed.query)
                revision = int(query['revision'][0]) if 'revision' in query else None
                snapshot = repository.get(revision)
                result = records(snapshot['data']) if parsed.path == '/records' else summarize(snapshot['data'])
                self.send_json(200, {'revision': snapshot['revision'], 'digest': snapshot['digest'], 'result': result})
            except LookupError as error:
                self.send_json(404, {'error': str(error)})
            except ValueError as error:
                self.send_json(400, {'error': str(error)})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ('/imports', '/exposure'):
                return self.send_json(404, {'error': '接口不存在'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4_000_000:
                    raise ValueError('请求正文大小不合法')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('请求必须是对象')
                if path == '/imports':
                    result = repository.import_data(payload['data'], payload['request_key'])
                    return self.send_json(200 if result['replayed'] else 201, result)
                revision = payload.get('revision')
                if revision is not None and type(revision) is not int:
                    raise ValueError('revision 必须为整数')
                snapshot = repository.get(revision)
                result = analyze(snapshot['data'], payload.get('scenarios'))
                self.send_json(200, {'revision': snapshot['revision'], 'digest': snapshot['digest'], 'result': result})
            except LookupError as error:
                self.send_json(404, {'error': str(error)})
            except Conflict as error:
                self.send_json(409, {'error': str(error)})
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                self.send_json(400, {'error': str(error)})
            except RuntimeError as error:
                self.send_json(500, {'error': str(error)})

    return ThreadingHTTPServer((host, port), Handler)
