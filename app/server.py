import os
import re
import sys
import time
import logging
import requests
from flask import Flask, request, Response, send_file
from urllib.parse import urljoin, urlparse, quote
from collections import defaultdict

# ---------------------------------------------------------------------------
# Logging setup — colour-coded, structured, stdout
# ---------------------------------------------------------------------------

RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
RED     = "\033[91m"
YELLOW  = "\033[93m"
GREEN   = "\033[92m"
CYAN    = "\033[96m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"
WHITE   = "\033[97m"


class ColourFormatter(logging.Formatter):
    LEVEL_COLOURS = {
        logging.DEBUG:    DIM + WHITE,
        logging.INFO:     CYAN,
        logging.WARNING:  YELLOW,
        logging.ERROR:    RED,
        logging.CRITICAL: BOLD + RED,
    }

    def format(self, record):
        colour = self.LEVEL_COLOURS.get(record.levelno, RESET)
        ts = time.strftime('%H:%M:%S')
        level = f"{colour}{record.levelname:<8}{RESET}"
        return f"{DIM}{ts}{RESET}  {level}  {record.getMessage()}"


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(ColourFormatter())
logging.root.setLevel(logging.DEBUG)
logging.root.handlers = [_handler]

for _noisy in ('urllib3', 'werkzeug', 'requests'):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger('proxy')

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__)
_sessions = defaultdict(requests.Session)


def session_for(url):
    host = urlparse(url).netloc
    return _sessions[host]


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

STATUS_COLOUR = {2: GREEN, 3: YELLOW, 4: RED, 5: BOLD + RED}


def coloured_status(code):
    return f"{STATUS_COLOUR.get(code // 100, WHITE)}{code}{RESET}"


def log_upstream_request(method, final_url, status_code, elapsed_ms, content_type=''):
    sc  = coloured_status(status_code)
    ms  = f"{elapsed_ms:.0f}ms"
    ct  = content_type.split(';')[0]
    url = final_url if len(final_url) <= 80 else final_url[:77] + '…'
    log.info(f"  {BOLD}{method:<5}{RESET} {sc} {MAGENTA}{ms:>6}{RESET}  {url}  {DIM}{ct}{RESET}")


def log_redirect_chain(history):
    for resp in history:
        loc = resp.headers.get('Location', '?')
        log.debug(f"    {DIM}↳ {coloured_status(resp.status_code)}  {loc}{RESET}")


def log_cookies_sent(sess, url):
    host = urlparse(url).netloc
    names = [c.name for c in sess.cookies if c.domain and c.domain in host]
    if names:
        log.debug(f"  {DIM}→ Cookies sent: {', '.join(names)}{RESET}")
    else:
        log.debug(f"  {DIM}→ No cookies for {host}{RESET}")


def log_cookies_received(raw_headers):
    cookies = raw_headers.getlist('Set-Cookie')
    if cookies:
        for c in cookies:
            log.debug(f"  {DIM}← Set-Cookie: {c.split('=')[0]}=…{RESET}")
    else:
        log.debug(f"  {DIM}← No Set-Cookie headers{RESET}")


def log_notable_response_headers(headers):
    """Warn about headers that are known to cause proxy breakage."""
    notable = {
        'x-frame-options':         (YELLOW, 'stripped ✓'),
        'content-security-policy': (YELLOW, 'stripped ✓'),
        'strict-transport-security':(YELLOW,'stripped ✓'),
        'www-authenticate':        (RED,    'auth required!'),
        'location':                (CYAN,   'redirect'),
        'content-type':            (DIM,    ''),
    }
    for k, v in headers.items():
        if k.lower() in notable:
            colour, note = notable[k.lower()]
            note_str = f"  ← {note}" if note else ''
            log.debug(f"  {colour}↓ {k}: {v}{note_str}{RESET}")


# ---------------------------------------------------------------------------
# Cookie rewriting
# ---------------------------------------------------------------------------

def rewrite_cookies(raw_headers):
    rewritten = []
    for raw in raw_headers.getlist('Set-Cookie'):
        parts  = [p.strip() for p in raw.split(';')]
        kept, stripped = [], []
        for p in parts:
            key = p.split('=')[0].strip().lower()
            if key in ('secure', 'domain', 'samesite'):
                stripped.append(p)
            else:
                kept.append(p)
        if stripped:
            log.debug(f"  {DIM}⚙ Cookie rewrite stripped: {', '.join(stripped)}{RESET}")
        rewritten.append('; '.join(kept))
    return rewritten


