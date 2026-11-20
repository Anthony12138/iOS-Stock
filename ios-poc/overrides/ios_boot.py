"""
iOS 启动垫片：把桌面版 app.py 原样跑在 iOS 内嵌的 CPython 里。

与安卓端 android_boot.py 一一对应，业务逻辑零改动：
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


def stop() -> None:
    global _srv, _port
    if _srv is not None:
        try:
            _srv.shutdown()
        except Exception:
            pass
        _srv = None
        _port = 0
