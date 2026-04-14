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

def rewrite_html(content, base_url):
    text = content.decode('utf-8', errors='replace')

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

    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    base_script = f'''<base href="{origin}/">
    <script>
    (function() {{
        var BASE = "{base_url}";
        var ORIGIN = "{origin}";

        // Proxy fetch
        var origFetch = window.fetch;
        window.fetch = function(url, opts) {{
            if (typeof url === "string" && !url.startsWith("/proxy")) {{
                var abs = url.startsWith("http") ? url : new URL(url, BASE).href;
                url = "/proxy/?url=" + encodeURIComponent(abs);
            }}
            return origFetch(url, opts);
        }};

        // Proxy XHR
        var origXHR = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url) {{
            if (typeof url === "string" && !url.startsWith("/proxy")) {{
                var abs = url.startsWith("http") ? url : new URL(url, BASE).href;
                url = "/proxy/?url=" + encodeURIComponent(abs);
            }}
            return origXHR.apply(this, arguments);
        }};

        // Intercept window.location redirects
        var _assign = window.location.assign.bind(window.location);
        Object.defineProperty(window, 'location', {{
            get: function() {{ return window._location_real || location; }},
            set: function(url) {{
                var abs = (typeof url === "string" && !url.startsWith("http"))
                    ? new URL(url, BASE).href : url;
                _assign("/proxy/?url=" + encodeURIComponent(abs));
            }}
        }});

        // Intercept history.pushState / replaceState (SPA routing)
        var origPush    = history.pushState.bind(history);
        var origReplace = history.replaceState.bind(history);
        function proxyState(fn, state, title, url) {{
            if (url && !url.startsWith("/proxy")) {{
                var abs = url.startsWith("http") ? url : new URL(url, BASE).href;
                url = "/proxy/?url=" + encodeURIComponent(abs);
            }}
            return fn(state, title, url);
        }}
        history.pushState    = function(s,t,u) {{ proxyState(origPush,    s, t, u); }};
        history.replaceState = function(s,t,u) {{ proxyState(origReplace, s, t, u); }};
    }})();
    </script>'''

    if '<head>' in text:
        text = text.replace('<head>', '<head>' + base_script, 1)
    else:
        text = base_script + text

    return text.encode('utf-8')


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
        sess = session_for(url)
        r = sess.get(url, timeout=10, allow_redirects=True, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        excluded = {'x-frame-options', 'content-security-policy', 'transfer-encoding',
                    'content-encoding', 'content-length'}
        headers = {k: v for k, v in r.headers.items() if k.lower() not in excluded}

        content_type = r.headers.get('content-type', '')
        if 'text/html' in content_type:
            content = rewrite_html(r.content, url)
            headers['content-type'] = 'text/html; charset=utf-8'
        elif 'javascript' in content_type:
            content = r.content
            headers['content-type'] = content_type
        else:
            content = r.content

        return Response(content, status=r.status_code, headers=headers)
    except Exception as e:
        return f'Proxy error: {e}', 500


@app.route('/proxy/', methods=['POST'])
def proxy_post():
    """Handles login form POSTs — passes form data through and stores the session cookie."""
    url = request.args.get('url')
    if not url:
        return 'Missing url', 400
    try:
        sess = session_for(url)

        content_type = request.content_type or ''
        if 'application/json' in content_type:
            r = sess.post(url, json=request.get_json(silent=True), timeout=10, allow_redirects=True, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Content-Type': content_type,
            })
        else:
            r = sess.post(url, data=request.form, timeout=10, allow_redirects=True, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Content-Type': content_type,
            })

        excluded = {'x-frame-options', 'content-security-policy', 'transfer-encoding',
                    'content-encoding', 'content-length'}
        headers = {k: v for k, v in r.headers.items() if k.lower() not in excluded}

        content_type_resp = r.headers.get('content-type', '')
        if 'text/html' in content_type_resp:
            content = rewrite_html(r.content, url)
            headers['content-type'] = 'text/html; charset=utf-8'
        elif 'javascript' in content_type_resp:
            content = r.content
            headers['content-type'] = content_type_resp
        else:
            content = r.content

        return Response(content, status=r.status_code, headers=headers)
    except Exception as e:
        return f'Proxy error: {e}', 500


@app.route('/login-helper')
def login_helper():
    """A simple page listing all configured URLs so you can click through and log in."""
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
<p>Click each link and log in through the proxy. Sessions are stored server-side and will be reused by the carousel.</p>
<ul>{links}</ul>
</body></html>'''
    return Response(html, mimetype='text/html')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)
