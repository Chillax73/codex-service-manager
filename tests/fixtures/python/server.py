"""A WSGI Python backend, launched via a package-style module command."""
import json
import os
from wsgiref.simple_server import make_server

def app(environ, start_response):
    body = json.dumps({'status':'ok','label':os.environ.get('CSM_LABEL'),'cwd':os.getcwd()}).encode()
    start_response('200 OK',[('Content-Type','application/json')])
    return [body]

if __name__ == '__main__':
    make_server('127.0.0.1',int(os.environ['CSM_PORT']),app).serve_forever()
