# -*- coding: utf-8 -*-
"""
在 macOS CI 上，对解压出来的官方 testbed 工程打补丁，变成完整版 App：
  1) 用改造的 SceneDelegate.m 覆盖原文件（初始化 Python + WKWebView 加载本地服务）
  2) 把 ios_boot.py / app.py / web/ 放进 bundle 的 app/ 目录（Python 可 import）
  3) 给 Info.plist 注入 NSAppTransportSecurity 例外，允许 http://127.0.0.1

用法：python3 patch_project.py <解压出的 testbed 目录> <overrides 目录>
  - app.py 与 web/ 的来源固定取「本脚本所在目录/app_source」，
    这样无论 CI 从哪个 cwd 调用都能找到，避免路径漂移。
"""
import os
import shutil
import sys
import plistlib

TESTBED = sys.argv[1]
OVERRIDES = sys.argv[2]
HERE = os.path.dirname(os.path.abspath(__file__))
APP_SOURCE = os.path.join(HERE, "app_source")

APP_DIR = os.path.join(TESTBED, "iOSTestbed")
BUNDLE_APP = os.path.join(APP_DIR, "app")          # bundle 里的只读 app 目录
plist_path = os.path.join(APP_DIR, "iOSTestbed-Info.plist")

# 1) 覆盖 SceneDelegate.m（ObjC 启动壳）
shutil.copyfile(os.path.join(OVERRIDES, "SceneDelegate.m"),
                os.path.join(APP_DIR, "SceneDelegate.m"))
print("patched:", os.path.join(APP_DIR, "SceneDelegate.m"))

# 2) 放置 Python 运行文件到 bundle 的 app/ 目录
#    ios_boot.py —— 启动垫片；app.py —— 业务本体；web/ —— 前端页面
os.makedirs(BUNDLE_APP, exist_ok=True)

for name in ("ios_boot.py", "app.py"):
    src = os.path.join(OVERRIDES, name) if name == "ios_boot.py" else os.path.join(APP_SOURCE, name)
    dst = os.path.join(BUNDLE_APP, name)
    shutil.copyfile(src, dst)
    print("placed:", dst, os.path.getsize(dst), "bytes")

src_web = os.path.join(APP_SOURCE, "web")
dst_web = os.path.join(BUNDLE_APP, "web")
if os.path.isdir(dst_web):
    shutil.rmtree(dst_web)
shutil.copytree(src_web, dst_web)
print("placed web ->", dst_web, os.listdir(dst_web))

# 3) Info.plist 注入 ATS 例外：允许本地 HTTP 加载（iOS App 自用）
with open(plist_path, "rb") as f:
    plist = plistlib.load(f)
plist["NSAppTransportSecurity"] = {
    "NSAllowsArbitraryLoads": True,
    "NSAllowsLocalNetworking": True,
}
with open(plist_path, "wb") as f:
    plistlib.dump(plist, f)
print("patched ATS:", plist_path)

print("PATCH_OK")
