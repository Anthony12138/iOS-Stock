# -*- coding: utf-8 -*-
"""
在 macOS CI 上，对解压出来的官方 testbed 工程打补丁，变成完整版 App：
  1) 用改造的 SceneDelegate.m 覆盖原文件（初始化 Python + WKWebView 加载本地服务）
  2) 把 ios_boot.py / app.py / web/ 放进 bundle 的 app/ 目录（Python 可 import）
  3) 给 Info.plist 注入 NSAppTransportSecurity 例外，允许 http://127.0.0.1
  4) 改 App 显示名为「股票筛选系统」（CFBundleDisplayName / CFBundleName）
  5) 安装 App 图标：assets/AppIcon-1024.png → AppIcon.appiconset + 登记 Contents.json
  6) v1.7.2：注入沉浸式状态栏开关（白色文字 / 不隐藏），配合边缘到边缘 WebView
  7) v1.7.4：从 app.py 的 VERSION 读取版本号写入 plist（CFBundleShortVersionString /
     CFBundleVersion），并设包名 CFBundleIdentifier=org.python.stockscreener，
     让 LiveContainer / 设置页显示真实版本与包名，而不再是 testbed 默认的 1.0 / iOSTestbed

用法：python3 patch_project.py <解压出的 testbed 目录> <overrides 目录>
  - app.py 与 web/ 的来源固定取「本脚本所在目录/app_source」，
    图标固定取「本脚本所在目录/assets」，不依赖 cwd，避免路径漂移。
"""
import os
import shutil
import sys
import plistlib

TESTBED = sys.argv[1]
OVERRIDES = sys.argv[2]
HERE = os.path.dirname(os.path.abspath(__file__))
APP_SOURCE = os.path.join(HERE, "app_source")
ASSETS = os.path.join(HERE, "assets")

APP_DIR = os.path.join(TESTBED, "iOSTestbed")
BUNDLE_APP = os.path.join(APP_DIR, "app")          # bundle 里的只读 app 目录
plist_path = os.path.join(APP_DIR, "iOSTestbed-Info.plist")
APP_NAME = "股票筛选系统"
APP_BUNDLE_ID = "org.python.stockscreener"   # LiveContainer / 设置页显示的包名

# v1.7.4：版本号直接从业务本体 app.py 的 VERSION 读取，避免手工改漏、
# 让 ipa 版本号始终与 PC 端 / 启动页一致（不再固定显示 testbed 默认的 1.0）。
def _read_version():
    src = os.path.join(APP_SOURCE, "app.py")
    try:
        import re
        with open(src, "r", encoding="utf-8") as f:
            m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "1.0"

APP_VERSION = _read_version()

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

# 3) Info.plist：改 App 显示名 + 注入 ATS 例外（允许本地 HTTP，iOS App 自用）
with open(plist_path, "rb") as f:
    plist = plistlib.load(f)
plist["CFBundleDisplayName"] = APP_NAME   # 桌面图标下方显示的名字
plist["CFBundleName"] = APP_NAME           # 系统设置/任务切换里显示的名字
# v1.7.4：版本号 + 包名。LiveContainer 列表显示的「x.y.z - 包名」就取自下面两处。
plist["CFBundleShortVersionString"] = APP_VERSION   # 用户可见版本号（1.7.3）
plist["CFBundleVersion"] = APP_VERSION              # build 号，保持一致
plist["CFBundleIdentifier"] = APP_BUNDLE_ID         # org.python.stockscreener
plist["NSAppTransportSecurity"] = {
    "NSAllowsArbitraryLoads": True,
    "NSAllowsLocalNetworking": True,
}
# v1.7.2：沉浸式状态栏。WKWebView 顶到屏幕最上沿（见 SceneDelegate.m），
# v1.7.5 起页面/状态栏底色改为 OLED 纯黑 #000000，状态栏文字用白色才能看清、与页面连成一体。
# 关闭「由 VC 管理状态栏」后下面两个全局开关才生效。
plist["UIViewControllerBasedStatusBarAppearance"] = False
plist["UIStatusBarStyle"] = "UIStatusBarStyleLightContent"
plist["UIStatusBarHidden"] = False
with open(plist_path, "wb") as f:
    plistlib.dump(plist, f)
print("patched plist: name =", APP_NAME, "| version =", APP_VERSION,
      "| bundle id =", APP_BUNDLE_ID, "|", plist_path)

# 4) 安装 App 图标：testbed 图标槽位原本无 filename（故显示空白）
icon_set = os.path.join(APP_DIR, "Assets.xcassets", "AppIcon.appiconset")
icon_src = os.path.join(ASSETS, "AppIcon-1024.png")
if os.path.isdir(icon_set) and os.path.isfile(icon_src):
    import json
    shutil.copyfile(icon_src, os.path.join(icon_set, "AppIcon-1024.png"))
    contents_path = os.path.join(icon_set, "Contents.json")
    with open(contents_path, "r", encoding="utf-8") as f:
        contents = json.load(f)
    for img in contents.get("images", []):
        if img.get("idiom") == "universal" and img.get("size") == "1024x1024":
            img["filename"] = "AppIcon-1024.png"
    with open(contents_path, "w", encoding="utf-8") as f:
        json.dump(contents, f, ensure_ascii=False, indent=2)
    print("installed icon ->", icon_set, os.path.getsize(icon_src), "bytes")
else:
    print("WARN: icon slot or source not found, skipped. icon_set=", icon_set, "src=", icon_src)

print("PATCH_OK")
