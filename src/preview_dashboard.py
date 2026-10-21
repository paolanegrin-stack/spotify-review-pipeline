"""Local static/API preview. Starts only when explicitly executed."""

import argparse
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

from api.index import handler
from .prepare import ROOT


class Preview(handler):
    def do_GET(self):
        if urlparse(self.path).path == '/':
            body = (ROOT/'web/index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    print(f'Dashboard preview: http://127.0.0.1:{args.port}')
    with ThreadingHTTPServer(('127.0.0.1',args.port),Preview) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
