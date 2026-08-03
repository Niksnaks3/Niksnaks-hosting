from __future__ import annotations

import ctypes
import ctypes.wintypes
import io
import sys
import threading
from pathlib import Path

import gi
from gi.repository import GLib

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

HANDLE = ctypes.c_void_p
try:
    HICON = ctypes.wintypes.HICON
except AttributeError:
    HICON = HANDLE
try:
    HBRUSH = ctypes.wintypes.HBRUSH
except AttributeError:
    HBRUSH = HANDLE
try:
    HMENU = ctypes.wintypes.HMENU
except AttributeError:
    HMENU = HANDLE
try:
    HINSTANCE = ctypes.wintypes.HINSTANCE
except AttributeError:
    HINSTANCE = HANDLE

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_QUIT = 0x0012
WM_NULL = 0x0000
WM_APP = 0x8000
WM_USER = 0x0400
NIN_SELECT = WM_USER + 0
WM_CONTEXTMENU = 0x007B

NIF_MESSAGE = 0x0001
NIF_ICON = 0x0002
NIF_TIP = 0x0004
NIF_GUID = 0x0020
NIF_SHOWTIP = 0x0040
NIM_ADD = 0x0000
NIM_MODIFY = 0x0001
NIM_DELETE = 0x0002
NIM_SETVERSION = 0x0004
NOTIFYICON_VERSION_4 = 4

WS_EX_TOOLWINDOW = 0x00000080
WS_OVERLAPPED = 0x00000000
CW_USEDEFAULT = ctypes.c_int(0x80000000).value

LR_DEFAULTSIZE = 0x0040
LR_LOADFROMFILE = 0x0010
IMAGE_ICON = 1

GCLP_HBRBACKGROUND = -10

COLOR_WINDOW = 5

TPM_RETURNCMD = 0x0100
TPM_LEFTALIGN = 0x0000
TPM_BOTTOMALIGN = 0x0020
TPM_RIGHTBUTTON = 0x0002

MF_BYPOSITION = 0x00000400
MF_STRING = 0x00000000

ID_SHOW = 1001
ID_QUIT = 1002

class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("hWnd", ctypes.wintypes.HWND),
        ("uID", ctypes.wintypes.UINT),
        ("uFlags", ctypes.wintypes.UINT),
        ("uCallbackMessage", ctypes.wintypes.UINT),
        ("hIcon", HICON),
        ("szTip", ctypes.wintypes.WCHAR * 128),
        ("dwState", ctypes.wintypes.DWORD),
        ("dwStateMask", ctypes.wintypes.DWORD),
        ("szInfo", ctypes.wintypes.WCHAR * 256),
        ("uVersion", ctypes.wintypes.UINT),
        ("szInfoTitle", ctypes.wintypes.WCHAR * 64),
        ("dwInfoFlags", ctypes.wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", HICON),
    ]

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

WPARAM = ctypes.c_uint if ctypes.sizeof(ctypes.c_void_p) <= 4 else ctypes.c_ulonglong
LPARAM = ctypes.c_long if ctypes.sizeof(ctypes.c_void_p) <= 4 else ctypes.c_longlong

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    WPARAM,
    LPARAM,
)

user32.DefWindowProcW.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    WPARAM,
    LPARAM,
]
user32.DefWindowProcW.restype = ctypes.c_ssize_t

kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = HINSTANCE

user32.GetSysColorBrush.argtypes = [ctypes.c_int]
user32.GetSysColorBrush.restype = HBRUSH

user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.RegisterClassExW.restype = ctypes.wintypes.ATOM

user32.CreateWindowExW.argtypes = [
    ctypes.wintypes.DWORD,
    ctypes.wintypes.LPCWSTR,
    ctypes.wintypes.LPCWSTR,
    ctypes.wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.wintypes.HWND,
    HMENU,
    HINSTANCE,
    ctypes.c_void_p,
]
user32.CreateWindowExW.restype = ctypes.wintypes.HWND

user32.LoadImageW.argtypes = [
    HINSTANCE,
    ctypes.wintypes.LPCWSTR,
    ctypes.wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.wintypes.UINT,
]
user32.LoadImageW.restype = HANDLE

user32.DestroyIcon.argtypes = [HICON]
user32.DestroyIcon.restype = ctypes.wintypes.BOOL

user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = HMENU

user32.DestroyMenu.argtypes = [HMENU]
user32.DestroyMenu.restype = ctypes.wintypes.BOOL

