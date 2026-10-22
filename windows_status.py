"""Small native Win32 status window and notification-area icon. No webview."""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path
import time


WPARAM=C.c_size_t
LPARAM=LRESULT=C.c_ssize_t
WNDPROC=C.WINFUNCTYPE(LRESULT,W.HWND,W.UINT,WPARAM,LPARAM)
RESTORE=0x8002
TRAY=0x8001
OPEN,FOLDER,QUIT=201,203,205


def status_text(state):
    return '服务运行中' if state=='running' else '服务未运行'


class WindowClass(C.Structure):
    _fields_=[('size',W.UINT),('style',W.UINT),('proc',WNDPROC),('class_extra',C.c_int),
              ('window_extra',C.c_int),('instance',W.HINSTANCE),('icon',W.HICON),
              ('cursor',W.HANDLE),('background',W.HBRUSH),('menu',W.LPCWSTR),
              ('name',W.LPCWSTR),('small_icon',W.HICON)]


class NotifyIcon(C.Structure):
    _fields_=[('size',W.DWORD),('window',W.HWND),('id',W.UINT),('flags',W.UINT),
              ('message',W.UINT),('icon',W.HICON),('tip',W.WCHAR*128),
              ('state',W.DWORD),('state_mask',W.DWORD),('info',W.WCHAR*256),
              ('version',W.UINT),('info_title',W.WCHAR*64),('info_flags',W.DWORD),
              ('guid',C.c_byte*16),('balloon_icon',W.HICON)]


def class_name(directory):
    identity=os.path.normcase(str(Path(directory).resolve())).encode('utf-8')
    return 'TeemerchantStatus-'+hashlib.sha256(identity).hexdigest()[:24]


