# iOS 方案 B · 最小验证工程

目的：不碰现有 exe/apk 项目，单独验证「Python 解释器能否在 iOS 上跑起来 + WebView 能否加载本地 Python 服务」这条链路。

**验证成功的标志**：打开 App 能看到一个写着 “Python on iOS OK” 的网页。看到它，就证明可以把完整的 `app.py` 移植进 iOS（方案 B 成立）；看不到就按日志排查，成本可控。

---

## 一、原理（为什么这样能成）

- iOS 不能像安卓那样用 Chaquopy 直接塞 Python，但可以**把 CPython 编译成 `Python.xcframework` 嵌进 App**，用官方嵌入 API 启动解释器。
- BeeWare 的 `Python-Apple-support` 提供**预编译好的** iOS 版 `Python.xcframework`（3.11/3.12/3.13，iOS 13+），不用自己交叉编译——这是最大的风险点，官方已经帮我们解决了。
- 我们基于官方的 `testbed` 工程改造：它已经处理好了 stdlib 打包、搜索路径、`install_python` 构建脚本等所有繁琐细节。
- 只改两处：① `SceneDelegate.m`——初始化 Python 后启动一个 `http.server`；② 用 WKWebView 加载 `http://127.0.0.1:8765/`。

---

## 二、我先做了什么（本目录内容）

| 文件 | 作用 |
|---|---|
| `overrides/SceneDelegate.m` | 改造版：初始化 CPython + 后台起 http.server + WKWebView 加载 localhost |
| `overrides/poc_server.py` | 极简验证服务，返回 “Python on iOS OK” 页面 |
| `patch_project.py` | CI 里给官方 testbed 打补丁（覆盖代码 + 注入 ATS 允许 HTTP） |
| `.github/workflows/build.yml` | GitHub Actions 云编译配置：下载 Python 包→打补丁→archive→打包 ipa→上传 |

**现有 exe/apk 项目一行没动。**

---

## 三、你要做的（全程不需要 Mac）

### 第 0 步：准备 GitHub 账号
没有就注册一个（github.com，免费）。公开仓库的 Actions 完全免费；私有仓库每月也有免费额度，够用。

### 第 1 步：建仓库并上传本目录
1. GitHub 右上角 `New repository`，名字随意（如 `ios-poc`），建议选 **Public**（Actions 免费且不受额度限制）。
2. 把本 `ios-poc` 目录里的内容上传到仓库根目录（含隐藏的 `.github/workflows/build.yml`）。
   - 网页上传：把文件拖进仓库；注意 `.github` 这个隐藏文件夹要一起传。
   - 或命令行：`cd ios-poc && git init && git add . && git commit -m "ios poc" && git remote add origin <仓库地址> && git push -u origin main`

### 第 2 步：触发编译
1. 仓库页面点顶部 `Actions` 标签。
2. 左侧选 `Build iOS Python POC (ipa)` → 右侧 `Run workflow` → 再点绿色 `Run workflow`。
3. 等 5~15 分钟（首次会慢些）。点进这次运行，能实时看日志。
4. 编译成功后，页面底部 `Artifacts` 里有 `NightStock-iOS-POC`，下载解压得到 `NightStock-iOS-POC.ipa`。

> **第一次大概率要调试**：我没有 Mac，无法在本地编译验证，workflow 是按官方标准写的，但 Xcode/签名细节可能有坑。如果编译失败，把 Actions 的报错日志复制给我，我按日志改，通常几轮内能过。

### 第 3 步：侧载到手机（Windows 上做）
推荐 **Sideloadly**（免费，支持 Windows）：
1. 电脑装 Sideloadly（sideloadly.io）。
2. iPhone 用数据线连电脑，信任此电脑。
3. Sideloadly 里：选刚下载的 ipa → 填你的 Apple ID（用于签名）→ 点 `Start`。
   - 用你个人开发者账号登录，签名可长期有效；免费 Apple ID 则 7 天需重签一次。
4. 手机 `设置 → 通用 → VPN与设备管理` 里信任证书，然后打开 App。

> 也可以用 AltStore、爱思助手等，流程类似。

---

## 四、成功之后（下一步）

看到 “Python on iOS OK” 后，说明方案 B 的底座成立。下一步我会：
1. 把 `app.py` 和 `web/index.html` 打包进 App 的 `app/` 目录。
2. 把 `SceneDelegate.m` 的启动脚本从 `poc_server` 换成 `app.main`（并适配数据目录到 App 沙盒、固定端口、去掉浏览器启动、处理 iOS 后台挂起）。
3. 完整功能迁移 + 再编译验证。

到时你就能在 iPhone 上用到和现在电脑/安卓一样的功能，且数据仍是手机直连数据源、不上云。

---

## 五、已知限制

- **后台会被挂起**：iOS 切到后台后 socket 会被系统暂停，扫描中要保持在前台。自用可接受。
- **App 体积**：内嵌 CPython，约 40~60MB，属正常。
- **签名**：侧载签名有有效期（免费 Apple ID 7 天），到期用 Sideloadly 重签即可，不用重新编译。
