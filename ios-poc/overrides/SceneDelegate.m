//
//  SceneDelegate.m
//  iOS Python POC (NightStock)
//
//  最小验证：在 iOS 上初始化嵌入式 CPython，启动一个极简 http.server
//  (127.0.0.1:8765)，主线程用 WKWebView 加载该地址。打开 App 若能显示
//  “Python on iOS OK” 页面，即证明方案 B 整条链路（Python 解释器 +
//  socket 网络 + WebView）可用，可以把完整 app.py 移植进来。
//
//  Python 初始化严格照抄 BeeWare/CPython 官方 testbed 的 TestbedTests.m；
//  stdlib / app 路径由 install_python 构建脚本 + site.addsitedir 处理。
//

#import "SceneDelegate.h"
#import <WebKit/WebKit.h>
#import <Python/Python.h>

@interface SceneDelegate ()
@property (nonatomic, strong) WKWebView *webView;
@property (nonatomic, strong) UILabel *statusLabel;
@end

@implementation SceneDelegate

- (void)showStatus:(NSString *)text {
    dispatch_async(dispatch_get_main_queue(), ^{
        self.statusLabel.text = text;
    });
}

// 主线程初始化 Python + 启动 server（poc_server.start 内部用 Python 线程，
// 立即返回，不阻塞）。初始化代码来自官方 testbed。
- (void)bootPython {
    PyStatus status;
    PyPreConfig preconfig;
    PyConfig config;
    wchar_t *wtmp;

    NSString *resourcePath = [[NSBundle mainBundle] resourcePath];
    NSString *pythonHome   = [NSString stringWithFormat:@"%@/python", resourcePath];
    NSString *appPath      = [NSString stringWithFormat:@"%@/app", resourcePath];
    NSString *appPkgsPath  = [NSString stringWithFormat:@"%@/app_packages", resourcePath];

    setenv("NO_COLOR", "1", true);
    setenv("PYTHON_COLORS", "0", true);

    PyPreConfig_InitIsolatedConfig(&preconfig);
    PyConfig_InitIsolatedConfig(&config);
    preconfig.utf8_mode = 1;
    config.buffered_stdio = 0;
    config.write_bytecode = 0;
    config.install_signal_handlers = 1;

    NSLog(@"[POC] Pre-initializing Python...");
    status = Py_PreInitialize(&preconfig);
    if (PyStatus_Exception(status)) {
        NSLog(@"[POC] Py_PreInitialize failed: %s", status.err_msg);
        PyConfig_Clear(&config);
        [self showStatus:@"FAIL: Py_PreInitialize"];
        return;
    }

    wtmp = Py_DecodeLocale([pythonHome UTF8String], NULL);
    status = PyConfig_SetString(&config, &config.home, wtmp);
    PyMem_RawFree(wtmp);
    if (PyStatus_Exception(status)) {
        NSLog(@"[POC] set PYTHONHOME failed: %s", status.err_msg);
        PyConfig_Clear(&config);
        [self showStatus:@"FAIL: PYTHONHOME"];
        return;
    }

    status = PyConfig_Read(&config);
    if (PyStatus_Exception(status)) {
        NSLog(@"[POC] PyConfig_Read failed: %s", status.err_msg);
        PyConfig_Clear(&config);
        [self showStatus:@"FAIL: PyConfig_Read"];
        return;
    }

    NSLog(@"[POC] Initializing Python runtime...");
    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) {
        NSLog(@"[POC] Py_Initialize failed: %s", status.err_msg);
        PyConfig_Clear(&config);
        [self showStatus:@"FAIL: Py_Initialize"];
        return;
    }
    PyConfig_Clear(&config);

    // site.addsitedir 把 app / app_packages 加进 sys.path（并处理 .pth）
    PyObject *site = PyImport_ImportModule("site");
    if (site) {
        PyObject *add = PyObject_GetAttrString(site, "addsitedir");
        if (add && PyCallable_Check(add)) {
            const char *paths[] = { [appPath UTF8String], [appPkgsPath UTF8String] };
            for (int i = 0; i < 2; i++) {
                PyObject *arg = Py_BuildValue("(s)", paths[i]);
                PyObject *r = PyObject_CallObject(add, arg);
                Py_XDECREF(r); Py_XDECREF(arg);
            }
        }
        Py_XDECREF(add);
        Py_XDECREF(site);
    }

    NSLog(@"[POC] Starting poc_server ...");
    int rc = PyRun_SimpleString("import poc_server; poc_server.start()");
    if (rc != 0) {
        NSLog(@"[POC] import poc_server FAILED (see traceback above)");
        [self showStatus:@"FAIL: import poc_server（看设备日志）"];
        return;
    }

    // 释放 GIL，否则 http.server 的 Python 线程拿不到 GIL 无法响应请求。
    PyEval_SaveThread();

    NSLog(@"[POC] Python ready on 127.0.0.1:8765");
    [self showStatus:@"Python on iOS · http.server running"];
    [self performSelectorOnMainThread:@selector(loadServerPage) withObject:nil waitUntilDone:NO];
}

- (void)loadServerPage {
    NSURL *url = [NSURL URLWithString:@"http://127.0.0.1:8765/"];
    [self.webView loadRequest:[NSURLRequest requestWithURL:url]];
}

- (void)scene:(UIScene *)scene willConnectToSession:(UISceneSession *)session options:(UISceneConnectionOptions *)connectionOptions {
    UIWindowScene *windowScene = (UIWindowScene *)scene;
    self.window = [[UIWindow alloc] initWithWindowScene:windowScene];

    UIViewController *vc = [[UIViewController alloc] init];
    vc.view.backgroundColor = [UIColor systemBackgroundColor];

    self.statusLabel = [[UILabel alloc] init];
    self.statusLabel.translatesAutoresizingMaskIntoConstraints = NO;
    self.statusLabel.text = @"Starting Python on iOS...";
    self.statusLabel.textColor = [UIColor secondaryLabelColor];
    self.statusLabel.font = [UIFont systemFontOfSize:13];
    self.statusLabel.textAlignment = NSTextAlignmentCenter;
    [vc.view addSubview:self.statusLabel];

    WKWebViewConfiguration *cfg = [[WKWebViewConfiguration alloc] init];
    self.webView = [[WKWebView alloc] initWithFrame:CGRectZero configuration:cfg];
    self.webView.translatesAutoresizingMaskIntoConstraints = NO;
    [vc.view addSubview:self.webView];

    [NSLayoutConstraint activateConstraints:@[
        [self.statusLabel.topAnchor constraintEqualToAnchor:vc.view.safeAreaLayoutGuide.topAnchor constant:8],
        [self.statusLabel.leadingAnchor constraintEqualToAnchor:vc.view.leadingAnchor constant:8],
        [self.statusLabel.trailingAnchor constraintEqualToAnchor:vc.view.trailingAnchor constant:-8],
        [self.webView.topAnchor constraintEqualToAnchor:self.statusLabel.bottomAnchor constant:8],
        [self.webView.leadingAnchor constraintEqualToAnchor:vc.view.leadingAnchor],
        [self.webView.trailingAnchor constraintEqualToAnchor:vc.view.trailingAnchor],
        [self.webView.bottomAnchor constraintEqualToAnchor:vc.view.bottomAnchor],
    ]];

    self.window.rootViewController = vc;
    [self.window makeKeyAndVisible];

    // 延迟一点再启动，确保 UI 已渲染
    [self performSelector:@selector(bootPython) withObject:nil afterDelay:0.2];
}

@end
