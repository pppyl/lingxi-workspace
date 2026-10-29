"""Default-browser UI lifecycle; the tray service owns one open UI at a time."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser

WINDOW_TITLE='本地工具台 · 客户端'


def focus_browser_window():
    user32=ctypes.WinDLL('user32',use_last_error=True)
    user32.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user32.ShowWindow.argtypes=[wintypes.HWND,ctypes.c_int]
    user32.SetForegroundWindow.argtypes=[wintypes.HWND]
    user32.IsIconic.argtypes=[wintypes.HWND]
    found=[]
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    @callback_type
    def visit(window,_):
        title=ctypes.create_unicode_buffer(1024)
        user32.GetWindowTextW(window,title,len(title))
        if WINDOW_TITLE in title.value:
            found.append(window);return False
        return True
    user32.EnumWindows(visit,0)
    if found:
        if user32.IsIconic(found[0]):user32.ShowWindow(found[0],9)
        user32.SetForegroundWindow(found[0])


def wait_for_page(url,timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            with urlopen(url,timeout=1) as response:
                if response.status==200 and response.read(512):return
        except (URLError,OSError):pass
        time.sleep(.05)
    raise RuntimeError('本地页面未能启动，请查看 desktop.log。')


class DefaultBrowserWindow:
    def __init__(self,url,*,opener=None,focus=None,now=None):
        self.url=url;self.opener=opener or webbrowser.open;self.focus=focus or focus_browser_window
        self.now=now or time.monotonic
        self.condition=threading.Condition();self.clients=set();self.pending_until=0
        self.focus_revision=0;self.opens=0

    def open(self):
        with self.condition:
            existing=bool(self.clients) or self.now()<self.pending_until
            if existing:
                self.focus_revision+=1;self.condition.notify_all()
            else:self.pending_until=self.now()+30;self.opens+=1
        if existing:self.focus();return False
        if not self.opener(self.url,new=0,autoraise=True):
            with self.condition:self.pending_until=0
            raise RuntimeError('无法打开默认浏览器，请手动访问 '+self.url)
        return True

    def attach(self,client_id):
        with self.condition:
            self.clients.add(client_id);self.pending_until=0

    def detach(self,client_id):
        with self.condition:self.clients.discard(client_id)
