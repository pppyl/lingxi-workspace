"""Lingxi text protocol from the APP's main H5, verified on a real session.

Source: analysis/native-h5/index.Dawt31QT.js, Xs (payload), Vt (service agreement readiness).
"""
from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from threading import Lock

from workspace.http_transport import requests, CurlHttpVersion
from workspace.http_transport import RequestException

AUTH_URL = "https://wx.10086.cn/api/nguac/model/sso/jsession"
CHAT_URL = "https://h.app.coc.10086.cn/lingxi/api/v1/chat"
VERIFY_URL = "https://cmkf.cmcc-cs.cn/xiaohe/internet/llmManager/getUserAgreementStatus"
MAX_EVENT_BYTES = 2_000_000


class LingxiError(Exception):
    def __init__(self, message: str, *, authentication: bool = False, code: str = ''):
        super().__init__(message)
        self.authentication = authentication
        self.code = code


def verify_token(token: str) -> None:
    """Check Lingxi readiness; its agreement-service errors do not invalidate APP login."""
    with requests.Session(impersonate="chrome") as client:
        response = client.post(
            VERIFY_URL, json={"authCode": token, "sdkAppKey": "zgydAppSDK"},
            headers={"X-App-Code": "lingxi_front", "Origin": "https://wx.10086.cn/", "Referer": "https://wx.10086.cn/"},
            timeout=(10, 20), allow_redirects=False,
        )
        if response.status_code != 200:
            raise LingxiError(f"灵犀服务核验返回 HTTP {response.status_code}，请稍后在灵犀中重试。",
                              authentication=response.status_code in {401, 403}, code=str(response.status_code))
        try:
            value = response.json()
        except ValueError as exc:
            raise LingxiError("登录验证服务返回格式不正确。") from exc
        if not isinstance(value, dict) or 'rtnCode' not in value:
            raise LingxiError('灵犀服务核验响应不完整，请稍后重试。', code='UPSTREAM_FORMAT')
        code = str(value['rtnCode'])
        if code != '0':
            safe_code = code if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', code) else 'unknown'
            raise LingxiError(f'灵犀服务核验未通过（{safe_code}），共享 APP 登录会独立核对，无需因此重新获取短信。',
                              authentication=code in {'401', '403'}, code=safe_code)


def cookie_parameters(value: str) -> dict[str, str]:
    parameters = {}
    for part in value.split(";"):
        key, separator, content = part.strip().partition("=")
        if separator and key:
            key = "ticketId" if key.lower() == "ticketid" else key.lower()
            parameters[key] = content.strip()
    if not parameters:
        raise LingxiError("登录信息格式不正确，请粘贴完整的 APP Cookie。")
    return parameters


def exchange_cookie(value: str) -> str:
    with requests.Session(impersonate="chrome") as client:
        response = client.post(
            AUTH_URL,
            json=cookie_parameters(value),
            headers={"Accept": "application/json, text/plain, */*", "cache-control": "no-cache"},
            timeout=(10, 25),
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise LingxiError(f"灵犀换票服务返回 HTTP {response.status_code}，请稍后在灵犀中重试。",
                              authentication=response.status_code in {401, 403}, code=str(response.status_code))
        try:
            result = response.json()
        except ValueError as exc:
            raise LingxiError("登录服务没有返回有效的 JSON。") from exc
        if not isinstance(result, dict):
            raise LingxiError("登录响应格式不正确。")
        bean = result.get("bean") or {}
        token = bean.get("authCode") if isinstance(bean, dict) else None
        if str(result.get("rtnCode")) != "0":
            code = str(result.get("rtnCode", "unknown"))
            safe_code = code if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', code) else 'unknown'
            raise LingxiError(f"灵犀换票服务未接受当前会话（{safe_code}），请稍后在灵犀中重连；共享 APP 登录会独立核对。",
                              authentication=True, code=safe_code)
        if not isinstance(token, str) or not token:
            raise LingxiError('灵犀换票响应缺少可验证会话，请稍后重试。', code='UPSTREAM_FORMAT')
        return token


def parse_sse(lines: Iterable[bytes | str]) -> Iterator[tuple[str, str]]:
    """Handle CRLF, comments, multi-line data and an event at EOF."""
    event = "message"
    data: list[str] = []
    size = 0
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="strict")
        line = line.rstrip("\r\n").lstrip("\ufeff")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data, size = "message", [], 0
            continue
        if line.startswith(":"):
            continue
        key, separator, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if key == "event" and separator:
            event = value
        elif key == "data" and separator:
            size += len(value.encode("utf-8"))
            if size > MAX_EVENT_BYTES:
                raise LingxiError("单条回复数据过大，已停止接收。")
            data.append(value)
    if data:
        yield event, "\n".join(data)


def chat_lines(response):
    """Honor the observed HTTP/1.1 EOF-delimited response boundary.

    This endpoint sends Connection: close with neither length nor chunking and
    omits TLS close_notify. BoringSSL reports that FIN as curl 56 after all data.
    Resets, timeouts and prematurely closed framed bodies still raise errors.
    """
    try:
        yield from response.iter_lines()
    except RequestException as exc:
        eof_delimited = (
            response.headers.get("connection", "").lower() == "close"
            and not response.headers.get("content-length")
            and not response.headers.get("transfer-encoding")
        )
        if not (eof_delimited and getattr(exc, "code", None) == 56 and "Connection closed abruptly" in str(exc)):
            raise


