"""Use the user's USB-connected APP session; never return its token to the browser.

The APP's own /authcode command is local to the Lingxi H5 page (Xs in
index.Dawt31QT.js). It shows the converted Lingxi token, not the native SDK seed.
No root, network interception or changes to the China Mobile APK are used here.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
import struct
import subprocess
from threading import Event, Lock
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib

ROOT = Path(__file__).resolve().parent
ADB = ROOT / "tools/platform-tools/adb.exe"
PACKAGE = "com.greenpoint.android.mc10086.activity"
PREFIX = PACKAGE + ":id/"
REMOTE_JAR = "/data/local/tmp/lingxi-login-reader.jar"
REMOTE_XML = "/data/local/tmp/lingxi-login-ui.xml"
PHONE_LOCK = Lock()


class PhoneError(Exception):
    pass


def adb_call(*args: str, serial: str = "", timeout: int = 15) -> bytes:
    if not ADB.is_file():
        raise PhoneError("缺少 ADB，请先安装本项目的 Android platform-tools。")
    cmd = [str(ADB)] + (["-s", serial] if serial else []) + list(args)
    env = os.environ.copy()
    env["ADB_USB_LEGACY"] = "1"
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding=None, errors=None, text=False, timeout=timeout, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise PhoneError("手机连接超时，请检查 USB 连接并解锁手机。") from exc
    if result.returncode:
        # ADB output can contain UI/credential data; expose a fixed diagnostic only.
        raise PhoneError("ADB 操作失败，请确认手机已连接，并允许这台电脑进行 USB 调试。")
    return result.stdout


def devices() -> list[dict]:
    result = []
    for line in adb_call("devices", "-l").decode("utf-8", "replace").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 2 or fields[0].startswith("*"):
            continue
        model = next((v.split(":", 1)[1] for v in fields[2:] if v.startswith("model:")), "Android 手机")
        result.append({"serial": fields[0], "state": fields[1], "model": model.replace("_", " ")})
    return result


def make_ui_reader(original: bytes) -> bytes:
    """Replace the system dumper's idle wait with a fixed one-second wait.

    Animated WebViews never become idle. All tree reading stays in Android's
    standard UiAutomation implementation. The helper is a separate temporary JAR;
    the system JAR and the target APP are never modified.
    """
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        dex = bytearray(archive.read("classes.dex"))
        u16 = lambda p: struct.unpack_from("<H", dex, p)[0]
        u32 = lambda p: struct.unpack_from("<I", dex, p)[0]

        def leb(p):
            value, shift = 0, 0
            while True:
                b = dex[p]
                p += 1
                value |= (b & 127) << shift
                shift += 7
                if b < 128:
                    return value, p

        def string(index):
            _, p = leb(u32(u32(60) + index * 4))
            return bytes(dex[p:dex.index(0, p)]).decode("utf-8", "replace")

        types = [string(u32(u32(68) + i * 4)) for i in range(u32(64))]
        methods = []
        for i in range(u32(88)):
            p = u32(92) + 8 * i
            proto = u32(76) + u16(p + 2) * 12
            params = u32(proto + 8)
            signature = tuple(types[u16(params + 4 + j * 2)] for j in range(u32(params))) if params else ()
            methods.append((types[u16(p)], string(u32(p + 4)), types[u32(proto + 4)], signature))
        sleep_id = methods.index(("Landroid/os/SystemClock;", "sleep", "V", ("J",)))
        idle_id = methods.index(("Landroid/app/UiAutomation;", "waitForIdle", "V", ("J", "J")))
        patches = 0
        for i in range(u32(96)):
            p = u32(100) + 32 * i
            if types[u32(p)] != "Lcom/android/commands/uiautomator/DumpCommand;":
                continue
            q = u32(p + 24)
            counts = []
            for _ in range(4):
                value, q = leb(q)
                counts.append(value)
            for _ in range(sum(counts[:2])):
                _, q = leb(q)
                _, q = leb(q)
            for count in counts[2:]:
                index = 0
                for _ in range(count):
                    diff, q = leb(q)
                    index += diff
                    _, q = leb(q)
                    code, q = leb(q)
                    if not code or methods[index][1] != "run":
                        continue
                    for offset in range(code + 16, code + 16 + u32(code + 12) * 2 - 4, 2):
                        if dex[offset] != 0x6E or u16(offset + 2) != idle_id or dex[offset + 1] >> 4 != 5:
                            continue
                        low = dex[offset + 4] >> 4
                        high = dex[offset + 5] & 15
                        dex[offset:offset + 6] = bytes((0x71, 0x20)) + struct.pack("<H", sleep_id) + bytes((low | high << 4, 0))
                        patches += 1
        if patches != 1:
            raise PhoneError("这台手机的界面读取组件暂不兼容，请勿手动粘贴凭据。")
        dex[12:32] = hashlib.sha1(dex[32:]).digest()
        struct.pack_into("<I", dex, 8, zlib.adler32(dex[12:]) & 0xFFFFFFFF)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as writer:
            for name in archive.namelist():
                writer.writestr(name, bytes(dex) if name == "classes.dex" else archive.read(name))
        return output.getvalue()


class Phone:
    def __init__(self, serial: str, cancel: Event):
        self.serial, self.cancel = serial, cancel

    def call(self, *args, **kwargs):
        if self.cancel.is_set():
            raise PhoneError("已取消连接。")
        return adb_call(*args, serial=self.serial, **kwargs)

    def prepare(self):
        original = self.call("exec-out", "cat", "/system/framework/uiautomator.jar")
        digest = hashlib.sha256(original).hexdigest()[:20]
        cache = ROOT / "data" / ("ui-reader-" + digest + ".jar")
        cache.parent.mkdir(exist_ok=True)
        if not cache.exists():
            try:
                cache.write_bytes(make_ui_reader(original))
            except (ValueError, KeyError, IndexError, struct.error, zipfile.BadZipFile) as exc:
                raise PhoneError("这台手机的界面读取组件暂不兼容。") from exc
        self.call("shell", "rm", "-f", REMOTE_JAR, REMOTE_XML)
        self.call("push", str(cache), REMOTE_JAR)
        self.call("shell", "chmod", "444", REMOTE_JAR)

    def tree(self):
        self.call("shell", "rm", "-f", REMOTE_XML)
        result = self.call("shell", f"CLASSPATH={REMOTE_JAR} app_process /system/bin com.android.commands.uiautomator.Launcher dump --compressed {REMOTE_XML}")
        if b"dumped to:" not in result:
            raise PhoneError("暂时无法读取手机界面，请解锁手机并保持中国移动 APP 在前台。")
        try:
            raw = self.call("shell", "cat", REMOTE_XML)
            return list(ET.fromstring(raw).iter("node"))
        finally:
            adb_call("shell", "rm", "-f", REMOTE_XML, serial=self.serial)

    def tap(self, node):
        bounds = [int(x) for x in re.findall(r"\d+", node.get("bounds", ""))]
        if len(bounds) != 4 or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            raise PhoneError("手机控件不可见，请回到中国移动 APP 首页后重试。")
        self.call("shell", "input", "tap", str((bounds[0] + bounds[2]) // 2), str((bounds[1] + bounds[3]) // 2))


def find_id(nodes, name):
    return next((n for n in nodes if n.get("resource-id") == PREFIX + name), None)


def find_text(nodes, text):
    return next((n for n in nodes if n.get("text") == text or n.get("content-desc") == text), None)


def connect_phone(report, cancel: Event, serial: str = "") -> dict:
    if not PHONE_LOCK.acquire(blocking=False):
        raise PhoneError("另一个连接任务正在操作手机，请稍后重试。")
    try:
        available = devices()
        eligible = [d for d in available if d["state"] == "device" and (not serial or serial == d["serial"])]
        if not eligible:
            raise PhoneError("未找到已授权的手机。请连接 USB、开启 USB 调试，并在手机上允许这台电脑。")
        if len(eligible) != 1:
            raise PhoneError("检测到多台手机，请先选择要连接的手机。")
        device = eligible[0]
        phone = Phone(device["serial"], cancel)
        report("opening", "正在打开手机上的中国移动 APP…")
        component = phone.call("shell", "cmd", "package", "resolve-activity", "--brief", PACKAGE).decode("utf-8", "replace").strip().splitlines()[-1]
        if not component.startswith(PACKAGE + "/"):
            raise PhoneError("手机上未安装中国移动 APP。")
        phone.prepare()
        phone.call("shell", "input", "keyevent", "WAKEUP")
        phone.call("shell", "am", "start", "-n", component)
        deadline = time.monotonic() + 180
        opened = False
        while time.monotonic() < deadline:
            nodes = phone.tree()
            login = next((n for n in nodes if n.get("package") == PACKAGE and n.get("text") in ("请登录", "点击登录", "登录/注册", "立即登录")), None)
            if login is not None:
                report("waiting_phone", "请在手机上的中国移动 APP 完成登录验证，网页会继续自动连接。")
                phone.tap(login)
                if cancel.wait(2):
                    raise PhoneError("已取消连接。")
                continue
            expand = find_text(nodes, "展开全屏")
            if expand is not None:
                report("reading", "正在打开灵犀并读取登录态…")
                phone.tap(expand)
                continue
            tip = find_id(nodes, "cmcc_digital_input_tips")
            editor = find_id(nodes, "cmcc_digital_keypans_input")
            if tip is not None:
                report("reading", "正在自动获取灵犀登录态…")
                phone.tap(tip)
                continue
            if editor is not None:
                draft = editor.get("text", "")
                if draft and draft not in ("/authcode", "请输入您要咨询的内容"):
                    raise PhoneError("灵犀输入框有未发送的文字，请先在手机处理后重新连接。")
                if draft != "/authcode":
                    phone.call("shell", "input", "text", "/authcode")
                    nodes = phone.tree()
                    editor = find_id(nodes, "cmcc_digital_keypans_input")
                send = find_id(nodes, "cmcc_digital_keypans_keyAsrOrSend_img")
                if editor is None or editor.get("text") != "/authcode" or send is None:
                    raise PhoneError("未能确认灵犀的登录态读取指令，请重新连接。")
                phone.tap(send)
                after = phone.tree()
                tokens = list(dict.fromkeys(n.get("text", "") for n in after if n.get("class") not in ("android.widget.Image", "android.widget.ImageView") and re.fullmatch(r"[A-Za-z0-9+/=_-]{60,1024}", n.get("text", ""))))
                if len(tokens) != 1:
                    raise PhoneError("APP 没有提供有效的灵犀登录态。请确认已登录，并在手机上正常打开灵犀后重试。")
                return {"auth_code": tokens[0], "device_model": device["model"]}
            # The observed homepage has four accessible tabs and a central AI
            # avatar in the gap between 视频 and 移动爱购. Derive its bounds.
            video, mall = find_text(nodes, "视频"), find_text(nodes, "移动爱购")
            if video is not None and mall is not None and not opened:
                vb = [int(x) for x in re.findall(r"\d+", video.get("bounds", ""))]
                mb = [int(x) for x in re.findall(r"\d+", mall.get("bounds", ""))]
                if len(vb) == len(mb) == 4 and mb[0] > vb[2] and vb[1] > 0:
                    report("reading", "正在打开手机上的灵犀…")
                    phone.call("shell", "input", "tap", str((vb[2] + mb[0]) // 2), str((vb[1] + vb[3]) // 2))
                    opened = True
                    continue
            report("waiting_phone", "请解锁手机；如出现登录页面，请在中国移动 APP 完成登录并打开灵犀，网页会继续自动连接。")
            if cancel.wait(2):
                raise PhoneError("已取消连接。")
        raise PhoneError("等待手机登录超时，请在 APP 登录并打开灵犀后重新连接。")
    finally:
        PHONE_LOCK.release()
