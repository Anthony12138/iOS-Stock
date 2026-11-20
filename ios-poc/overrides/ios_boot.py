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

_srv = None
_port = 0
_lock = threading.Lock()


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

        os.chdir(runtime)                       # DATA_DIR 落在可写 runtime
        if runtime not in sys.path:
            sys.path.insert(0, runtime)

        _install_ssl()

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
