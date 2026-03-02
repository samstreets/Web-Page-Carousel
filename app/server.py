import os
import re
import requests
from flask import Flask, request, Response, send_file
from urllib.parse import urljoin, urlparse, quote

app = Flask(__name__)

def rewrite_html(content, base_url):
    """Rewrite relative URLs in HTML to go through our proxy."""
    text = content.decode('utf-8', errors='replace')
    
    def rewrite_url(url, base):
        if not url or url.startswith('data:') or url.startswith('javascript:') or url.startswith('#'):
            return url
        absolute = urljoin(base, url)
        return '/proxy/?url=' + quote(absolute, safe='')

    # Rewrite src and href attributes
    def replace_attr(m):
        attr = m.group(1)
        quote_char = m.group(2)
        url = m.group(3)
        new_url = rewrite_url(url, base_url)
        return f'{attr}={quote_char}{new_url}{quote_char}'

    text = re.sub(r'(src|href|action)=(["\'])(?!#)([^"\']+)\2', replace_attr, text, flags=re.IGNORECASE)

    # Rewrite CSS url()
    def replace_css_url(m):
        url = m.group(1).strip('\'"')
        new_url = rewrite_url(url, base_url)
        return f'url("{new_url}")'

    text = re.sub(r'url\(([^)]+)\)', replace_css_url, text)

    # Inject base tag and script to fix fetch/XHR
    base_script = f'''<script>
    (function() {{
        var BASE = "{base_url}";
        var origFetch = window.fetch;
        window.fetch = function(url, opts) {{
            if (typeof url === "string" && !url.startsWith("http") && !url.startsWith("/proxy")) {{
                var abs = new URL(url, BASE).href;
                url = "/proxy/?url=" + encodeURIComponent(abs);
            }}
            return origFetch(url, opts);
        }};
        var origXHR = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url) {{
            if (typeof url === "string" && !url.startsWith("http") && !url.startsWith("/proxy")) {{
                var abs = new URL(url, BASE).href;
                url = "/proxy/?url=" + encodeURIComponent(abs);
            }}
            return origXHR.apply(this, arguments);
        }};
    }})();
    </script>'''

    text = text.replace('<head>', '<head>' + base_script, 1)
    if '<head>' not in text:
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
        r = requests.get(url, timeout=10, allow_redirects=True, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        excluded = {'x-frame-options', 'content-security-policy', 'transfer-encoding', 'content-encoding', 'content-length'}
        headers = {k: v for k, v in r.headers.items() if k.lower() not in excluded}

        content_type = r.headers.get('content-type', '')
        if 'text/html' in content_type:
            content = rewrite_html(r.content, url)
            headers['content-type'] = 'text/html; charset=utf-8'
        else:
            content = r.content

        return Response(content, status=r.status_code, headers=headers)
    except Exception as e:
        return f'Proxy error: {e}', 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)