def api():
    user=C.WinDLL('user32',use_last_error=True)
    kernel=C.WinDLL('kernel32',use_last_error=True)
    shell=C.WinDLL('shell32',use_last_error=True)
    gdi=C.WinDLL('gdi32',use_last_error=True)
    signatures={
        user:{
            'RegisterClassExW':(W.ATOM,[C.POINTER(WindowClass)]),
            'UnregisterClassW':(W.BOOL,[W.LPCWSTR,W.HINSTANCE]),
            'CreateWindowExW':(W.HWND,[W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,C.c_int,C.c_int,C.c_int,C.c_int,W.HWND,W.HMENU,W.HINSTANCE,C.c_void_p]),
            'DefWindowProcW':(LRESULT,[W.HWND,W.UINT,WPARAM,LPARAM]),
            'SendMessageW':(LRESULT,[W.HWND,W.UINT,WPARAM,LPARAM]),
            'PostMessageW':(W.BOOL,[W.HWND,W.UINT,WPARAM,LPARAM]),
            'SetWindowTextW':(W.BOOL,[W.HWND,W.LPCWSTR]),
            'ShowWindow':(W.BOOL,[W.HWND,C.c_int]),
            'SetForegroundWindow':(W.BOOL,[W.HWND]),
            'DestroyWindow':(W.BOOL,[W.HWND]),
            'EnableWindow':(W.BOOL,[W.HWND,W.BOOL]),
            'UpdateWindow':(W.BOOL,[W.HWND]),
            'SetTimer':(WPARAM,[W.HWND,WPARAM,W.UINT,C.c_void_p]),
            'KillTimer':(W.BOOL,[W.HWND,WPARAM]),
            'GetMessageW':(C.c_int,[C.POINTER(W.MSG),W.HWND,W.UINT,W.UINT]),
            'TranslateMessage':(W.BOOL,[C.POINTER(W.MSG)]),
            'DispatchMessageW':(LRESULT,[C.POINTER(W.MSG)]),
            'IsDialogMessageW':(W.BOOL,[W.HWND,C.POINTER(W.MSG)]),
            'PostQuitMessage':(None,[C.c_int]),
            'FindWindowW':(W.HWND,[W.LPCWSTR,W.LPCWSTR]),
            'RegisterWindowMessageW':(W.UINT,[W.LPCWSTR]),
            'LoadCursorW':(W.HANDLE,[W.HINSTANCE,C.c_void_p]),
            'LoadIconW':(W.HICON,[W.HINSTANCE,C.c_void_p]),
            'LoadImageW':(W.HANDLE,[W.HINSTANCE,W.LPCWSTR,W.UINT,C.c_int,C.c_int,W.UINT]),
            'DestroyIcon':(W.BOOL,[W.HICON]),
            'AdjustWindowRectEx':(W.BOOL,[C.POINTER(W.RECT),W.DWORD,W.BOOL,W.DWORD]),
            'GetSystemMetrics':(C.c_int,[C.c_int]),
            'GetCursorPos':(W.BOOL,[C.POINTER(W.POINT)]),
            'CreatePopupMenu':(W.HMENU,[]),
            'AppendMenuW':(W.BOOL,[W.HMENU,W.UINT,WPARAM,W.LPCWSTR]),
            'TrackPopupMenu':(W.UINT,[W.HMENU,W.UINT,C.c_int,C.c_int,C.c_int,W.HWND,C.c_void_p]),
            'DestroyMenu':(W.BOOL,[W.HMENU]),
            'MessageBoxW':(C.c_int,[W.HWND,W.LPCWSTR,W.LPCWSTR,W.UINT]),
            'InvalidateRect':(W.BOOL,[W.HWND,C.c_void_p,W.BOOL]),
        },
        kernel:{
            'GetModuleHandleW':(W.HINSTANCE,[W.LPCWSTR]),
            'CreateMutexW':(W.HANDLE,[C.c_void_p,W.BOOL,W.LPCWSTR]),
            'CloseHandle':(W.BOOL,[W.HANDLE]),
        },
        shell:{
            'Shell_NotifyIconW':(W.BOOL,[W.DWORD,C.POINTER(NotifyIcon)]),
            'ShellExecuteW':(C.c_ssize_t,[W.HWND,W.LPCWSTR,W.LPCWSTR,W.LPCWSTR,W.LPCWSTR,C.c_int]),
        },
        gdi:{
            'CreateFontW':(W.HFONT,[C.c_int]*5+[W.DWORD]*8+[W.LPCWSTR]),
            'CreateSolidBrush':(W.HBRUSH,[W.DWORD]),
            'DeleteObject':(W.BOOL,[W.HANDLE]),
            'SetBkMode':(C.c_int,[W.HDC,C.c_int]),
            'SetTextColor':(W.DWORD,[W.HDC,W.DWORD]),
        },
    }
    for dll,functions in signatures.items():
        for name,(result,args) in functions.items():
            fn=getattr(dll,name);fn.restype=result;fn.argtypes=args
    return user,kernel,shell,gdi


