# -*- coding: utf-8 -*-
"""
iOS 最小验证用 HTTP 服务：纯标准库，绑定 127.0.0.1:8765。
ObjC 侧调用 start() 后在后台 daemon 线程 serve_forever，start 立即返回。
浏览器/WKWebView 打开根路径能看到一个验证页，证明 Python 在 iOS 上
能初始化解释器、开 socket、响应 HTTP 请求。
"""
import threading
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PORT = 8765
_started = False
_lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<style>body{font-family:-apple-system;background:#111;color:#eee;"
            "padding:28px;line-height:1.7}h1{color:#5cf}code{color:#fd6}</style>"
            "</head><body>"
            "<h1>Python on iOS OK</h1>"
            "<p>嵌入式 CPython 已在 iOS 上成功启动并响应 HTTP 请求。</p>"
            "<p><code>Python %s</code></p>" % sys.version.split()[0]
            + "<hr><p>方案 B 链路验证：解释器 ✓ · socket ✓ · WebView ✓</p>"
            "<p style='color:#888'>下一步：把 app.py 移植进来。</p>"
            "</body></html>"
        ).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def log_message(self, fmt, *args):
        # 静默，避免刷屏设备日志
        pass


def start():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print("[poc_server] serving on http://127.0.0.1:%d" % PORT, flush=True)
