# iOS 方案 B · 完整移植工程

把**桌面版已有的整套系统**（`app.py` + `web/index.html`）原样搬到 iOS，**不重写任何业务逻辑、不从头构建**。iOS 端只做一层薄壳：初始化 Python → 起本地服务 → WKWebView 加载。（安卓端已于 v1.7.0 下线，只保留 PC 与 iOS 两端。）

**进度**：✅ 最小验证（CPython + WebView）已真机跑通；✅ 完整移植代码已完成、本地行为验证通过；⏳ 等你云编译 + 真机验证抓数据。

---

## 一、原理（为什么能成）

- iOS 不能像 Android 那样靠桥接直接把 Python 打进 App，但可以把 CPython 编译成 `Python.xcframework` 嵌进 App。BeeWare 的 `Python-Apple-support` 提供**预编译好的** iOS 版（3.11/3.11+，iOS 13+），不用自己交叉编译。
- 基于官方 `testbed` 工程改造：stdlib 打包、搜索路径、`install_python` 构建脚本都由它处理好了。
- 业务逻辑与桌面版**同源同码**：`app.py` 和 `web/index.html` 逐字节复制自桌面版。移动端差异全部收敛在启动垫片里：

| 平台 | 原生壳 | 启动垫片 | 业务本体 |
|---|---|---|---|
| PC | 无（直接 `python app.py`） | 无 | `app.py` + `web/` |
| iOS | `SceneDelegate.m` | `ios_boot.py` | `app.py` + `web/`（完全复用） |

`ios_boot.py` 负责设 `NO_BROWSER`/`NIGHT_LIGHT`、装 SSL 证书兜底、切到可写目录、起 `ThreadingHTTPServer`、回传端口；`ensure()` 供回前台时保活，会真连一次 `/api/health` 再决定是否重启。与桌面版的唯一差别是 iOS 的 bundle 只读，需先把 `app.py`/`web/` 复制到沙盒 `Documents/runtime`。

---

## 二、本目录文件

| 文件 | 作用 |
|---|---|
| `app_source/app.py` | 业务本体（复制自桌面版，逐字节一致） |
| `app_source/web/index.html` | 前端页面（复制自桌面版，逐字节一致） |
| `assets/AppIcon-1024.png` | App 图标（桌面 `icon.png` 放大到 1024×1024、深色底不透明） |
| `overrides/ios_boot.py` | 启动垫片：起服务、`ensure()` 唤醒保活 |
| `overrides/SceneDelegate.m` | 原生壳：初始化 Python + 起服务 + WKWebView 铺满全屏 + 失败提示/重试 |
| `patch_project.py` | CI 里给官方 testbed 打补丁（覆盖壳、放置运行文件、注入 ATS、改 App 名为「股票筛选系统」、装图标） |
| `ios-upload.zip` | 本目录完整打包，直接解压覆盖上传到 GitHub 仓库即可 |
| `.github/workflows/build.yml` | 云编译：下载 Python 包→打补丁→archive→打包 ipa→上传 |

**PC 端同源升级。** 源码同步来源：桌面版 `app.py` 与 `web/index.html`（v1.7.5）。App 显示名「股票筛选系统」、图标与 PC 端一致。

v1.7.5 iOS 变更：启动标识升到 1.7.5；页面与桌面版同源同步（开始扫描=强制全量拉取五板块、新增刷新键用缓存刷当前板块、股票搜索/战法规则时两键置灰）。

v1.7.4 iOS 变更：启动标识升到 1.7.4；页面与桌面版同源同步（冷启动一次扫齐五板块、切板块不再重复补扫、扫描浮层可取消、按钮文案简化为「开始扫描」、界面细节修正）。

v1.7.3 iOS 变更：①一夜持股加「一夜持股 · 主板」标题，搜索 + 漏斗移到板块顶部；②ipa 版本号从 `app.py` 的 `VERSION` 动态写入 plist（不再固定 1.0），包名统一为 `org.python.stockscreener`（`patch_project.py` 与 `build.yml` 的 `PRODUCT_BUNDLE_IDENTIFIER` 同步）；③详情量比 / 主力净额由单股接口补齐。

v1.7.2 iOS 变更（已包含）：①详情在周末/节假日/盘前用上一交易日快照并弹标注（午休/收盘后不弹）；②禁 WKWebView 捏合放大；③沉浸式状态栏（WebView 顶到屏幕沿 + 白色状态栏文字 + 页面 safe-area 避让，由 `patch_project.py` 注入 plist）。

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

