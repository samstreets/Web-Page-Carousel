import os
import re
import requests
from flask import Flask, request, Response, send_file
from urllib.parse import urljoin, urlparse, quote
from collections import defaultdict

app = Flask(__name__)

# One persistent session per hostname — stores cookies automatically
_sessions = defaultdict(requests.Session)

def session_for(url):
    host = urlparse(url).netloc
    return _sessions[host]

def rewrite_cookies(raw_headers):
    """
    Rewrite Set-Cookie headers so the browser accepts them under localhost:
    - Strip Domain= (so cookie applies to localhost)
    - Strip Secure flag (we're on http locally)
    - Strip SameSite=Strict/Lax (would block cross-site cookie sending)
    - Rewrite Path if needed
    """
    rewritten = []
    for raw in raw_headers.getlist('Set-Cookie'):
        parts = [p.strip() for p in raw.split(';')]
        filtered = []
        for p in parts:
            key = p.split('=')[0].strip().lower()
            if key == 'secure':
                continue
            if key == 'domain':
                continue
            if key == 'samesite':
                # Replace with None would need Secure, so just drop it
                continue
            filtered.append(p)
        rewritten.append('; '.join(filtered))
    return rewritten


def rewrite_html(content, base_url):
    text = content.decode('utf-8', errors='replace')

    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    def rewrite_url(url, base):
        if not url or url.startswith('data:') or url.startswith('javascript:') or url.startswith('#'):
            return url
        absolute = urljoin(base, url)
        return '/proxy/?url=' + quote(absolute, safe='')

    def replace_attr(m):
        attr, quote_char, url = m.group(1), m.group(2), m.group(3)
        return f'{attr}={quote_char}{rewrite_url(url, base_url)}{quote_char}'

    text = re.sub(r'(src|href|action)=(["\'])(?!#)([^"\']+)\2', replace_attr, text, flags=re.IGNORECASE)

    def replace_css_url(m):
        url = m.group(1).strip('\'"')
        return f'url("{rewrite_url(url, base_url)}")'

    text = re.sub(r'url\(([^)]+)\)', replace_css_url, text)

    base_script = f'''<script>
(function() {{
    var BASE = "{base_url}";
    var ORIGIN = "{origin}";

    function toProxy(url) {{
        if (!url || url.startsWith('data:') || url.startsWith('blob:') || url.startsWith('/proxy/?url=')) {{
            return url;
        }}
        var abs;
        if (url.startsWith('http://') || url.startsWith('https://')) {{
            abs = url;
        }} else if (url.startsWith('//')) {{
            abs = '{parsed.scheme}:' + url;
        }} else if (url.startsWith('/')) {{
            abs = ORIGIN + url;
        }} else {{
            abs = new URL(url, BASE).href;
        }}
        return '/proxy/?url=' + encodeURIComponent(abs);
    }}

    // Proxy fetch
    var origFetch = window.fetch;
    window.fetch = function(url, opts) {{
        if (typeof url === 'string' && !url.startsWith('/proxy/')) url = toProxy(url);
        if (url instanceof Request && !url.url.startsWith('/proxy/')) {{
            url = new Request(toProxy(url.url), url);
        }}
        return origFetch(url, opts);
    }};

    // Proxy XHR
    var origXHR = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {{
        if (typeof url === 'string' && !url.startsWith('/proxy/')) url = toProxy(url);
        return origXHR.apply(this, arguments);
    }};

    // Proxy WebSocket — reroute wss:// through a ws-proxy endpoint
    var OrigWS = window.WebSocket;
    window.WebSocket = function(url, protocols) {{
        var wsProxy = '/wsproxy/?url=' + encodeURIComponent(url);
        // Convert http proxy base to ws
        var loc = window.location;
        var wsBase = (loc.protocol === 'https:' ? 'wss://' : 'ws://') + loc.host;
        var fullWsUrl = wsBase + wsProxy;
        if (protocols) {{
            return new OrigWS(fullWsUrl, protocols);
        }}
        return new OrigWS(fullWsUrl);
    }};
    window.WebSocket.CONNECTING = 0;
    window.WebSocket.OPEN = 1;
    window.WebSocket.CLOSING = 2;
    window.WebSocket.CLOSED = 3;

    // Intercept navigation
    var origAssign  = window.location.assign.bind(window.location);
    var origReplace = window.location.replace.bind(window.location);

    window.location.assign  = function(url) {{ origAssign(toProxy(url)); }};
    window.location.replace = function(url) {{ origReplace(toProxy(url)); }};

    // Intercept history SPA routing
    var origPush     = history.pushState.bind(history);
    var origRepState = history.replaceState.bind(history);
    history.pushState    = function(s, t, u) {{ origPush(s, t, u ? toProxy(u) : u); }};
    history.replaceState = function(s, t, u) {{ origRepState(s, t, u ? toProxy(u) : u); }};

    // Catch <a> clicks that weren't rewritten (dynamically inserted links)
    document.addEventListener('click', function(e) {{
        var a = e.target.closest('a');
        if (!a) return;
        var href = a.getAttribute('href');
        if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
        if (!href.startsWith('/proxy/')) {{
            e.preventDefault();
            window.location.assign(toProxy(href));
        }}
    }}, true);

    // Patch document.cookie getter/setter to work cross-origin
    // This prevents JS cookie reads from failing silently
    try {{
        var cookieDesc = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie') ||
                         Object.getOwnPropertyDescriptor(HTMLDocument.prototype, 'cookie');
        if (cookieDesc && cookieDesc.configurable) {{
            Object.defineProperty(document, 'cookie', {{
                get: function() {{ return cookieDesc.get.call(document); }},
                set: function(val) {{ cookieDesc.set.call(document, val); }},
                configurable: true
            }});
        }}
    }} catch(e) {{}}
}})();
</script>'''

    if '<head>' in text:
        text = text.replace('<head>', '<head>' + base_script, 1)
    elif '<html>' in text:
        text = text.replace('<html>', '<html>' + base_script, 1)
    else:
        text = base_script + text

    return text.encode('utf-8')


