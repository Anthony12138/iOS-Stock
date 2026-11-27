//
//  SceneDelegate.m
//  股票筛选系统 iOS —— 完整版启动壳（与安卓 MainActivity.startBoot 对齐）
//
//  启动流程：
//    1) bundle 里的 app.py / web/ / ios_boot.py 在只读 resourcePath/app
//    2) 初始化嵌入式 CPython（照抄官方 testbed 的 TestbedTests.m）
//    3) 调 ios_boot.start(bundleApp, runtime)：复制资源到可写 runtime、
//       起 ThreadingHTTPServer、返回实际端口
//    4) 主线程 WKWebView 加载 http://127.0.0.1:<port>/
//    5) 失败时显示错误提示 + 重试，不再是白屏
//
//  唤醒恢复（v1.6.8 起，v1.7.0 重写）：
//    iOS 锁屏/切后台一段时间后，系统可能回收 WKWebView 的 WebContent 进程，
//    也可能只掐掉「WebView → 本地服务」这一条连接 —— Python 服务进程还活着，
//    NSURLSession 也连得上 127.0.0.1，但页面里所有请求全部超时，左上角胶囊
//    一直显示「服务异常」，只能重启 App。v1.6.8 的自检只查 DOM 节点数和
//    NSURLSession /api/health，恰好落在这条链路的空档里，所以叫不醒。
//    v1.7.0 改成四层逐级自愈：
//      ① 回前台先调页面自己的 window.__appResume()（补心跳，0.8/2/4s 重试）；
//      ② 在 WebView 里真发一条 fetch /api/health（1.4s）—— 这才是「页面此刻
//         能不能连上服务」的真实判据，NSURLSession 探不到这条链路；
//      ③ 链路不通但服务进程活着 → 无缓存重载页面（换一条 WebKit 连接），
//         1.8s 后复检；
//      ④ 复检仍不通 → ios_boot.ensure() 真探活并重启服务（可能换端口），再重载。
//    WebContent 被回收时由 webViewWebContentProcessDidTerminate: 直接走 ④。
//
//  Python 初始化严格照抄 BeeWare/CPython 官方 testbed；stdlib 由
//  install_python 构建脚本处理，app / app_packages 由 site.addsitedir 加入 sys.path。
//

#import "SceneDelegate.h"
#import <WebKit/WebKit.h>
#import <Python/Python.h>

// 页面背景色（v1.7.5 OLED 纯黑：与 web/index.html 的 html.ios-native --bg 一致）。
// WebView 顶到屏幕最上沿、沉浸式状态栏，状态栏底色就是这里；必须与页面同为纯黑。
static UIColor *PageBackgroundColor(void) {
    return [UIColor blackColor];
}

// 页面存活探针：readyState + 元素数量 + 页面自己记录的心跳失败次数。
// WebContent 被回收时这个调用会报错或不回调。
static NSString *const kPageProbeJS =
    @"(function(){try{"
    @"if(!document||!document.body)return 'NOBODY';"
    @"var n=document.body.querySelectorAll('*').length;"
    @"return (document.readyState||'?')+'/'+n+'/'+(window.__svcFails||0);"
    @"}catch(e){return 'ERR';}})()";

// 链路探针：在 WebView 里真发一条 fetch（1.4 秒超时），结果写进 window.__netProbe。
// 这条请求走的是 WebKit 自己的网络栈，正是唤醒后会被掐断的那条路。
static NSString *const kNetProbeJS =
    @"(function(){try{"
    @"window.__netProbe='pending';"
    @"var c=(typeof AbortController!=='undefined')?new AbortController():null;"
    @"var t=c?setTimeout(function(){try{c.abort();}catch(e){}},1400):null;"
    @"fetch('/api/health?wake='+Date.now(),{cache:'no-store',signal:c?c.signal:undefined})"
    @".then(function(r){if(t){clearTimeout(t);}window.__netProbe=(r&&r.ok)?'ok':'bad';})"
    @".catch(function(e){if(t){clearTimeout(t);}window.__netProbe='err';});"
    @"return 'sent';}catch(e){window.__netProbe='js';return 'js';}})()";

