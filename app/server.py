import os
import requests
from flask import Flask, request, Response, send_file

app = Flask(__name__)

@app.route('/')
def index():
    return send_file('/app/index.html')

@app.route('/config.js')
def config():
    pages = os.environ.get('PAGES', 'https://example.com')
    interval = os.environ.get('INTERVAL', '30')

    page_list = [p.strip() for p in pages.split(',') if p.strip()]
    pages_json = '[' + ','.join(f'"{p}"' for p in page_list) + ']'

    js = f'window.CAROUSEL_CONFIG = {{ pages: {pages_json}, interval: {interval} }};'
    return Response(js, mimetype='application/javascript', headers={'Cache-Control': 'no-cache'})

@app.route('/proxy/')
def proxy():
    url = request.args.get('url')
    if not url:
        return 'Missing url', 400
    try:
        r = requests.get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        # Strip headers that block iframes
        excluded = {'x-frame-options', 'content-security-policy', 'transfer-encoding'}
        headers = {k: v for k, v in r.headers.items() if k.lower() not in excluded}
        return Response(r.content, status=r.status_code, headers=headers)
    except Exception as e:
        return f'Proxy error: {e}', 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)