# ---------------------------------------------------------------------------
# HTML rewriting
# ---------------------------------------------------------------------------

def rewrite_html(content, base_url):
    text   = content.decode('utf-8', errors='replace')
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    def rewrite_url(url, base):
        if not url or url.startswith(('data:', 'javascript:', '#')):
            return url
        return '/proxy/?url=' + quote(urljoin(base, url), safe='')

    def replace_attr(m):
        attr, q, url = m.group(1), m.group(2), m.group(3)
        return f'{attr}={q}{rewrite_url(url, base_url)}{q}'

    text, n_attr = re.subn(
        r'(src|href|action)=(["\'])(?!#)([^"\']+)\2', replace_attr, text, flags=re.IGNORECASE)

    def replace_css_url(m):
        return f'url("{rewrite_url(m.group(1).strip(chr(34)+chr(39)), base_url)}")'

    text, n_css = re.subn(r'url\(([^)]+)\)', replace_css_url, text)

    log.debug(f"  {DIM}⚙ HTML rewrite: {n_attr} attrs, {n_css} CSS urls{RESET}")

    inject = f'''<script>
(function() {{
    var BASE = "{base_url}";
    var ORIGIN = "{origin}";

    function toProxy(url) {{
        if (!url || url.startsWith('data:') || url.startsWith('blob:') || url.startsWith('/proxy/?url=')) return url;
        var abs;
        if (url.startsWith('http://') || url.startsWith('https://')) {{ abs = url; }}
        else if (url.startsWith('//')) {{ abs = '{parsed.scheme}:' + url; }}
        else if (url.startsWith('/')) {{ abs = ORIGIN + url; }}
        else {{ abs = new URL(url, BASE).href; }}
        return '/proxy/?url=' + encodeURIComponent(abs);
    }}

    /* fetch */
    var _fetch = window.fetch;
    window.fetch = function(url, opts) {{
        if (typeof url === 'string' && !url.startsWith('/proxy/')) url = toProxy(url);
        return _fetch(url, opts);
    }};

    /* XHR */
    var _xhrOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {{
        if (typeof url === 'string' && !url.startsWith('/proxy/')) url = toProxy(url);
        return _xhrOpen.apply(this, arguments);
    }};

    /* WebSocket */
    var _WS = window.WebSocket;
    window.WebSocket = function(url, protocols) {{
        console.log('[proxy] WS intercepted:', url);
        var loc = window.location;
        var wsBase = (loc.protocol === 'https:' ? 'wss://' : 'ws://') + loc.host;
        var proxied = wsBase + '/wsproxy/?url=' + encodeURIComponent(url);
        return protocols ? new _WS(proxied, protocols) : new _WS(proxied);
    }};
    window.WebSocket.CONNECTING = 0; window.WebSocket.OPEN = 1;
    window.WebSocket.CLOSING = 2;   window.WebSocket.CLOSED = 3;

    /* navigation */
    var _assign  = window.location.assign.bind(window.location);
    var _replace = window.location.replace.bind(window.location);
    window.location.assign  = function(u) {{ console.log('[proxy] assign:', u); _assign(toProxy(u)); }};
    window.location.replace = function(u) {{ console.log('[proxy] replace:', u); _replace(toProxy(u)); }};

    /* history SPA */
    var _push = history.pushState.bind(history), _rep = history.replaceState.bind(history);
    history.pushState    = function(s,t,u) {{ _push(s,t, u ? toProxy(u) : u); }};
    history.replaceState = function(s,t,u) {{ _rep(s,t,  u ? toProxy(u) : u); }};

    /* dynamic links */
    document.addEventListener('click', function(e) {{
        var a = e.target.closest('a');
        if (!a) return;
        var href = a.getAttribute('href');
        if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
        if (!href.startsWith('/proxy/')) {{ e.preventDefault(); window.location.assign(toProxy(href)); }}
    }}, true);
}})();
</script>'''

    if '<head>' in text:
        text = text.replace('<head>', '<head>' + inject, 1)
    elif '<html>' in text:
        text = text.replace('<html>', '<html>' + inject, 1)
    else:
        text = inject + text

    return text.encode('utf-8')


# ---------------------------------------------------------------------------
# Shared upstream call + response builder
# ---------------------------------------------------------------------------

PROXY_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
    'Accept-Language': 'en-US,en;q=0.9',
}