// 回前台 / 切后台时通知页面（与安卓壳 onResume / onPause 对齐）。
// v1.7.0 之前 iOS 壳从不调这两个钩子，页面的自愈逻辑一次都没跑过。
static NSString *const kResumeJS =
    @"(function(){try{if(window.__appResume)window.__appResume();return 1;}catch(e){return 0;}})()";
static NSString *const kPauseJS =
    @"(function(){try{if(window.__appPause)window.__appPause();return 1;}catch(e){return 0;}})()";

@interface SceneDelegate () <WKNavigationDelegate>

@property (nonatomic, strong) WKWebView *webView;
@property (nonatomic, strong) UILabel *statusLabel;
@property (nonatomic, strong) UIButton *retryButton;
@property (nonatomic, assign) BOOL pyReady;        // CPython 是否已初始化成功
@property (nonatomic, assign) BOOL bootAttempted;  // 是否已尝试过启动（防重入）
@property (nonatomic, assign) BOOL gilReleased;    // 是否已释放 GIL（释放后主线程不能再调 Python C API）
@property (nonatomic, assign) int serverPort;      // 已启动服务的端口

@property (nonatomic, copy) NSString *bundleAppPath;  // 唤醒恢复时重启服务要用
@property (nonatomic, copy) NSString *runtimePath;
@property (nonatomic, strong) NSURL *pageURL;         // 当前应显示的地址
@property (nonatomic, assign) BOOL recovering;        // 正在恢复，防并发重入
@property (nonatomic, assign) NSInteger navFailCount; // 连续加载失败次数
@property (nonatomic, assign) NSInteger wakeSeq;      // 自检轮次令牌：0 表示本轮已结束
@property (nonatomic, assign) NSInteger wakeReloads;  // 冷却窗口内已自动重载次数
@property (nonatomic, assign) NSTimeInterval wakeReloadAt;
@property (nonatomic, assign) NSTimeInterval lastWakeVerifyAt; // 回前台自检去重时间戳
@end

@implementation SceneDelegate

#pragma mark - UI 状态

- (void)setStatus:(NSString *)text {
    dispatch_async(dispatch_get_main_queue(), ^{
        self.statusLabel.text = text;
    });
}

- (void)showError:(NSString *)msg detail:(NSString *)detail {
    dispatch_async(dispatch_get_main_queue(), ^{
        self.statusLabel.textColor = [UIColor systemRedColor];
        self.statusLabel.numberOfLines = 0;
        self.statusLabel.text = [NSString stringWithFormat:@"启动失败：%@\n%@", msg, detail ?: @""];
        self.statusLabel.hidden = NO;
        self.retryButton.hidden = NO;
        self.webView.hidden = YES;
    });
}

// 唤醒后恢复失败：不遮挡整个页面，只把状态条浮在上层
- (void)showWakeError:(NSString *)msg {
    dispatch_async(dispatch_get_main_queue(), ^{
        self.statusLabel.textColor = [UIColor systemRedColor];
        self.statusLabel.numberOfLines = 0;
        self.statusLabel.text = [NSString stringWithFormat:@"%@\n点「重试」重新连接本地服务", msg ?: @"唤醒后页面未能恢复"];
        self.statusLabel.hidden = NO;
        self.retryButton.hidden = NO;
        [self.retryButton.superview bringSubviewToFront:self.statusLabel];
        [self.retryButton.superview bringSubviewToFront:self.retryButton];
    });
}

- (void)showBooting {
    dispatch_async(dispatch_get_main_queue(), ^{
        self.statusLabel.hidden = NO;
        self.statusLabel.textColor = [UIColor secondaryLabelColor];
        self.statusLabel.numberOfLines = 1;
        self.statusLabel.text = @"股票筛选系统 iOS v1.8.2 · 正在启动…";
        self.retryButton.hidden = YES;
    });
}

#pragma mark - Python 初始化