def build_response(r):
    """Build a Flask Response from a requests.Response, with cookie rewriting."""
    excluded = {'x-frame-options', 'content-security-policy', 'transfer-encoding',
                'content-encoding', 'content-length', 'strict-transport-security',
                'set-cookie'}  # handle cookies manually below
    headers = {k: v for k, v in r.headers.items() if k.lower() not in excluded}

    content_type = r.headers.get('content-type', '')
    if 'text/html' in content_type:
        content = rewrite_html(r.content, r.url)
        headers['content-type'] = 'text/html; charset=utf-8'
    else:
        content = r.content

    resp = Response(content, status=r.status_code, headers=headers)

    # Rewrite and re-attach cookies
    for cookie_str in rewrite_cookies(r.raw.headers):
        resp.headers.add('Set-Cookie', cookie_str)

    return resp


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


PROXY_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}


@app.route('/proxy/', methods=['GET'])
def proxy():
    url = request.args.get('url')
    if not url:
        return 'Missing url', 400
    try:
        sess = session_for(url)
        hdrs = {**PROXY_HEADERS,
                'Accept': request.headers.get('Accept', 'text/html,*/*'),
                'Referer': url}  # some sites check Referer for CSRF
        r = sess.get(url, timeout=15, allow_redirects=True, headers=hdrs)
        return build_response(r)
    except Exception as e:
        return f'Proxy error: {e}', 500


