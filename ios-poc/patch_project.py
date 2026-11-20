# -*- coding: utf-8 -*-
"""
在 macOS CI 上，对解压出来的官方 testbed 工程打补丁，变成最小验证 App：
  1) 用我们改造的 SceneDelegate.m 覆盖原文件（初始化 Python + WKWebView）
  2) 把 poc_server.py 放进 app/ 目录（PYTHONPATH 会包含它）
  3) 给 Info.plist 注入 NSAppTransportSecurity 例外，允许 http://127.0.0.1

用法：python3 patch_project.py <解压出的 testbed 目录> <overrides 目录>
"""
import os
import shutil
import sys
import plistlib

TESTBED = sys.argv[1]
OVERRIDES = sys.argv[2]

APP_DIR = os.path.join(TESTBED, "iOSTestbed")
scene_src = os.path.join(OVERRIDES, "SceneDelegate.m")
scene_dst = os.path.join(APP_DIR, "SceneDelegate.m")
poc_src = os.path.join(OVERRIDES, "poc_server.py")
poc_dst = os.path.join(APP_DIR, "app", "poc_server.py")
plist_path = os.path.join(APP_DIR, "iOSTestbed-Info.plist")

# 1) 覆盖 SceneDelegate.m
shutil.copyfile(scene_src, scene_dst)
print("patched:", scene_dst)

# 2) 放置 poc_server.py 到 app/（app 已由 install_python 处理进 bundle）
os.makedirs(os.path.dirname(poc_dst), exist_ok=True)
shutil.copyfile(poc_src, poc_dst)
print("placed:", poc_dst)

# 3) Info.plist 注入 ATS 例外：允许任意本地/HTTP 加载（POC 自用）
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