// 初始化 CPython，成功返回 YES。照抄官方 testbed 的 isolated config + home + addsitedir。
- (BOOL)initPythonWithResourcePath:(NSString *)resourcePath {
    PyStatus status;
    PyPreConfig preconfig;
    PyConfig config;
    wchar_t *wtmp;

    NSString *pythonHome  = [resourcePath stringByAppendingPathComponent:@"python"];
    NSString *appPath     = [resourcePath stringByAppendingPathComponent:@"app"];
    NSString *appPkgsPath = [resourcePath stringByAppendingPathComponent:@"app_packages"];

    setenv("NO_COLOR", "1", true);
    setenv("PYTHON_COLORS", "0", true);

    PyPreConfig_InitIsolatedConfig(&preconfig);
    PyConfig_InitIsolatedConfig(&config);
    preconfig.utf8_mode = 1;        // 强制 UTF-8（行情接口含中文）
    config.buffered_stdio = 0;      // 日志即时可见
    config.write_bytecode = 0;      // bundle 签名后不可写
    config.install_signal_handlers = 1;

    status = Py_PreInitialize(&preconfig);
    if (PyStatus_Exception(status)) {
        NSLog(@"[iOS] Py_PreInitialize failed: %s", status.err_msg);
        PyConfig_Clear(&config);
        [self showError:@"解释器预初始化失败" detail:[NSString stringWithUTF8String:status.err_msg ?: ""]];
        return NO;
    }

    wtmp = Py_DecodeLocale([pythonHome UTF8String], NULL);
    status = PyConfig_SetString(&config, &config.home, wtmp);
    PyMem_RawFree(wtmp);
    if (PyStatus_Exception(status)) {
        NSLog(@"[iOS] set PYTHONHOME failed: %s", status.err_msg);
        PyConfig_Clear(&config);
        [self showError:@"设置 Python 环境失败" detail:[NSString stringWithUTF8String:status.err_msg ?: ""]];
        return NO;
    }

    status = PyConfig_Read(&config);
    if (PyStatus_Exception(status)) {
        NSLog(@"[iOS] PyConfig_Read failed: %s", status.err_msg);
        PyConfig_Clear(&config);
        [self showError:@"读取 Python 配置失败" detail:[NSString stringWithUTF8String:status.err_msg ?: ""]];
        return NO;
    }

    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) {
        NSLog(@"[iOS] Py_Initialize failed: %s", status.err_msg);
        PyConfig_Clear(&config);
        [self showError:@"Python 解释器启动失败" detail:[NSString stringWithUTF8String:status.err_msg ?: ""]];
        return NO;
    }
    PyConfig_Clear(&config);

    // site.addsitedir 把 app / app_packages 加入 sys.path（并处理 .pth）
    PyObject *site = PyImport_ImportModule("site");
    if (site) {
        PyObject *add = PyObject_GetAttrString(site, "addsitedir");
        if (add && PyCallable_Check(add)) {
            const char *paths[] = { [appPath UTF8String], [appPkgsPath UTF8String] };
            for (int i = 0; i < 2; i++) {
                PyObject *arg = Py_BuildValue("(s)", paths[i]);
                PyObject *r = PyObject_CallObject(add, arg);
                if (!r) PyErr_Print();
                Py_XDECREF(r);
                Py_XDECREF(arg);
            }
        }
        Py_XDECREF(add);
        Py_XDECREF(site);
    }

    self.pyReady = YES;
    return YES;
}

// 调 ios_boot.start(bundleApp, runtime)，返回实际端口（失败返回 -1）
- (int)startServerWithBundleApp:(NSString *)bundleApp runtime:(NSString *)runtime {
    PyObject *boot = PyImport_ImportModule("ios_boot");
    if (!boot) {
        PyErr_Print();
        [self showError:@"加载启动模块失败" detail:@"ios_boot.py 未找到，请检查工程打包"];
        return -1;
    }
    PyObject *fn = PyObject_GetAttrString(boot, "start");
    int port = -1;
    if (fn && PyCallable_Check(fn)) {
        PyObject *args = Py_BuildValue("(ss)", [bundleApp UTF8String], [runtime UTF8String]);
        PyObject *res = PyObject_CallObject(fn, args);
        if (res) {
            port = (int)PyLong_AsLong(res);
            if (PyErr_Occurred()) { PyErr_Print(); port = -1; }
            Py_DECREF(res);
        } else {
            PyErr_Print();
        }
        Py_XDECREF(args);
    }
    Py_XDECREF(fn);
    Py_XDECREF(boot);

    if (port <= 0) {
        [self showError:@"本地服务启动失败" detail:@"请看设备日志（Console）里的 Python traceback"];
    }
    return port;
}