1. 启动页会先显示 **"股票筛选系统 iOS v1.7.5 · 正在启动…"**，随后进入完整功能界面。如果还是 "Python on iOS OK"，说明装的仍是旧 POC 包，需要重新编译 + 侧载。LiveContainer 列表应显示「1.7.5 - org.python.stockscreener」；bundle id 变更后会与旧包并存，属于正常现象。
2. 跑一次"一夜持股"扫描，**确认能抓到行情数据**（有票、有分数）。这一步验证 iOS 版 Python 的 https 能否正常访问行情接口。
3. 若有数据 → 大功告成；若扫描报"数据源失败/全是 0"，多半是 SSL 证书问题，把现象发我，我调整证书兜底（安卓已有同款处理，可控）。

---

## 四、已做的验证

- ✅ POC 真机跑通（CPython 解释器 + socket + WKWebView 三件套成立）。
- ✅ `ios_boot.py` 本地行为验证：资源复制正确、二次同步不重写、`start()` 能起真实服务、`/api/health` 返回 `1.7.5`、首页返回完整前端。
- ✅ `patch_project.py` 端到端模拟：文件到位、ATS 注入、ObjC 引用一致。
- ✅ Python/YAML 语法检查全过。

### 桌面版 → iOS 移植安全性审查（逐项）

指标算法本身**不会**因为换平台而改变：iOS 与 PC 跑的是**同一份 `app.py`**（逐字节一致），MA、量比、涨幅、换手、分时均价线、跑赢上证比例等全是纯 Python 数学，arm64 两端浮点都是 IEEE 754 double，结果一致。

真正需要关注的是「平台能力差异」，逐项核对结果：

| 检查项 | 结论 |
|---|---|
| `sys.platform` / 平台特化分支 | 全项目 **0 处**，不存在走错分支的问题 |
| 时区 / 交易日判断 | 显式 `Asia/Shanghai`，取不到时回退固定 UTC+8（A 股无夏令时，完全等价） |
| 数据目录 / 工作目录 | `BASE_DIR` 取 `app.py` 所在目录 = 沙盒 `Documents/runtime`，可写；`WEB_DIR` 同目录 |
| `subprocess` / `fork` / `multiprocessing` | **0 处**，iOS 禁进程派生不影响 |
| `webbrowser` / 打开浏览器 | 仅 `main()` 内调用，iOS 走 `import app` 不执行 `main`，且 `NO_BROWSER=1` |
| `print` / stderr 日志 | `Handler.log_message` 已重写为空操作；`print` 只在 `main()` |
| 并发 / 内存 | `NIGHT_LIGHT=1` 生效，CPU 密集线程压到 4、网络等待压到 8 |
| HTTPS / CA 证书 | 有 certifi 用它，没有则退化为不校验（只访问公开行情、不传凭据）；`http.client` 与 `urllib` 都走该 context |
| 未捕获异常 | 新增 `crash.log`（主线程 + 子线程），新增 `startup.log`（Python/OpenSSL/证书/时区） |

结论：**指标、技术分析、评分标准不存在平台相关的计算差异**。iOS 上唯一的不确定性是「行情接口能否连通」，属于网络/证书层，已做兜底；真机若出现缺数据，前端会以 `degraded_fields` 明确标注，不会静默算错。

## 五、已知限制

- **后台会被挂起**：iOS 切后台后 socket 被系统暂停，扫描时保持前台（自用可接受）。
  v1.7.0 起回前台按四层自愈：调页面 `__appResume` 补心跳 → 在 WebView 里真发 `fetch /api/health`
  → 不通就先无缓存重载页面 → 仍不通才重启 Python 服务（`ensure()` 真探活，能识别半死 socket）。
  黑屏和「服务异常」都会自动恢复，不需要手动重启 App（代价是页面可能重新加载，之前的扫描结果需重跑）。
- **数据独立**：iOS 版数据存在 App 沙盒 `Documents/runtime`，与电脑/安卓各自独立，不互通。
- **签名有效期**：免费 Apple ID 7 天，到期用 Sideloadly 重签，不用重新编译。
- **SSL 证书**：iOS 版 CPython 的 CA 根证书情况需真机确认；代码已做兜底（有 certifi 用 certifi，无则退化为不校验，仅访问公开行情接口、不传凭据）。