@dataclass
class Account:
    auth_code: str = field(default="", repr=False)
    verified: bool = False
    session_id: str = field(default_factory=lambda: secrets.token_hex(16))
    device_model: str = ""
    app_cookie: str = field(default="", repr=False)
    app_phone: str = field(default="", repr=False)
    app_profile: dict = field(default_factory=dict, repr=False)
    lock: Lock = field(default_factory=Lock, repr=False)

    def clear_lingxi(self):
        self.auth_code = ""
        self.verified = False

    def clear(self):
        self.clear_lingxi()
        self.session_id = secrets.token_hex(16)
        self.device_model = ""
        self.app_cookie = self.app_phone = ""
        self.app_profile = {}

    def status(self):
        return {"has_credentials": bool(self.auth_code), "verified": self.verified, "device_model": self.device_model,
                "phone_masked": self.app_phone[:3] + '****' + self.app_phone[-4:] if self.app_phone else ''}


def chat_payload(message: str, session_id: str) -> dict:
    page = "https://chinamobileapp/rn/common/search"
    return {
        "sessionId": session_id,
        "type": 2,
        "requestId": secrets.token_hex(8),
        "msg": message,
        "tokenId": "",
        "inputType": "TextInput",
        "extInfo": {
            "device": {"netType": "wifi", "osType": "android", "appVersion": "12.0.9"},
            "scene": {"initPageUrl": page, "latestPageUrl": page, "pageTitle": "搜索", "pageType": 1},
            "productExplain": [],
            "orderExplain": [],
            "location": {},
            "uiState": "FullScreen",
        },
        "videoEnabled": False,
    }


def chat(account: Account, message: str, *, before_send=None) -> Iterator[dict]:
    if not account.auth_code:
        raise LingxiError("请先输入手机号和验证码登录。", authentication=True)
    if not account.lock.acquire(blocking=False):
        raise LingxiError("上一条消息仍在接收，请稍候或停止接收。")
    response = None
    try:
        with requests.Session(impersonate="chrome") as client:
            if before_send is not None:
                before_send()
            response = client.post(
                CHAT_URL,
                json=chat_payload(message, account.session_id),
                headers={
                    "sdkAppkey": "zgydAppSDK",
                    "Content-Type": "application/json",
                    "authtoken": account.auth_code,
                    "Referer": "https://wx.10086.cn/",
                    "Origin": "https://wx.10086.cn/",
                    "Accept": "text/event-stream",
                    "Cache-Control": "no-cache",
                },
                timeout=(10, 90),
                # HTTP/1.1 exposes this server's EOF-delimited framing; see
                # chat_lines for its legacy TLS shutdown behavior.
                http_version=CurlHttpVersion.V1_1,
                allow_redirects=False,
                stream=True,
            )
            if response.status_code in (401, 403):
                account.clear_lingxi()
                raise LingxiError("灵犀会话已失效，可使用现有共享账号重新连接。", authentication=True)
            if response.status_code != 200:
                raise LingxiError(f"聊天服务返回 HTTP {response.status_code}。")
            if "text/event-stream" not in response.headers.get("content-type", "").lower():
                raise LingxiError("聊天服务没有返回预期的流式回复。")
            received = False
            for event, data in parse_sse(chat_lines(response)):
                if data.strip() == "[DONE]":
                    break
                try:
                    item = json.loads(data)
                except ValueError as exc:
                    raise LingxiError("聊天服务返回了无法解析的数据。") from exc
                if not isinstance(item, dict):
                    raise LingxiError("聊天回复格式不正确。")
                code = item.get("code")
                if str(code) in ("401", "403"):
                    account.clear_lingxi()
                    raise LingxiError("灵犀会话未通过鉴权，可重新连接此共享账号。", authentication=True)
                if str(code) != "0":
                    raise LingxiError(f"灵犀返回错误（{str(code)[:60]}）。")
                account.verified = True
                part = item.get("data")
                if not isinstance(part, dict):
                    continue
                kind, content = part.get("type"), part.get("content")
                if kind == "Txt" and isinstance(content, str):
                    try:
                        structured = json.loads(content)
                        answer = structured.get("aBeans") if isinstance(structured, dict) else None
                        if isinstance(answer, dict) and isinstance(answer.get("a"), str):
                            question = answer.get("q")
                            content = (question + "\n" if isinstance(question, str) and question else "") + answer["a"]
                    except ValueError:
                        pass
                    received = True
                    yield {"type": "delta", "text": content}
                elif kind == "Clear":
                    received = False
                    yield {"type": "clear"}
                elif kind in ("Bubble", "Card"):
                    if kind == "Card" and isinstance(content, dict) and str(content.get("cardNumber")) == "-1000":
                        # Internal lookup progress is not a user answer.
                        progress = content.get("data") or {}
                        if isinstance(progress, dict) and isinstance(progress.get("msg"), str):
                            yield {"type": "progress", "text": progress["msg"]}
                        continue
                    received = True
                    yield {"type": "card", "kind": kind, "content": content}
                elif kind:
                    yield {"type": "event", "kind": kind, "content": content}
            if not received:
                raise LingxiError("连接已结束，但没有收到文字或卡片回复。")
            yield {"type": "done", "verified": account.verified}
    finally:
        if response is not None:
            response.close()
        account.lock.release()