// 唤醒恢复专用：调 ios_boot.ensure()，服务活着返回原端口，死了/半死自动重启。
// 必须在后台线程调用——GIL 已释放，主线程不再直接碰 Python C API。
- (int)ensureServerAliveWithBundleApp:(NSString *)bundleApp runtime:(NSString *)runtime {
    if (!self.pyReady || !self.gilReleased) return -1;
    int port = -1;
    PyGILState_STATE st = PyGILState_Ensure();
    PyObject *boot = PyImport_ImportModule("ios_boot");
    if (boot) {
        PyObject *fn = PyObject_GetAttrString(boot, "ensure");
        if (fn && PyCallable_Check(fn)) {
            PyObject *args = Py_BuildValue("(ss)", [bundleApp UTF8String], [runtime UTF8String]);
            PyObject *res = PyObject_CallObject(fn, args);
            if (res) {
                port = (int)PyLong_AsLong(res);
                if (PyErr_Occurred()) { PyErr_Print(); port = -1; }
                Py_DECREF(res);
            } else {
                PyErr_Print();
            }
            Py_XDECREF(args);
        }
        Py_XDECREF(fn);
        Py_XDECREF(boot);
    } else {
        PyErr_Print();
    }
    PyGILState_Release(st);
    return port;
}

#pragma mark - 页面加载

- (void)loadPageAtPort:(int)port forceReload:(BOOL)force {
    if (port <= 0) return;
    NSString *urlStr = [NSString stringWithFormat:@"http://127.0.0.1:%d/", port];
    self.pageURL = [NSURL URLWithString:urlStr];
    // v1.7.5 修复：不要在这里清零 navFailCount。失败重试也走本方法，
    // 清零会让 didFail 里的计数永远回到 1，3 次上限形同虚设（无限重载）。
    // 只有 didFinishNavigation 真成功、或用户点「重试」/recover 新开一轮时才清零。
    dispatch_async(dispatch_get_main_queue(), ^{
        self.statusLabel.hidden = YES;   // 成功后隐藏状态栏，网页铺满全屏
        self.retryButton.hidden = YES;
        self.webView.hidden = NO;
        NSURLRequestCachePolicy policy = force ? NSURLRequestReloadIgnoringLocalCacheData
                                               : NSURLRequestUseProtocolCachePolicy;
        NSURLRequest *req = [NSURLRequest requestWithURL:self.pageURL
                                             cachePolicy:policy
                                         timeoutInterval:30];
        [self.webView loadRequest:req];
    });
}

- (void)loadPort:(int)port {
    [self loadPageAtPort:port forceReload:NO];
}

// 只重载页面，不重启服务
- (void)reloadPageForce:(BOOL)force {
    if (self.serverPort > 0) {
        [self loadPageAtPort:self.serverPort forceReload:force];
        return;
    }
    if (self.pageURL) {
        dispatch_async(dispatch_get_main_queue(), ^{
            self.statusLabel.hidden = YES;
            self.retryButton.hidden = YES;
            self.webView.hidden = NO;
            [self.webView loadRequest:[NSURLRequest requestWithURL:self.pageURL]];
        });
    }
}

// 完整启动入口
- (void)bootPython {
    if (self.bootAttempted) return;
    self.bootAttempted = YES;
    [self showBooting];

    NSString *resourcePath = [[NSBundle mainBundle] resourcePath];
    NSString *bundleApp    = [resourcePath stringByAppendingPathComponent:@"app"];

    // 可写 runtime：Documents/runtime（iOS 沙盒内可写，卸载 App 才清）
    NSArray *docs = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES);
    NSString *runtime = [[docs firstObject] stringByAppendingPathComponent:@"runtime"];
    self.bundleAppPath = bundleApp;
    self.runtimePath = runtime;

    if (!self.pyReady) {
        if (![self initPythonWithResourcePath:resourcePath]) return;
    }

    NSLog(@"[iOS] Starting app.py (bundle=%@, runtime=%@)", bundleApp, runtime);
    int port = [self startServerWithBundleApp:bundleApp runtime:runtime];
    if (port <= 0) return;

    // 释放 GIL，否则 http.server 的 Python 线程拿不到 GIL 无法响应请求。
    // 注意：释放后主线程不能再调用任何 Python C API，唤醒恢复改在后台线程
    // 用 PyGILState_Ensure 重新取 GIL。
    if (!self.gilReleased) {
        PyEval_SaveThread();
        self.gilReleased = YES;
    }
    self.serverPort = port;

    [self loadPort:port];
}

