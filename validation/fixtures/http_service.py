import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, required=True)
parser.add_argument('--child-file')
args = parser.parse_args()
if args.child_file:
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
    Path(args.child_file).write_text(str(child.pid))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ('poc-python:' + os.environ.get('POC_LABEL', 'default')).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

HTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