STRIP_RESPONSE = {
    'x-frame-options', 'content-security-policy', 'transfer-encoding',
    'content-encoding', 'content-length', 'strict-transport-security', 'set-cookie',
}


def do_request(method, url, **kwargs):
    sess = session_for(url)
    log_cookies_sent(sess, url)

    t0 = time.perf_counter()
    r  = sess.request(method, url, timeout=15, allow_redirects=True, **kwargs)
    ms = (time.perf_counter() - t0) * 1000

    if r.history:
        log.debug(f"  Redirect chain ({len(r.history)} hop(s)):")
        log_redirect_chain(r.history)

    log_upstream_request(method, r.url, r.status_code, ms, r.headers.get('content-type', ''))
    log_notable_response_headers(r.headers)
    log_cookies_received(r.raw.headers)

    if r.status_code >= 400:
        preview = r.text[:400].replace('\n', ' ')
        log.warning(f"  {YELLOW}Body preview: {preview}{RESET}")

    return r


def build_response(r):
    headers = {k: v for k, v in r.headers.items() if k.lower() not in STRIP_RESPONSE}
    ct = r.headers.get('content-type', '')
    if 'text/html' in ct:
        content = rewrite_html(r.content, r.url)
        headers['content-type'] = 'text/html; charset=utf-8'
    else:
        content = r.content
    resp = Response(content, status=r.status_code, headers=headers)
    for c in rewrite_cookies(r.raw.headers):
        resp.headers.add('Set-Cookie', c)
    return resp


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return send_file('/app/index.html')


@app.route('/config.js')
def config():
    pages    = os.environ.get('PAGES', 'https://example.com')
    interval = os.environ.get('INTERVAL', '30')
    page_list = [p.strip() for p in pages.split(',') if p.strip()]
    pages_json = '[' + ','.join(f'"{p}"' for p in page_list) + ']'
    js = f'window.CAROUSEL_CONFIG = {{ pages: {pages_json}, interval: {interval} }};'
    log.debug(f"config.js served: {len(page_list)} pages, interval={interval}s")
    return Response(js, mimetype='application/javascript', headers={'Cache-Control': 'no-cache'})


@app.route('/proxy/', methods=['GET'])
def proxy():
    url = request.args.get('url')
    if not url:
        log.warning("GET /proxy/ missing ?url=")
        return 'Missing url', 400

    log.info(f"{CYAN}┌ GET  {url}{RESET}")
    try:
        r = do_request('GET', url, headers={
            **PROXY_HEADERS,
            'Accept':  request.headers.get('Accept', 'text/html,*/*'),
            'Referer': url,
        })
        resp = build_response(r)
        log.info(f"{CYAN}└ done{RESET}")
        return resp
    except requests.exceptions.ConnectionError as e:
        log.error(f"{RED}Connection error → {url}: {e}{RESET}")
        return f'Connection error: {e}', 502
    except requests.exceptions.Timeout:
        log.error(f"{RED}Timeout → {url}{RESET}")
        return 'Upstream timeout', 504
    except Exception as e:
        log.exception(f"{RED}Unexpected error → {url}{RESET}")
        return f'Proxy error: {e}', 500


@app.route('/proxy/', methods=['POST'])
def proxy_post():
    url = request.args.get('url')
    if not url:
        log.warning("POST /proxy/ missing ?url=")
        return 'Missing url', 400

    ct = request.content_type or ''
    log.info(f"{CYAN}┌ POST {url}  {DIM}[{ct}]{RESET}")

    try:
        base = {**PROXY_HEADERS, 'Accept': request.headers.get('Accept', '*/*'), 'Referer': url}

        if 'application/json' in ct:
            body = request.get_json(silent=True, force=True)
            log.debug(f"  {DIM}body(json): {str(body)[:200]}{RESET}")
            r = do_request('POST', url, json=body, headers={**base, 'Content-Type': ct})

        elif 'application/x-www-form-urlencoded' in ct or 'multipart/form-data' in ct:
            log.debug(f"  {DIM}form fields: {list(request.form.keys())}{RESET}")
            r = do_request('POST', url, data=request.form,
                           files=request.files or None, headers=base)
        else:
            raw = request.get_data()
            log.debug(f"  {DIM}body(raw {len(raw)}B): {raw[:200]}{RESET}")
            r = do_request('POST', url, data=raw, headers={**base, 'Content-Type': ct})

        resp = build_response(r)
        log.info(f"{CYAN}└ done{RESET}")
        return resp
    except requests.exceptions.ConnectionError as e:
        log.error(f"{RED}Connection error → {url}: {e}{RESET}")
        return f'Connection error: {e}', 502
    except requests.exceptions.Timeout:
        log.error(f"{RED}Timeout → {url}{RESET}")
        return 'Upstream timeout', 504
    except Exception as e:
        log.exception(f"{RED}Unexpected error → {url}{RESET}")
        return f'Proxy error: {e}', 500