- (void)retryTapped {
    self.wakeSeq = 0;
    self.navFailCount = 0;   // 用户主动重试＝新开一轮，失败计数从头算
    // 服务若已在跑（GIL 已释放），只重载页面，绝不再碰 Python C API。
    if (self.gilReleased && self.serverPort > 0) {
        [self reloadPageForce:YES];
        return;
    }
    self.bootAttempted = NO;
    [self bootPython];
}

#pragma mark - 唤醒恢复

// 自查本地服务是否还能响应（2.5 秒超时）。服务被系统回收时这里会失败。
// 注意：这一路走的是 NSURLSession，和 WebView 不是同一条连接，
// 所以它只能证明「Python 服务进程活着」，不能证明页面连得上。
- (void)probeServerWithCompletion:(void (^)(BOOL ok))done {
    if (self.serverPort <= 0) { done(NO); return; }
    NSURL *u = [NSURL URLWithString:[NSString stringWithFormat:@"http://127.0.0.1:%d/api/health", self.serverPort]];
    NSMutableURLRequest *req = [NSMutableURLRequest requestWithURL:u];
    req.timeoutInterval = 2.5;
    req.cachePolicy = NSURLRequestReloadIgnoringLocalCacheData;
    __block BOOL finished = NO;
    void (^finish)(BOOL) = ^(BOOL ok) {
        if (finished) return;
        finished = YES;
        dispatch_async(dispatch_get_main_queue(), ^{ done(ok); });
    };
    [[[NSURLSession sharedSession] dataTaskWithRequest:req
                                     completionHandler:^(NSData *data, NSURLResponse *resp, NSError *err) {
        NSInteger code = [resp isKindOfClass:[NSHTTPURLResponse class]] ? ((NSHTTPURLResponse *)resp).statusCode : 0;
        finish(err == nil && code == 200);
    }] resume];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(3.0 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        finish(NO);   // 连不上就别等了，直接恢复
    });
}

// 第 ④ 层：重启/保活服务并重新加载页面
- (void)recoverNow:(NSString *)reason {
    if (self.recovering) return;
    if (!self.pyReady || self.bundleAppPath.length == 0) return;   // 启动都没成功，交给原错误界面
    self.recovering = YES;
    self.wakeSeq = 0;
    self.navFailCount = 0;   // 重新加载是一轮新尝试，失败计数从头算
    NSLog(@"[iOS] wake recovery triggered: %@", reason);

    NSString *bundleApp = self.bundleAppPath;
    NSString *runtime   = self.runtimePath;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        int port = [self ensureServerAliveWithBundleApp:bundleApp runtime:runtime];
        dispatch_async(dispatch_get_main_queue(), ^{
            self.recovering = NO;
            if (port > 0) {
                self.serverPort = port;
                NSLog(@"[iOS] recovered on port %d", port);
                [self loadPageAtPort:port forceReload:YES];
            } else {
                [self showWakeError:@"本地服务未能重启"];
            }
        });
    });
}

// 回前台自检入口
- (void)verifyAfterWake {
    // v1.7.5：UIApplication 通知与 Scene 生命周期在回前台时会各触发一次，
    // 1s 内去重，避免两轮自检互相 ++wakeSeq 打断（令牌兜底虽能防误动作，仍白白探测两次）。
    NSTimeInterval now = [NSDate timeIntervalSinceReferenceDate];
    if (now - self.lastWakeVerifyAt < 1.0) return;
    self.lastWakeVerifyAt = now;
    [self wakeCheck:1 allowLoading:NO];
}

