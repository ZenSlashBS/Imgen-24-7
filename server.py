import os
from http.server import SimpleHTTPRequestHandler
import socketserver

PORT = int(os.environ.get('PORT', 8000))
Handler = SimpleHTTPRequestHandler

with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Serving at port {PORT}")
    httpd.serve_forever()