# ---------------------------------------------------------------------------
# WebSocket proxy
# ---------------------------------------------------------------------------

try:
    from geventwebsocket import WebSocketError
    import websocket as ws_client

    @app.route('/wsproxy/')
    def wsproxy():
        target = request.args.get('url')
        if not target:
            return 'Missing url', 400

        client_ws = request.environ.get('wsgi.websocket')
        if not client_ws:
            log.warning(f"WS upgrade missing for {target}")
            return 'WebSocket upgrade required', 426

        log.info(f"{MAGENTA}┌ WS open  {target}{RESET}")
        try:
            upstream = ws_client.create_connection(
                target,
                header=[f'User-Agent: {PROXY_HEADERS["User-Agent"]}'],
                suppress_origin=True,
            )
            log.info(f"{MAGENTA}│ upstream connected{RESET}")
        except Exception as e:
            log.error(f"{RED}WS upstream connect error: {e}{RESET}")
            return f'WS connect error: {e}', 502

        import gevent

        def c2u():
            try:
                while True:
                    msg = client_ws.receive()
                    if msg is None:
                        break
                    log.debug(f"  {DIM}WS c→u: {str(msg)[:120]}{RESET}")
                    upstream.send(msg)
            except WebSocketError as e:
                log.debug(f"  {DIM}WS client closed: {e}{RESET}")
            finally:
                upstream.close()

        def u2c():
            try:
                while True:
                    msg = upstream.recv()
                    if msg is None:
                        break
                    log.debug(f"  {DIM}WS u→c: {str(msg)[:120]}{RESET}")
                    client_ws.send(msg)
            except Exception as e:
                log.debug(f"  {DIM}WS upstream closed: {e}{RESET}")
            finally:
                client_ws.close()

        gevent.joinall([gevent.spawn(c2u), gevent.spawn(u2c)])
        log.info(f"{MAGENTA}└ WS closed {target}{RESET}")
        return ''

    WS_AVAILABLE = True

except ImportError as e:
    WS_AVAILABLE = False

    @app.route('/wsproxy/')
    def wsproxy():
        return 'WebSocket proxy unavailable', 501


# ---------------------------------------------------------------------------
# Login helper
# ---------------------------------------------------------------------------

@app.route('/login-helper')
def login_helper():
    pages = os.environ.get('PAGES', '')
    page_list = [p.strip() for p in pages.split(',') if p.strip()]
    links = ''.join(
        f'<li><a href="/proxy/?url={quote(p, safe="")}" target="_blank">{p}</a></li>'
        for p in page_list
    )
    html = f'''<!DOCTYPE html><html><head><title>Login helper</title>
<style>body{{font-family:monospace;padding:2em;background:#111;color:#eee}}
h2{{color:#0ff}}p{{color:#aaa;margin-bottom:1.5em}}ul{{list-style:none;padding:0}}
li{{margin:.6em 0}}a{{color:#0ff;text-decoration:none}}a:hover{{text-decoration:underline}}</style>
</head><body><h2>Login helper</h2>
<p>Click each link, log in through the proxy window, then close it.<br>
Sessions are stored server-side and reused by the carousel.</p>
<ul>{links}</ul></body></html>'''
    return Response(html, mimetype='text/html')


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    log.info(f"{BOLD}{GREEN}═══ Web Page Carousel proxy starting ═══{RESET}")
    log.info(f"Pages    : {os.environ.get('PAGES', 'https://example.com')}")
    log.info(f"Interval : {os.environ.get('INTERVAL', '30')}s")
    log.info(f"WebSocket: {'yes (gevent)' if WS_AVAILABLE else 'no'}")

    if WS_AVAILABLE:
        from gevent import pywsgi
        from geventwebsocket.handler import WebSocketHandler
        log.info(f"Server   : gevent WSGIServer on :80")
        pywsgi.WSGIServer(('0.0.0.0', 80), app, handler_class=WebSocketHandler).serve_forever()
    else:
        log.info(f"Server   : Flask dev on :80")
        app.run(host='0.0.0.0', port=80)
