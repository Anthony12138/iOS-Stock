"""
iOS 启动垫片：把桌面版 app.py 原样跑在 iOS 内嵌的 CPython 里。

与桌面版 app.py 一一对应，业务逻辑零改动（安卓端已于 v1.7.0 下线）：
  - 桌面版靠 `python app.py` 启动并自动开浏览器；
  - 移动端由原生壳调用 start(bundle_app_dir, runtime_dir)，
    只把 HTTP 服务跑起来，再把端口号回传给 WKWebView。

与安卓的差异仅两点：
  1) iOS 的 app.py 与 web/ 在只读的 App bundle 里，需先复制到可写的
     runtime 目录（Documents/runtime），app.py 的 DATA_DIR 才会落在该目录；
  2) runtime 路径由原生壳传入（iOS 没有 Android 的 getFilesDir）。
"""
import os
import sys
import shutil
import threading
import time

_srv = None
_port = 0
_lock = threading.Lock()


def _ensure_stdio(runtime: str):
    """iOS GUI App 没有控制台时 sys.stdout / sys.stderr 可能为 None。
    未捕获异常的 traceback 会写 stderr，若为 None 会在异常处理路径上二次崩溃，
    表现为「整个进程莫名退出、没有任何提示」。这里兜底到 runtime 下的日志文件；
    实在打不开就丢弃到 StringIO，保证 print / traceback 永远不抛二次异常。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    import io
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is not None:
            continue
        try:
            f = open(os.path.join(runtime, name + ".log"), "a",
                     buffering=1, encoding="utf-8", errors="replace")
        except Exception:
            f = io.StringIO()
        setattr(sys, name, f)


def _install_excepthook(runtime: str):
    """把主线程 / 子线程未捕获异常追加到 runtime/data/crash.log。
    真机没有 Xcode 控制台，出问题只能靠这份日志定位。"""
    import traceback
    log_path = os.path.join(runtime, "data", "crash.log")

    def _write(exc_type, exc, tb, prefix):
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write("\n[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), prefix))
                traceback.print_exception(exc_type, exc, tb, file=f)
        except Exception:
            pass

    sys.excepthook = lambda t, e, tb: _write(t, e, tb, "main")
    try:
        threading.excepthook = lambda a: _write(
            a.exc_type, a.exc_value, a.exc_traceback,
            "thread:%s" % (a.thread.name if a.thread else "?"))
    except Exception:
        pass


def _write_startup_log(runtime: str):
    """记录一次启动时的运行环境，便于判断 SSL / 时区 / CA 是否正常。"""
    lines = []
    try:
        lines.append("python=%s" % sys.version.replace("\n", " "))
        try:
            import ssl
            lines.append("openssl=%s" % ssl.OPENSSL_VERSION)
        except Exception as e:
            lines.append("openssl=UNAVAILABLE (%s)" % e)
        try:
            import certifi
            lines.append("certifi=%s" % certifi.where())
        except Exception:
            lines.append("certifi=missing -> 使用不校验 context（仅公开行情接口）")
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("Asia/Shanghai")
            lines.append("zoneinfo=OK")
        except Exception as e:
            lines.append("zoneinfo=fallback UTC+8 (%s)" % e)
        lines.append("frozen=%r executable=%r" % (getattr(sys, "frozen", False), sys.executable))
    except Exception:
        pass
    try:
        p = os.path.join(runtime, "data", "startup.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def _install_ssl():
    """iOS 的 CPython 缺 CA 根证书时，保证所有 https 行情接口能握手成功。

    与安卓同款处理：优先用 certifi 的根证书；没有 certifi 就退化为不校验。
    这里只访问公开行情接口，不传任何账号凭据，不校验可接受。
    """
    try:
        import ssl
    except Exception:
        return
    try:
        import certifi
        cafile = certifi.where()
        ctx = ssl.create_default_context(cafile=cafile)
        os.environ["SSL_CERT_FILE"] = cafile
    except Exception:
        ctx = ssl._create_unverified_context()
    ssl._create_default_https_context = lambda: ctx


def _prepare_runtime(bundle_app: str, runtime: str):
    """把 bundle 里的 app.py + web/ 同步到可写 runtime 目录。

    逐文件比对：内容一致则跳过，避免每次启动都全量重写（web/index.html 有 323KB）。
    bundle 只读、runtime 可写；app.py 的 chdir 与 DATA_DIR 都基于 runtime。
    """
    os.makedirs(runtime, exist_ok=True)

    def sync_file(src, dst):
        need = True
        if os.path.exists(dst):
            try:
                with open(src, "rb") as a, open(dst, "rb") as b:
                    need = a.read() != b.read()
            except Exception:
                need = True
        if need:
            shutil.copyfile(src, dst)

    sync_file(os.path.join(bundle_app, "app.py"), os.path.join(runtime, "app.py"))
    src_web = os.path.join(bundle_app, "web")
    dst_web = os.path.join(runtime, "web")
    if os.path.isdir(src_web):
        os.makedirs(dst_web, exist_ok=True)
        for name in os.listdir(src_web):
            s = os.path.join(src_web, name)
            if os.path.isfile(s):
                sync_file(s, os.path.join(dst_web, name))


def start(bundle_app: str, runtime: str) -> int:
    """启动本地 HTTP 服务，返回实际端口；重复调用返回已有端口。"""
    global _srv, _port
    with _lock:
        if _srv is not None:
            return _port

        # 手机轻量模式：并发线程砍到 4，解释执行环境下更快更省内存（同安卓）
        os.environ["NIGHT_LIGHT"] = "1"
        os.environ["NO_BROWSER"] = "1"

        _prepare_runtime(bundle_app, runtime)
        _ensure_stdio(runtime)                  # 必须在 import app 之前：导入期异常也要能落日志
        _install_excepthook(runtime)

        os.chdir(runtime)                       # DATA_DIR 落在可写 runtime
        if runtime not in sys.path:
            sys.path.insert(0, runtime)

        _install_ssl()
        _write_startup_log(runtime)

        import app                              # 必须在 chdir 之后导入
        port = app.pick_port()
        srv = app.ThreadingHTTPServer((app.HOST, port), app.Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()

        _srv, _port = srv, port
        return port


def _probe(port: int, timeout: float = 1.5) -> bool:
    """真连一次 /api/health，确认服务此刻还能响应。

    v1.7.0：只查「对象还在、线程 is_alive」是不够的 —— iOS 挂起应用时会把
    监听的 socket 掐成半死状态，Python 服务线程照样活着，但 accept 再也不来
    请求，页面就一直是「服务异常」。所以必须真发一个 HTTP 请求过去。
    """
    if port <= 0:
        return False
    import urllib.request
    try:
        url = "http://127.0.0.1:%d/api/health" % port
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= getattr(r, "status", 200) < 400
    except Exception:
        return False


def ensure(bundle_app: str, runtime: str) -> int:
    """前台唤醒时调用：服务还能响应就返回原端口，半死/被回收就重启。

    iOS 在后台挂起应用时会掐掉监听 socket，回前台后请求全部失败，
    WKWebView 只能显示空白/黑屏。这里做两层存活检查：
      1) 对象与线程还在（廉价，先排除明显死掉的）；
      2) 真发一次 HTTP /api/health（1.5 秒）—— 这一步才能发现「线程活着但
         接受不到请求」的半死状态。
    任一层不过就关掉旧对象、重新起一个（端口可能变化，由原生壳更新 URL）。
    探活在锁外做，避免 1.5 秒的重启流程卡住其它调用。
    """
    global _srv, _port
    with _lock:
        srv, port = _srv, _port
    if srv is not None and port > 0:
        obj_ok = False
        try:
            srv.socket.getsockname()
            obj_ok = any(t.name == "http" and t.is_alive() for t in threading.enumerate())
        except Exception:
            obj_ok = False
        if obj_ok and _probe(port, 1.5):
            return port
    # 走到这里：要么没有服务，要么服务半死 → 清理后重启
    with _lock:
        if _srv is not None:
            try:
                _srv.shutdown()
            except Exception:
                pass
            try:
                _srv.server_close()
            except Exception:
                pass
            _srv = None
            _port = 0
    return start(bundle_app, runtime)


def stop() -> None:
    global _srv, _port
    if _srv is not None:
        try:
            _srv.shutdown()
        except Exception:
            pass
        _srv = None
        _port = 0
