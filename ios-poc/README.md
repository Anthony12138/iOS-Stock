# iOS 方案 B · 完整移植工程

把**安卓端已有的整套系统**（`app.py` + `web/index.html`）原样搬到 iOS，**不重写任何业务逻辑、不从头构建**。iOS 端只做一层薄壳：初始化 Python → 起本地服务 → WKWebView 加载，和安卓的 `MainActivity` + `android_boot.py` 架构一一对应。

**进度**：✅ 最小验证（CPython + WebView）已真机跑通；✅ 完整移植代码已完成、本地行为验证通过；⏳ 等你云编译 + 真机验证抓数据。

---

## 一、原理（为什么能成）

- iOS 不能像安卓用 Chaquopy，但可以把 CPython 编译成 `Python.xcframework` 嵌进 App。BeeWare 的 `Python-Apple-support` 提供**预编译好的** iOS 版（3.11/3.11+，iOS 13+），不用自己交叉编译。
- 基于官方 `testbed` 工程改造：stdlib 打包、搜索路径、`install_python` 构建脚本都由它处理好了。
- 业务逻辑与安卓**同源同码**：`app.py` 和 `web/index.html` 逐字节复制自桌面版（和安卓 `sync_assets.py` 的同步来源一致）。移动端差异全部收敛在启动垫片里：

| 平台 | 原生壳 | 启动垫片 | 业务本体 |
|---|---|---|---|
| 安卓 | `MainActivity.java` | `android_boot.py` | `app.py` + `web/` |
| iOS | `SceneDelegate.m` | `ios_boot.py` | `app.py` + `web/`（完全复用） |

`ios_boot.py` 与 `android_boot.py` 逻辑一一对应：设 `NO_BROWSER`/`NIGHT_LIGHT`、装 SSL 证书兜底、切到可写目录、起 `ThreadingHTTPServer`、回传端口。唯一差别是 iOS 的 bundle 只读，需先把 `app.py`/`web/` 复制到沙盒 `Documents/runtime`。

---

## 二、本目录文件

| 文件 | 作用 |
|---|---|
| `app_source/app.py` | 业务本体（复制自桌面版，逐字节一致） |
| `app_source/web/index.html` | 前端页面（复制自桌面版，逐字节一致） |
| `overrides/ios_boot.py` | 启动垫片（对应安卓 `android_boot.py`） |
| `overrides/SceneDelegate.m` | 原生壳：初始化 Python + 起服务 + WKWebView + 失败提示/重试 |
| `patch_project.py` | CI 里给官方 testbed 打补丁（覆盖壳、放置运行文件、注入 ATS） |
| `.github/workflows/build.yml` | 云编译：下载 Python 包→打补丁→archive→打包 ipa→上传 |

**现有 exe/apk 项目一行没动。** 源码同步来源：桌面版 `app.py` 与 `web/index.html`（v1.6.5）。

---

## 三、你要做的（全程不需要 Mac）

### 第 1 步：把本目录上传到 GitHub 仓库

仓库：`Anthony12138/iOS-Stock`（已建好）。需要保证仓库里这几个都在：

```
.github/workflows/build.yml   ← 根目录，Actions 只认这里
ios-poc/
  ├─ README.md
  ├─ patch_project.py
  ├─ app_source/
  │    ├─ app.py
  │    └─ web/index.html
  └─ overrides/
       ├─ SceneDelegate.m
       └─ ios_boot.py
```

> **本次更新新增了 `app_source/` 整个目录和两个改动文件**。网页拖拽上传会忽略点开头的 `.github`（已传好不用动），但 `app_source/` 是普通目录，正常拖进去即可。用 Git 命令行最省事：
> ```
> git add -A && git commit -m "full migration" && git push
> ```

### 第 2 步：触发编译

1. 仓库 `Actions` → 左侧 `Build iOS Python POC (ipa)` → `Run workflow` → 绿色 `Run workflow`。
2. 等 5~15 分钟。成功后在 run 页面点左侧 `Summary`，拉到底部 `Artifacts` → `NightStock-iOS-POC` → 下载解压得 `NightStock-iOS-POC.ipa`（有效期 30 天，及时下）。
3. 失败就把红色日志贴给我，按日志改。

### 第 3 步：侧载到 iPhone

1. Windows 装 **Sideloadly**（sideloadly.io，免费）。
2. iPhone 数据线连电脑，点"信任此电脑"。
3. Sideloadly 选 ipa → 填 Apple ID → `Start`。个人开发者账号签名可长期有效；免费 Apple ID 7 天需重签。
4. 手机 `设置 → 通用 → VPN与设备管理` 信任证书，打开 App。

### 第 4 步：验证（关键）

1. 启动页会先显示 **"股票筛选系统 iOS v1.6.5 · 正在启动…"**，随后进入完整功能界面。如果还是 "Python on iOS OK"，说明装的仍是旧 POC 包，需要重新编译 + 侧载。
2. 跑一次"一夜持股"扫描，**确认能抓到行情数据**（有票、有分数）。这一步验证 iOS 版 Python 的 https 能否正常访问行情接口。
3. 若有数据 → 大功告成；若扫描报"数据源失败/全是 0"，多半是 SSL 证书问题，把现象发我，我调整证书兜底（安卓已有同款处理，可控）。

---

## 四、已做的验证

- ✅ POC 真机跑通（CPython 解释器 + socket + WKWebView 三件套成立）。
- ✅ `ios_boot.py` 本地行为验证：资源复制正确、二次同步不重写、`start()` 能起真实服务、`/api/health` 返回 `1.6.5`、首页返回完整前端。
- ✅ `patch_project.py` 端到端模拟：文件到位、ATS 注入、ObjC 引用一致。
- ✅ Python/YAML 语法检查全过。

## 五、已知限制

- **后台会被挂起**：iOS 切后台后 socket 被系统暂停，扫描时保持前台（自用可接受）。
- **数据独立**：iOS 版数据存在 App 沙盒 `Documents/runtime`，与电脑/安卓各自独立，不互通。
- **签名有效期**：免费 Apple ID 7 天，到期用 Sideloadly 重签，不用重新编译。
- **SSL 证书**：iOS 版 CPython 的 CA 根证书情况需真机确认；代码已做兜底（有 certifi 用 certifi，无则退化为不校验，仅访问公开行情接口、不传凭据）。