user32.AppendMenuW.argtypes = [HMENU, ctypes.wintypes.UINT, WPARAM, ctypes.wintypes.LPCWSTR]
user32.AppendMenuW.restype = ctypes.wintypes.BOOL

user32.SetMenuDefaultItem.argtypes = [HMENU, ctypes.wintypes.UINT, ctypes.wintypes.UINT]
user32.SetMenuDefaultItem.restype = ctypes.wintypes.BOOL

user32.TrackPopupMenu.argtypes = [
    HMENU,
    ctypes.wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.wintypes.HWND,
    ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = ctypes.wintypes.BOOL

user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL

user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.GetCursorPos.restype = ctypes.wintypes.BOOL

user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, WPARAM, LPARAM]
user32.PostMessageW.restype = ctypes.wintypes.BOOL

user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int

user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.TranslateMessage.restype = ctypes.wintypes.BOOL

user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.restype = ctypes.c_ssize_t

user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None

shell32.Shell_NotifyIconW.argtypes = [ctypes.wintypes.DWORD, ctypes.c_void_p]
shell32.Shell_NotifyIconW.restype = ctypes.wintypes.BOOL

class WindowsTrayManager:
    def __init__(self, app):
        self.app = app
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._icon_handle: int | None = None
        self._status_message = "Niksnaks-Hosting"
        self._running = threading.Event()
        self._nid = None
        self._icon_id = 0
        self._wndproc = None

    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._hwnd = None
        self._nid = None
        self._wndproc = None

    def set_status(self, message: str) -> None:
        self._status_message = f"Niksnaks-Hosting \u2014 {message}" if message else "Niksnaks-Hosting"
        self._update_tooltip()

    def _update_tooltip(self) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_APP + 1, 0, 0)

    def _thread_main(self) -> None:
        module = kernel32.GetModuleHandleW(None)
        class_name = f"NiksnaksHostingTray_{threading.get_native_id()}"

        self._wndproc = WNDPROC(self._wnd_proc)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.UINT),
                ("style", ctypes.wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", HINSTANCE),
                ("hIcon", HICON),
                ("hCursor", HANDLE),
                ("hbrBackground", HBRUSH),
                ("lpszMenuName", ctypes.wintypes.LPCWSTR),
                ("lpszClassName", ctypes.wintypes.LPCWSTR),
                ("hIconSm", HICON),
            ]

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p).value
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = module
        wc.hIcon = 0
        wc.hCursor = 0
        wc.hbrBackground = user32.GetSysColorBrush(COLOR_WINDOW)
        wc.lpszMenuName = None
        wc.lpszClassName = class_name
        wc.hIconSm = 0

        if not user32.RegisterClassExW(ctypes.byref(wc)):
            return

        self._hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "NiksnaksHostingTray",
            WS_OVERLAPPED,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            0,
            0,
            module,
            None,
        )

        if not self._hwnd:
            return

        self._add_icon()

        msg = ctypes.wintypes.MSG()
        while self._running.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._remove_icon()

    def _wnd_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_COMMAND:
            cmd_id = wparam & 0xFFFF
            if cmd_id == ID_SHOW:
                GLib.idle_add(self._show_window)
            elif cmd_id == ID_QUIT:
                GLib.idle_add(self._quit_app)
            return 0

        if msg == WM_APP + 1:
            self._update_icon_tooltip()
            return 0

        if msg == WM_DESTROY:
            self._remove_icon()
            user32.PostQuitMessage(0)
            return 0

        handled = self._handle_tray_notify(msg, lparam)
        if handled:
            return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_tray_notify(self, msg: int, lparam: int) -> bool:
        callback_msg = WM_APP + 100
        if msg != callback_msg:
            return False

        event = lparam

        if event == WM_CONTEXTMENU:
            GLib.idle_add(self._show_popup)
            return True

        if event in (NIN_SELECT, 0x0402):
            GLib.idle_add(self._show_window)
            return True

        return False

    def _add_icon(self) -> None:
        self._icon_handle = self._create_hicon()

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = self._icon_id
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        nid.uCallbackMessage = WM_APP + 100
        nid.hIcon = self._icon_handle
        nid.szTip = self._status_message[:127]

        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        nid.uVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))

        self._nid = nid

    def _remove_icon(self) -> None:
        if self._nid:
            nid = self._nid
            nid.uFlags = 0
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self._nid = None

        if self._icon_handle:
            user32.DestroyIcon(self._icon_handle)
            self._icon_handle = None

    def _update_icon_tooltip(self) -> None:
        if not self._nid:
            return
        nid = self._nid
        nid.uFlags = NIF_TIP
        nid.szTip = self._status_message[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _create_hicon(self) -> int:
        try:
            pil_image = self._build_icon_pil()
            if pil_image:
                hicon = self._pil_to_hicon(pil_image)
                if hicon:
                    return hicon
        except Exception as e:
            print(f"PIL icon creation failed: {e}", file=sys.stderr)

        return self._hicon_fallback()

    def _build_icon_pil(self) -> object | None:
        try:
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf

            svg_path = self._find_svg_path()
            if svg_path and svg_path.exists():
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(svg_path), 64, 64, True)
                res = pixbuf.save_to_bufferv("png", [], [])
                if isinstance(res, tuple) and len(res) == 2:
                    success, buffer = res
                    if success:
                        from PIL import Image

                        return Image.open(io.BytesIO(buffer))
        except Exception as e:
            print(f"SVG icon load failed: {e}", file=sys.stderr)

        from PIL import Image

        return self._draw_fallback_pil()

    def _draw_fallback_pil(self):
        from PIL import Image, ImageDraw

        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        draw.ellipse([2, 2, size - 2, size - 2], fill=(46, 194, 126, 255))

        try:
            from PIL import ImageFont

            font = ImageFont.truetype("segoeui.ttf", 36)
        except Exception:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), "H", font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (size - tw) / 2 - bbox[0]
        ty = (size - th) / 2 - bbox[1]
        draw.text((tx, ty), "H", fill=(255, 255, 255, 255), font=font)

        return img

    def _find_svg_path(self) -> Path | None:
        if getattr(sys, "frozen", False):
            bundle_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
            candidates = [
                bundle_dir / "share" / "icons" / "hicolor" / "scalable" / "apps" / "com.niksnakshosting.NiksnaksHosting.svg",
                bundle_dir / "icons" / "com.niksnakshosting.NiksnaksHosting.svg",
                bundle_dir / "com.niksnakshosting.NiksnaksHosting.svg",
            ]
            for candidate in candidates:
                if candidate.exists():
                    return candidate

        dev_path = Path(__file__).resolve().parents[3] / "packaging" / "linux" / "com.niksnakshosting.NiksnaksHosting.svg"
        if dev_path.exists():
            return dev_path
        return None

    def _pil_to_hicon(self, img) -> int:
        import os
        import tempfile

        if img.mode not in ("RGBA", "RGB"):
            img = img.convert("RGBA")

        fd, path = tempfile.mkstemp(suffix=".ico")
        try:
            os.close(fd)
            img.save(path, format="ICO", sizes=[(img.width, img.height)])
            hicon = user32.LoadImageW(
                0,
                path,
                IMAGE_ICON,
                0,
                0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE,
            )
            return hicon or 0
        except Exception as e:
            print(f"Temp ICO load failed: {e}", file=sys.stderr)
            return 0
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    def _hicon_fallback(self) -> int:
        hicon = user32.LoadImageW(
            None,
            "#32512",
            IMAGE_ICON,
            32,
            32,
            LR_DEFAULTSIZE,
        )
        return hicon or 0

    def _show_popup(self) -> None:
        if not self._hwnd:
            return

        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return

        user32.AppendMenuW(hmenu, MF_STRING, ID_SHOW, "Show Niksnaks-Hosting")
        user32.AppendMenuW(hmenu, MF_STRING, ID_QUIT, "Quit")
        user32.SetMenuDefaultItem(hmenu, ID_SHOW, False)

        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))

        user32.SetForegroundWindow(self._hwnd)

        cmd = user32.TrackPopupMenu(
            hmenu,
            TPM_RETURNCMD | TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON,
            pt.x,
            pt.y,
            0,
            self._hwnd,
            None,
        )

        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)

        user32.DestroyMenu(hmenu)

        if cmd == ID_SHOW:
            GLib.idle_add(self._show_window)
        elif cmd == ID_QUIT:
            GLib.idle_add(self._quit_app)

    def _show_window(self) -> None:
        if self.app._window:
            self.app._window.set_visible(True)
            self.app._window.present()

    def _quit_app(self) -> None:
        self.app.activate_action("quit", None)
