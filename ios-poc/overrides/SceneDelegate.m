//
//  SceneDelegate.m
//  NightStock iOS —— 完整版启动壳（与安卓 MainActivity.startBoot 对齐）
//
//  启动流程：
//    1) bundle 里的 app.py / web/ / ios_boot.py 在只读 resourcePath/app
//    2) 初始化嵌入式 CPython（照抄官方 testbed 的 TestbedTests.m）
//    3) 调 ios_boot.start(bundleApp, runtime)：复制资源到可写 runtime、
//       起 ThreadingHTTPServer、返回实际端口
//    4) 主线程 WKWebView 加载 http://127.0.0.1:<port>/
//    5) 失败时显示错误提示 + 重试，不再是白屏
//
//  唤醒恢复（v1.6.8 起）：
//    iOS 锁屏/切后台一段时间后，系统可能回收 WKWebView 的 WebContent 进程，
//    也可能掐掉本地 HTTP 服务的连接；回前台时页面就只剩一块黑屏。
//    这里在回前台做自检：先探测页面 DOM 是否还在、本地服务是否还能响应，
//    任一项失败就自动重启 Python 服务并重新加载页面；WebContent 被回收时
//    由 webViewWebContentProcessDidTerminate: 直接重载。
//
//  Python 初始化严格照抄 BeeWare/CPython 官方 testbed；stdlib 由
//  install_python 构建脚本处理，app / app_packages 由 site.addsitedir 加入 sys.path。
//

#import "SceneDelegate.h"
#import <WebKit/WebKit.h>
#import <Python/Python.h>

// 页面背景色（与 web/index.html 的 --bg 一致），避免唤醒瞬间闪出黑/白块
static UIColor *PageBackgroundColor(void) {
    return [UIColor colorWithRed:7.0 / 255.0 green:10.0 / 255.0 blue:17.0 / 255.0 alpha:1.0];
}

// 页面存活探针：readyState + 元素数量。WebContent 被回收时这个调用会报错或不回调。
static NSString *const kPageProbeJS =
    @"(function(){try{"
    @"if(!document||!document.body)return 'NOBODY';"
    @"var n=document.body.querySelectorAll('*').length;"
    @"return (document.readyState||'?')+'/'+n;"
    @"}catch(e){return 'ERR';}})()";

@interface SceneDelegate () <WKNavigationDelegate>

@property (nonatomic, strong) WKWebView *webView;
@property (nonatomic, strong) UILabel *statusLabel;
@property (nonatomic, strong) UIButton *retryButton;
@property (nonatomic, assign) BOOL pyReady;       // CPython 是否已初始化成功
@property (nonatomic, assign) BOOL bootAttempted;  // 是否已尝试过启动（防重入）
@property (nonatomic, assign) BOOL gilReleased;    // 是否已释放 GIL（释放后主线程不能再调 Python C API）
@property (nonatomic, assign) int serverPort;      // 已启动服务的端口

@property (nonatomic, copy) NSString *bundleAppPath;  // 唤醒恢复时重启服务要用
@property (nonatomic, copy) NSString *runtimePath;
@property (nonatomic, strong) NSURL *pageURL;         // 当前应显示的地址
@property (nonatomic, assign) BOOL recovering;        // 正在恢复，防并发重入
@property (nonatomic, assign) NSInteger navFailCount; // 连续加载失败次数
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
        self.statusLabel.text = @"股票筛选系统 iOS v1.6.9 · 正在启动…";
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

// 唤醒恢复专用：调 ios_boot.ensure()，服务活着返回原端口，死了自动重启。
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
    self.navFailCount = 0;
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

// 重启/保活服务并重新加载页面
- (void)recoverNow:(NSString *)reason {
    if (self.recovering) return;
    if (!self.pyReady || self.bundleAppPath.length == 0) return;   // 启动都没成功，交给原错误界面
    self.recovering = YES;
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

// 回前台自检：页面 DOM 还在、服务还能响应，就什么都不做；否则恢复。
- (void)verifyAfterWake {
    if (!self.pyReady || self.serverPort <= 0 || self.recovering) return;
    if (self.webView.isLoading) return;      // 首次启动/正在加载，别干扰
    if (self.webView.hidden) {          // 之前停在错误页，直接重新加载
        [self recoverNow:@"页面未显示"];
        return;
    }
    // 服务探测与 DOM 探测并行，任一失败即恢复
    [self probeServerWithCompletion:^(BOOL ok) {
        if (!ok) [self recoverNow:@"本地服务无响应"];
    }];

    __block BOOL answered = NO;
    __weak typeof(self) weakSelf = self;
    [self.webView evaluateJavaScript:kPageProbeJS completionHandler:^(id result, NSError *error) {
        answered = YES;
        SceneDelegate *sself = weakSelf;
        if (!sself) return;
        NSString *s = [result isKindOfClass:[NSString class]] ? (NSString *)result : @"";
        NSArray *parts = [s componentsSeparatedByString:@"/"];
        BOOL stateOK = parts.count == 2 && [parts[0] isEqualToString:@"complete"];
        NSInteger nodes = parts.count == 2 ? [parts[1] integerValue] : 0;
        if (error || !stateOK || nodes < 20) {
            [sself recoverNow:[NSString stringWithFormat:@"页面失效(%@)", s.length ? s : @"nil"]];
        }
    }];
    // WebContent 被回收时 evaluateJavaScript 可能永远不回调，超时也走恢复
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.5 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        if (!answered) [weakSelf recoverNow:@"页面探针无响应"];
    });
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
    [vc.view addSubview:self.webView];

    [NSLayoutConstraint activateConstraints:@[
        [self.statusLabel.topAnchor constraintEqualToAnchor:vc.view.safeAreaLayoutGuide.topAnchor constant:12],
        [self.statusLabel.leadingAnchor constraintEqualToAnchor:vc.view.leadingAnchor constant:16],
        [self.statusLabel.trailingAnchor constraintEqualToAnchor:vc.view.trailingAnchor constant:-16],
        [self.retryButton.topAnchor constraintEqualToAnchor:self.statusLabel.bottomAnchor constant:8],
        [self.retryButton.centerXAnchor constraintEqualToAnchor:vc.view.centerXAnchor],
        [self.webView.topAnchor constraintEqualToAnchor:vc.view.safeAreaLayoutGuide.topAnchor],
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

@end
