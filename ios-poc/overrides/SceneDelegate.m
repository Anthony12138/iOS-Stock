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
//  Python 初始化严格照抄 BeeWare/CPython 官方 testbed；stdlib 由
//  install_python 构建脚本处理，app / app_packages 由 site.addsitedir 加入 sys.path。
//

#import "SceneDelegate.h"
#import <WebKit/WebKit.h>
#import <Python/Python.h>

@interface SceneDelegate ()
@property (nonatomic, strong) WKWebView *webView;
@property (nonatomic, strong) UILabel *statusLabel;
@property (nonatomic, strong) UIButton *retryButton;
@property (nonatomic, assign) BOOL pyReady;      // CPython 是否已初始化成功
@property (nonatomic, assign) BOOL bootAttempted; // 是否已尝试过启动（防重入）
@property (nonatomic, assign) BOOL gilReleased;   // 是否已释放 GIL（释放后主线程不能再调 Python C API）
@property (nonatomic, assign) int serverPort;     // 已启动服务的端口
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
        self.retryButton.hidden = NO;
        self.webView.hidden = YES;
    });
}

- (void)showBooting {
    dispatch_async(dispatch_get_main_queue(), ^{
        self.statusLabel.textColor = [UIColor secondaryLabelColor];
        self.statusLabel.numberOfLines = 1;
        self.statusLabel.text = @"股票筛选系统 iOS v1.6.5 · 正在启动…";
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

- (void)loadPort:(int)port {
    NSString *urlStr = [NSString stringWithFormat:@"http://127.0.0.1:%d/", port];
    NSURL *url = [NSURL URLWithString:urlStr];
    dispatch_async(dispatch_get_main_queue(), ^{
        [self.statusLabel setText:[NSString stringWithFormat:@"股票筛选系统 iOS v1.6.5 · 就绪 · %@", urlStr]];
        self.webView.hidden = NO;
        [self.webView loadRequest:[NSURLRequest requestWithURL:url]];
    });
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

    if (!self.pyReady) {
        if (![self initPythonWithResourcePath:resourcePath]) return;
    }

    NSLog(@"[iOS] Starting app.py (bundle=%@, runtime=%@)", bundleApp, runtime);
    int port = [self startServerWithBundleApp:bundleApp runtime:runtime];
    if (port <= 0) return;

    // 释放 GIL，否则 http.server 的 Python 线程拿不到 GIL 无法响应请求。
    // 注意：释放后主线程不能再调用任何 Python C API，重试只重载页面即可。
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
        // 服务已在跑，直接重载 URL，不碰 Python C API
        NSString *urlStr = [NSString stringWithFormat:@"http://127.0.0.1:%d/", self.serverPort];
        NSURL *url = [NSURL URLWithString:urlStr];
        dispatch_async(dispatch_get_main_queue(), ^{
            self.statusLabel.textColor = [UIColor secondaryLabelColor];
            self.statusLabel.text = [NSString stringWithFormat:@"分析引擎就绪 · %@", urlStr];
            self.retryButton.hidden = YES;
            self.webView.hidden = NO;
            [self.webView loadRequest:[NSURLRequest requestWithURL:url]];
        });
        return;
    }
    self.bootAttempted = NO;
    [self bootPython];
}

#pragma mark - 生命周期

- (void)scene:(UIScene *)scene willConnectToSession:(UISceneSession *)session options:(UISceneConnectionOptions *)connectionOptions {
    UIWindowScene *windowScene = (UIWindowScene *)scene;
    self.window = [[UIWindow alloc] initWithWindowScene:windowScene];

    UIViewController *vc = [[UIViewController alloc] init];
    vc.view.backgroundColor = [UIColor systemBackgroundColor];

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
    [vc.view addSubview:self.webView];

    [NSLayoutConstraint activateConstraints:@[
        [self.statusLabel.topAnchor constraintEqualToAnchor:vc.view.safeAreaLayoutGuide.topAnchor constant:12],
        [self.statusLabel.leadingAnchor constraintEqualToAnchor:vc.view.leadingAnchor constant:16],
        [self.statusLabel.trailingAnchor constraintEqualToAnchor:vc.view.trailingAnchor constant:-16],
        [self.retryButton.topAnchor constraintEqualToAnchor:self.statusLabel.bottomAnchor constant:8],
        [self.retryButton.centerXAnchor constraintEqualToAnchor:vc.view.centerXAnchor],
        [self.webView.topAnchor constraintEqualToAnchor:self.retryButton.bottomAnchor constant:8],
        [self.webView.leadingAnchor constraintEqualToAnchor:vc.view.leadingAnchor],
        [self.webView.trailingAnchor constraintEqualToAnchor:vc.view.trailingAnchor],
        [self.webView.bottomAnchor constraintEqualToAnchor:vc.view.bottomAnchor],
    ]];

    self.window.rootViewController = vc;
    [self.window makeKeyAndVisible];

    // 延迟启动，确保 UI 已渲染
    [self performSelector:@selector(bootPython) withObject:nil afterDelay:0.2];
}

@end