// 第 ① ② 层：DOM 探针 → 通知页面补心跳 → 链路 fetch 探针
- (void)wakeCheck:(NSInteger)round allowLoading:(BOOL)allowLoading {
    if (!self.pyReady || self.serverPort <= 0 || self.recovering) return;
    if (self.webView.hidden) { [self recoverNow:@"页面未显示"]; return; }
    if (self.webView.isLoading && !allowLoading) return;   // 首次启动/正在加载，别干扰

    NSInteger seq = ++self.wakeSeq;
    __weak typeof(self) weakSelf = self;

    // WebContent 被回收时 evaluateJavaScript 可能永远不回调，超时也走恢复
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.6 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        SceneDelegate *ss = weakSelf;
        if (!ss || ss.wakeSeq != seq) return;
        ss.wakeSeq = 0;
        [ss recoverNow:@"页面探针无响应"];
    });

    [self.webView evaluateJavaScript:kPageProbeJS completionHandler:^(id result, NSError *error) {
        SceneDelegate *ss = weakSelf;
        if (!ss || ss.wakeSeq != seq) return;          // 已被超时兜底接管
        NSString *s = [result isKindOfClass:[NSString class]] ? (NSString *)result : @"";
        NSArray *parts = [s componentsSeparatedByString:@"/"];
        BOOL stateOK = parts.count >= 2 && [parts[0] isEqualToString:@"complete"];
        NSInteger nodes = parts.count >= 2 ? [parts[1] integerValue] : 0;
        if (error || !stateOK || nodes < 20) {
            ss.wakeSeq = 0;
            [ss recoverNow:[NSString stringWithFormat:@"页面失效(%@)", s.length ? s : @"nil"]];
            return;
        }
        // ① 先让页面自己补一轮心跳（安卓壳一直是这么调的，iOS 之前漏了）
        [ss.webView evaluateJavaScript:kResumeJS completionHandler:nil];
        // ② 再验「WebView → 本地服务」这条路。
        // v1.7.5 修复：换一个新令牌，让第①层的 1.6s 超时兜底立即失效——
        // 链路探针本身要等 1.7s 才读结果，若两层共用 seq，超时块必然先触发，
        // 健康状态回前台也会被误判成「页面探针无响应」而强刷。
        NSInteger netSeq = ++ss.wakeSeq;
        [ss runNetProbeOnRound:round seq:netSeq];
    }];
}

// 第 ② 层：在页面里 fetch /api/health，1.7 秒后读结果
- (void)runNetProbeOnRound:(NSInteger)round seq:(NSInteger)seq {
    __weak typeof(self) weakSelf = self;
    [self.webView evaluateJavaScript:kNetProbeJS completionHandler:nil];
    // 第二层独立兜底：WebContent 卡住导致 evaluateJavaScript 永不回调时，
    // 6s 后强制恢复（覆盖 3s 服务探活 + 1.8s 重载窗口）。令牌只在本阶段有效。
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(6.0 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        SceneDelegate *ss = weakSelf;
        if (!ss || ss.wakeSeq != seq) return;
        ss.wakeSeq = 0;
        [ss recoverNow:@"链路探针超时"];
    });
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.7 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        SceneDelegate *ss = weakSelf;
        if (!ss || ss.wakeSeq != seq) return;
        [ss.webView evaluateJavaScript:@"(window.__netProbe||'')"
                     completionHandler:^(id r, NSError *e) {
            SceneDelegate *s2 = weakSelf;
            if (!s2 || s2.wakeSeq != seq) return;
            NSString *v = [r isKindOfClass:[NSString class]] ? (NSString *)r : @"";
            if ([v isEqualToString:@"ok"]) {
                s2.wakeSeq = 0;
                NSLog(@"[iOS] wake check OK (round %ld)", (long)round);
                return;
            }
            NSLog(@"[iOS] wake check failed (round %ld, net=%@)", (long)round, v.length ? v : @"nil");
            [s2 handleBrokenLinkOnRound:round seq:seq];
        }];
    });
}

// 第 ③ ④ 层：先判断服务进程死活，再决定「重载页面」还是「重启服务」
- (void)handleBrokenLinkOnRound:(NSInteger)round seq:(NSInteger)seq {
    __weak typeof(self) weakSelf = self;
    if (round >= 2) {                      // 重载过一页还是不通 → 换端口重启服务
        self.wakeSeq = 0;
        [self recoverNow:@"本地服务链路无响应"];
        return;
    }
    [self probeServerWithCompletion:^(BOOL ok) {
        SceneDelegate *ss = weakSelf;
        if (!ss || ss.wakeSeq != seq) return;
        if (!ok) {                         // 服务进程本身死了，别浪费一次重载
            ss.wakeSeq = 0;
            [ss recoverNow:@"本地服务无响应"];
            return;
        }
        NSTimeInterval now = [NSDate timeIntervalSinceReferenceDate];
        if (ss.wakeReloads > 0 && now - ss.wakeReloadAt > 600) ss.wakeReloads = 0;
        if (ss.wakeReloads >= 3) {         // 冷却窗口内已连着重载 3 次，交给用户
            ss.wakeSeq = 0;
            [ss showWakeError:@"本地服务链路反复中断"];
            return;
        }
        ss.wakeReloads += 1;
        ss.wakeReloadAt = now;
        ss.wakeSeq = 0;                    // 本轮结束，重载后重新开一轮
        NSLog(@"[iOS] link broken but server alive, reloading page (%ld/3)", (long)ss.wakeReloads);
        [ss reloadPageForce:YES];
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.8 * NSEC_PER_SEC)),
                       dispatch_get_main_queue(), ^{ [ss wakeCheck:2 allowLoading:YES]; });
    }];
}