class StatusWindow:
    def __init__(self,runtime,libraries,name):
        self.runtime=runtime
        self.user,self.kernel,self.shell,self.gdi=libraries
        self.name=name
        self.window=None
        self.controls={}
        self.fonts=[]
        self.quitting=False
        self.tray_present=False
        self.previous=None
        self.instance=self.kernel.GetModuleHandleW(None)
        try:
            self.user.SetProcessDPIAware()
            self.user.GetDpiForSystem.restype=W.UINT
            self.scale=self.user.GetDpiForSystem()/96
        except AttributeError:self.scale=1
        self.brush=self.gdi.CreateSolidBrush(0xFAFAFA)
        icon_path=Path(__file__).parent/'static/teemerchant.ico'
        self.icon=self.user.LoadImageW(None,str(icon_path),1,self.px(32),self.px(32),0x10)
        self.owned_icon=bool(self.icon)
        if not self.icon:self.icon=self.user.LoadIconW(None,C.c_void_p(32512))
        self.callback=WNDPROC(self.dispatch)
        self.taskbar_created=self.user.RegisterWindowMessageW('TaskbarCreated')

    def px(self,value):return round(value*self.scale)

    def control(self,identifier,kind,text,x,y,width,height,style=0,font=None):
        handle=self.user.CreateWindowExW(0,kind,text,0x50000000|style,self.px(x),self.px(y),self.px(width),self.px(height),self.window,identifier,self.instance,None)
        if not handle:raise C.WinError(C.get_last_error())
        self.controls[identifier]=handle
        self.user.SendMessageW(handle,0x30,font or self.fonts[0],1)
        return handle

    def create(self):
        cls=WindowClass(size=C.sizeof(WindowClass),proc=self.callback,instance=self.instance,
                        icon=self.icon,small_icon=self.icon,cursor=self.user.LoadCursorW(None,C.c_void_p(32512)),
                        background=self.brush,name=self.name)
        if not self.user.RegisterClassExW(C.byref(cls)):raise C.WinError(C.get_last_error())
        style=0x00C80000|0x00020000 # caption, system menu, minimize; fixed size
        rect=W.RECT(0,0,self.px(380),self.px(140))
        self.user.AdjustWindowRectEx(C.byref(rect),style,False,0)
        width,height=rect.right-rect.left,rect.bottom-rect.top
        self.window=self.user.CreateWindowExW(0,self.name,'teemerchant 服务',style,
            max(0,(self.user.GetSystemMetrics(0)-width)//2),max(0,(self.user.GetSystemMetrics(1)-height)//2),width,height,None,None,self.instance,None)
        if not self.window:raise C.WinError(C.get_last_error())
        for size,weight in ((15,400),(18,600)):
            self.fonts.append(self.gdi.CreateFontW(-self.px(size),0,0,0,weight,0,0,0,1,0,0,5,0,'Segoe UI'))
        self.control(102,'STATIC',status_text('starting'),24,24,332,28,1,font=self.fonts[1])
        self.control(OPEN,'BUTTON','打开管理页面',24,78,160,38,0x00010001)
        self.control(FOLDER,'BUTTON','打开数据目录',196,78,160,38,0x00010000)
        self.add_tray()
        self.user.SetTimer(self.window,1,500,None)
        self.update()
        self.restore()

    def tray_data(self):
        data=NotifyIcon(size=C.sizeof(NotifyIcon),window=self.window,id=1,flags=0x87,message=TRAY,icon=self.icon)
        data.tip='teemerchant · '+status_text(self.runtime.state)
        return data

    def add_tray(self):
        data=self.tray_data()
        self.tray_present=bool(self.shell.Shell_NotifyIconW(0,C.byref(data)))
        if self.tray_present:
            data.version=4
            self.shell.Shell_NotifyIconW(4,C.byref(data))

    def restore(self):
        if self.window:
            self.user.ShowWindow(self.window,9)
            self.user.SetForegroundWindow(self.window)

    def hide(self):
        if not self.tray_present:self.add_tray()
        if self.tray_present:self.user.ShowWindow(self.window,0)
        else:
            self.restore()
            self.user.MessageBoxW(self.window,'系统托盘暂不可用，窗口将保持显示。','teemerchant',0x30)

    def update(self):
        state=self.runtime.state
        current=(state,self.quitting)
        if current!=self.previous:
            self.previous=current
            self.user.SetWindowTextW(self.controls[102],status_text(state))
            self.user.EnableWindow(self.controls[OPEN],state=='running' and not self.quitting)
            self.user.InvalidateRect(self.controls[102],None,True)
            if self.tray_present:self.shell.Shell_NotifyIconW(1,C.byref(self.tray_data()))
            if state=='failed':self.restore()
        if self.quitting and self.runtime.done.is_set():self.user.DestroyWindow(self.window)

    def open_target(self,target):
        if self.shell.ShellExecuteW(self.window,'open',str(target),None,None,1)<=32:
            self.user.MessageBoxW(self.window,'无法打开，请检查系统默认应用。','teemerchant',0x30)

    def command(self,identifier):
        if identifier==OPEN and self.runtime.state=='running' and not self.quitting:self.open_target(self.runtime.args.url)
        elif identifier==FOLDER:self.open_target(self.runtime.args.data)
        elif identifier==QUIT:
            self.quitting=True
            self.runtime.shutdown()
            self.restore()
            self.update()
        elif identifier==RESTORE:self.restore()

    def menu(self):
        menu=self.user.CreatePopupMenu()
        for identifier,label in ((RESTORE,'显示窗口'),(OPEN,'打开管理页面'),(FOLDER,'打开数据目录')):
            disabled=identifier==OPEN and (self.runtime.state!='running' or self.quitting)
            self.user.AppendMenuW(menu,1 if disabled else 0,identifier,label)
        self.user.AppendMenuW(menu,0x800,0,None)
        self.user.AppendMenuW(menu,1 if self.quitting else 0,QUIT,'退出服务')
        point=W.POINT();self.user.GetCursorPos(C.byref(point))
        self.user.SetForegroundWindow(self.window)
        choice=self.user.TrackPopupMenu(menu,0x100|2,point.x,point.y,0,self.window,None)
        self.user.DestroyMenu(menu)
        self.user.PostMessageW(self.window,0,0,0)
        if choice:self.command(choice)

    def dispatch(self,window,message,wparam,lparam):
        if message==0x111:
            self.command(wparam&0xffff);return 0
        if message==0x112 and wparam&0xfff0==0xf020:
            self.hide();return 0
        if message==0x10:
            self.hide();return 0
        if message==RESTORE:self.restore();return 0
        if message==TRAY:
            event=lparam&0xffff
            if event in (0x202,0x203,0x400,0x401):self.restore()
            elif event in (0x205,0x7b):self.menu()
            return 0
        if message==self.taskbar_created:
            self.tray_present=False;self.add_tray()
            if not self.tray_present:self.restore()
            return 0
        if message==0x113:self.update();return 0
        if message==0x138: # WM_CTLCOLORSTATIC
            self.gdi.SetBkMode(wparam,1)
            color=0x50483F
            if lparam==self.controls.get(102):color=0x367E12 if self.runtime.state=='running' else 0x3030B0 if self.runtime.state=='failed' else 0x50483F
            self.gdi.SetTextColor(wparam,color)
            return self.brush
        if message==0x11:return 1 # Allow Windows logoff/shutdown.
        if message==0x16 and wparam:self.runtime.shutdown();return 0
        if message==0x2:
            self.user.KillTimer(window,1)
            if self.tray_present:self.shell.Shell_NotifyIconW(2,C.byref(self.tray_data()))
            self.user.PostQuitMessage(0)
            return 0
        return self.user.DefWindowProcW(window,message,wparam,lparam)

    def loop(self):
        try:
            self.create()
            self.runtime.start()
            message=W.MSG()
            while True:
                result=self.user.GetMessageW(C.byref(message),None,0,0)
                if result<=0:break
                if not self.user.IsDialogMessageW(self.window,C.byref(message)):
                    self.user.TranslateMessage(C.byref(message));self.user.DispatchMessageW(C.byref(message))
        finally:
            self.runtime.shutdown()
            if self.runtime.thread:self.runtime.done.wait()
            for font in self.fonts:self.gdi.DeleteObject(font)
            self.user.UnregisterClassW(self.name,self.instance)
            self.gdi.DeleteObject(self.brush)
            if self.owned_icon:self.user.DestroyIcon(self.icon)


def run(runtime):
    libraries=api()
    user,kernel,_,_=libraries
    name=class_name(runtime.args.data)
    C.set_last_error(0)
    mutex=kernel.CreateMutexW(None,False,'Local\\'+name)
    if not mutex:raise C.WinError(C.get_last_error())
    duplicate=C.get_last_error()==183
    try:
        if duplicate:
            for _ in range(100):
                window=user.FindWindowW(name,None)
                if window:
                    user.ShowWindow(window,9);user.SetForegroundWindow(window)
                    user.PostMessageW(window,RESTORE,0,0)
                    return 0
                time.sleep(.05)
            user.MessageBoxW(None,'teemerchant 正在启动或退出，请稍后重试。','teemerchant',0x40)
            return 0
        StatusWindow(runtime,libraries,name).loop()
        return 1 if runtime.error else 0
    finally:kernel.CloseHandle(mutex)