@app.route('/proxy/', methods=['POST'])
def proxy_post():
    url = request.args.get('url')
    if not url:
        return 'Missing url', 400
    try:
        sess = session_for(url)
        ct = request.content_type or ''
        hdrs = {**PROXY_HEADERS,
                'Accept': request.headers.get('Accept', '*/*'),
                'Referer': url}

        if 'application/json' in ct:
            r = sess.post(url, json=request.get_json(silent=True, force=True),
                          timeout=15, allow_redirects=True,
                          headers={**hdrs, 'Content-Type': ct})
        elif 'application/x-www-form-urlencoded' in ct or 'multipart/form-data' in ct:
            r = sess.post(url, data=request.form, files=request.files or None,
                          timeout=15, allow_redirects=True, headers=hdrs)
        else:
            r = sess.post(url, data=request.get_data(),
                          timeout=15, allow_redirects=True,
                          headers={**hdrs, 'Content-Type': ct})

        return build_response(r)
    except Exception as e:
        return f'Proxy error: {e}', 500


# ---------------------------------------------------------------------------
# WebSocket proxy — bridges browser WS to the upstream wss:// server
# Requires: pip install flask-sockets gevent gevent-websocket
# The Dockerfile pip install line must include these packages.
# ---------------------------------------------------------------------------
try:
    from geventwebsocket import WebSocketError
    from geventwebsocket.handler import WebSocketHandler
    import websocket as ws_client  # websocket-client

    @app.route('/wsproxy/')
    def wsproxy():
        target_url = request.args.get('url')
        if not target_url:
            return 'Missing url', 400

        client_ws = request.environ.get('wsgi.websocket')
        if not client_ws:
            return 'WebSocket upgrade required', 426

        try:
            upstream = ws_client.create_connection(
                target_url,
                header=[f'User-Agent: {PROXY_HEADERS["User-Agent"]}'],
                suppress_origin=True,
            )
        except Exception as e:
            return f'WS upstream connect error: {e}', 502

        import gevent

        def client_to_upstream():
            try:
                while True:
                    msg = client_ws.receive()
                    if msg is None:
                        break
                    upstream.send(msg)
            except WebSocketError:
                pass
            finally:
                upstream.close()

        def upstream_to_client():
            try:
                while True:
                    msg = upstream.recv()
                    if msg is None:
                        break
                    client_ws.send(msg)
            except (WebSocketError, Exception):
                pass
            finally:
                client_ws.close()

        c2u = gevent.spawn(client_to_upstream)
        u2c = gevent.spawn(upstream_to_client)
        gevent.joinall([c2u, u2c])
        return ''

    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

    @app.route('/wsproxy/')
    def wsproxy():
        return 'WebSocket proxy not available — install flask-sockets gevent gevent-websocket websocket-client', 501


@app.route('/login-helper')
def login_helper():
    pages = os.environ.get('PAGES', '')
    page_list = [p.strip() for p in pages.split(',') if p.strip()]
    links = ''.join(
        f'<li><a href="/proxy/?url={quote(p, safe="")}" target="_blank">{p}</a></li>'
        for p in page_list
    )
    html = f'''<!DOCTYPE html>
<html><head><title>Login helper</title>
<style>
  body {{ font-family: monospace; padding: 2em; background: #111; color: #eee; }}
  h2 {{ color: #0ff; }}
  p {{ color: #aaa; margin-bottom: 1.5em; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ margin: 0.6em 0; }}
  a {{ color: #0ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head><body>
<h2>Login helper</h2>
<p>Click each link, log in through the proxy window that opens, then close it and return here.<br>
Sessions are stored server-side and will be reused automatically by the carousel.</p>
<ul>{links}</ul>
</body></html>'''
    return Response(html, mimetype='text/html')


if __name__ == '__main__':
    if WS_AVAILABLE:
        from gevent import pywsgi
        from geventwebsocket.handler import WebSocketHandler
        server = pywsgi.WSGIServer(('0.0.0.0', 80), app, handler_class=WebSocketHandler)
        print("Starting with WebSocket support")
        server.serve_forever()
    else:
        print("Starting without WebSocket support (install gevent packages to enable)")
        app.run(host='0.0.0.0', port=80)