#pragma mark - WKNavigationDelegate

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    self.navFailCount = 0;
    dispatch_async(dispatch_get_main_queue(), ^{
        self.statusLabel.hidden = YES;
        self.retryButton.hidden = YES;
    });
}

- (void)webView:(WKWebView *)webView didFailProvisionalNavigation:(WKNavigation *)navigation withError:(NSError *)error {
    if (error.code == NSURLErrorCancelled) return;   // 主动打断不算失败
    self.navFailCount += 1;
    NSLog(@"[iOS] navigation failed (%ld): %@", (long)self.navFailCount, error.localizedDescription);
    if (self.navFailCount <= 2) {
        // 本地服务刚重启时可能还没监听，重试两次
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.8 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            [self reloadPageForce:YES];
        });
    } else {
        [self showWakeError:@"本地页面加载失败"];
    }
}

- (void)webView:(WKWebView *)webView didFailNavigation:(WKNavigation *)navigation withError:(NSError *)error {
    if (error.code == NSURLErrorCancelled) return;
    self.navFailCount += 1;
    if (self.navFailCount > 2) [self showWakeError:@"本地页面加载失败"];
}

// WebContent 进程被系统回收（锁屏后最常见）：直接重载，必要时顺带重启服务
- (void)webViewWebContentProcessDidTerminate:(WKWebView *)webView {
    NSLog(@"[iOS] WebContent process terminated, reloading");
    self.wakeSeq = 0;
    [self recoverNow:@"WebContent 被回收"];
}

#pragma mark - 生命周期

- (void)scene:(UIScene *)scene willConnectToSession:(UISceneSession *)session options:(UISceneConnectionOptions *)connectionOptions {
    UIWindowScene *windowScene = (UIWindowScene *)scene;
    self.window = [[UIWindow alloc] initWithWindowScene:windowScene];

    UIViewController *vc = [[UIViewController alloc] init];
    vc.view.backgroundColor = PageBackgroundColor();

    self.statusLabel = [[UILabel alloc] init];
    self.statusLabel.translatesAutoresizingMaskIntoConstraints = NO;
    self.statusLabel.text = @"正在启动…";
    self.statusLabel.textColor = [UIColor secondaryLabelColor];
    self.statusLabel.font = [UIFont systemFontOfSize:13];
    self.statusLabel.textAlignment = NSTextAlignmentCenter;
    [vc.view addSubview:self.statusLabel];

    self.retryButton = [UIButton buttonWithType:UIButtonTypeSystem];
    self.retryButton.translatesAutoresizingMaskIntoConstraints = NO;
    [self.retryButton setTitle:@"重试" forState:UIControlStateNormal];
    [self.retryButton addTarget:self action:@selector(retryTapped) forControlEvents:UIControlEventTouchUpInside];
    self.retryButton.hidden = YES;
    [vc.view addSubview:self.retryButton];

    WKWebViewConfiguration *cfg = [[WKWebViewConfiguration alloc] init];
    self.webView = [[WKWebView alloc] initWithFrame:CGRectZero configuration:cfg];
    self.webView.translatesAutoresizingMaskIntoConstraints = NO;
    self.webView.hidden = YES;
    self.webView.navigationDelegate = self;
    // 透明 + 页面同色底：唤醒瞬间不会闪白/黑块
    self.webView.opaque = NO;
    self.webView.backgroundColor = PageBackgroundColor();
    self.webView.scrollView.backgroundColor = PageBackgroundColor();
    // v1.7.2：禁捏合放大。页面 viewport 已写 user-scalable=no，这里再把原生
    // UIScrollView 的缩放范围锁成 1x，双指手势/双击放大都失效（详情页仍能正常上下滚）。
    self.webView.scrollView.minimumZoomScale = 1.0;
    self.webView.scrollView.maximumZoomScale = 1.0;
    self.webView.scrollView.zoomScale = 1.0;
    self.webView.scrollView.bouncesZoom = NO;
    [vc.view addSubview:self.webView];

    [NSLayoutConstraint activateConstraints:@[
        [self.statusLabel.topAnchor constraintEqualToAnchor:vc.view.safeAreaLayoutGuide.topAnchor constant:12],
        [self.statusLabel.leadingAnchor constraintEqualToAnchor:vc.view.leadingAnchor constant:16],
        [self.statusLabel.trailingAnchor constraintEqualToAnchor:vc.view.trailingAnchor constant:-16],
        [self.retryButton.topAnchor constraintEqualToAnchor:self.statusLabel.bottomAnchor constant:8],
        [self.retryButton.centerXAnchor constraintEqualToAnchor:vc.view.centerXAnchor],
        // v1.7.2：沉浸式状态栏——WebView 顶到屏幕最上沿，页面背景 / 顶栏自己用
        // env(safe-area-inset-top) 躲开状态栏（见 web/index.html），状态栏文字改白色
        // （见 patch_project.py 注入的 plist）。这样状态栏与页面同色、不再是黑条。
        [self.webView.topAnchor constraintEqualToAnchor:vc.view.topAnchor],
        [self.webView.leadingAnchor constraintEqualToAnchor:vc.view.leadingAnchor],
        [self.webView.trailingAnchor constraintEqualToAnchor:vc.view.trailingAnchor],
        [self.webView.bottomAnchor constraintEqualToAnchor:vc.view.bottomAnchor],
    ]];
    // 网页铺满后，状态标签/重试按钮作为浮层叠在上层（仅启动中或失败时可见）
    [vc.view bringSubviewToFront:self.statusLabel];
    [vc.view bringSubviewToFront:self.retryButton];

    self.window.rootViewController = vc;
    [self.window makeKeyAndVisible];

    // 回前台自检：锁屏/切后台久了，页面或本地服务可能已被系统回收
    NSNotificationCenter *nc = [NSNotificationCenter defaultCenter];
    [nc addObserver:self selector:@selector(appWillEnterForeground) name:UIApplicationWillEnterForegroundNotification object:nil];
    [nc addObserver:self selector:@selector(appDidBecomeActive) name:UIApplicationDidBecomeActiveNotification object:nil];
    [nc addObserver:self selector:@selector(appWillResignActive) name:UIApplicationWillResignActiveNotification object:nil];

    // 延迟启动，确保 UI 已渲染
    [self performSelector:@selector(bootPython) withObject:nil afterDelay:0.2];
}

- (void)dealloc {
    [[NSNotificationCenter defaultCenter] removeObserver:self];
}

- (void)appWillEnterForeground {
    // 唤醒瞬间 WebKit 还在重建渲染层，稍等再探
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.35 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        [self.webView setNeedsDisplay];
        [self.webView.scrollView setNeedsDisplay];
    });
}

- (void)appDidBecomeActive {
    // 首次启动时 bootPython 还没跑完，这时的自检没有意义，交给启动流程
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.6 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        [self verifyAfterWake];
    });
}

// 切后台时让页面停掉定时器（与安卓 onPause 对齐），回前台 __appResume 才能正确重建
- (void)appWillResignActive {
    if (!self.pyReady || self.webView.hidden) return;
    [self.webView evaluateJavaScript:kPauseJS completionHandler:nil];
}

- (void)sceneDidBecomeActive:(UIScene *)scene {
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.8 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        [self verifyAfterWake];
    });
}

- (void)sceneWillEnterForeground:(UIScene *)scene {
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.3 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        [self.webView setNeedsDisplay];
    });
}

- (void)sceneDidEnterBackground:(UIScene *)scene {
    if (!self.pyReady || self.webView.hidden) return;
    [self.webView evaluateJavaScript:kPauseJS completionHandler:nil];
}

@end
