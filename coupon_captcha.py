"""Fixed-origin transport for the official, human-operated coupon captcha.

The browser supplies all challenge data. This module neither solves challenges
nor changes their origin, response, or verification tokens.
"""
from pathlib import PurePosixPath
import re
from urllib.parse import urlencode

from flask import Response, jsonify, request

from http_transport import requests


OFFICIAL_ORIGIN = 'https://yxfk.market.chinamobile.com:8081'
CAPTCHA_ROOT = 'mgt/decision-captcha'
APP_ID = 'e1f92650d20263d38dbe327e7237f516'
STATIC_EXTENSIONS = {'.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.woff', '.woff2', '.ttf'}
APPLY_PATHS = {CAPTCHA_ROOT+'/api/'+name for name in ('a', 'a2', 'a3', 'p1')}
VERIFY_PATHS = {CAPTCHA_ROOT+'/api/'+name for name in ('v1', 'v2', 'v3')}
DEVICE_PATH = 'mgt/decision-engine/udid/c1'
WALLET_RESOURCES = {
    'wallet/index-private-web_a54cc0c.js': 'https://touch.10086.cn/i/mobile/component/index-private-web_a54cc0c.js',
    'wallet/udid/c1': 'https://touch.10086.cn/i/mobile/tfk/mgt/decision-engine/udid/c1',
}


def register_coupon_captcha(app, *, session_factory=None):
    factory = session_factory or requests.Session
    app_ids = app.extensions.setdefault('coupon_captcha_app_ids', {APP_ID})

    @app.route('/coupon-verification/<path:resource>', methods=['GET', 'POST'])
    def coupon_verification_proxy(resource):
        # Validate the only untrusted routing input at this boundary.
        if (not re.fullmatch(r'[A-Za-z0-9_./-]+', resource)
                or any(part in ('', '.', '..') for part in resource.split('/'))):
            return jsonify(error='验证资源地址无效。'), 404
        is_static = (resource.startswith(CAPTCHA_ROOT+'/zw-captcha/')
                     and PurePosixPath(resource).suffix.lower() in STATIC_EXTENSIONS)
        is_logo = (resource.startswith(CAPTCHA_ROOT+'/api/logo/')
                   and resource.rsplit('/', 1)[-1] in app_ids)
        if resource == 'wallet/index-private-web_a54cc0c.js':
            methods = {'GET'}
        elif resource == 'wallet/udid/c1':
            methods = {'GET', 'POST'}
        elif is_static or is_logo or resource in APPLY_PATHS:
            methods = {'GET'}
        elif resource in VERIFY_PATHS:
            methods = {'POST'}
        elif resource == DEVICE_PATH:
            methods = {'GET', 'POST'}
        else:
            return jsonify(error='不支持的验证资源。'), 404
        if request.method not in methods:
            return jsonify(error='验证资源不支持此请求方式。'), 405

        # Never pass the local account Cookie/Authorization to the external SDK.
        headers = {'Origin': request.headers.get('Origin') or request.host_url.rstrip('/'),
                   'User-Agent': request.headers.get('User-Agent', '')}
        for key in ('Referer', 'Content-Type', 'Accept'):
            if key in request.headers:
                headers[key] = request.headers[key]
        if resource in (DEVICE_PATH, 'wallet/udid/c1'):
            for key in ('appId', 'param'):
                if key in request.headers:
                    headers[key] = request.headers[key]
        query = (urlencode([(key, value) for key, value in request.args.items(multi=True) if key != 'operation_id'])
                 if resource == 'wallet/udid/c1' else request.query_string.decode('ascii'))
        url = WALLET_RESOURCES.get(resource, OFFICIAL_ORIGIN+'/'+resource)+('?' + query if query else '')
        try:
            body = request.get_data(cache=False) if request.method == 'POST' else None
            if resource == 'wallet/udid/c1':
                upstream = app.extensions['coupon_uses'].wallet_sdk(request.args.get('operation_id', ''),
                    request.method, query, headers, body, request.cookies.get('_zw_kvani5r', ''))
            else:
                with factory() as client:
                    upstream = client.request(request.method, url, headers=headers, data=body,
                        allow_redirects=False, timeout=(5, 15))
            if 300 <= upstream.status_code < 400:
                return jsonify(error='官方验证服务返回了不支持的跳转。'), 502
            return Response(upstream.content, status=upstream.status_code,
                headers={'Content-Type': upstream.headers.get('Content-Type', 'application/octet-stream'),
                         'Cache-Control': 'no-store'})
        except requests.RequestsError:
            return jsonify(error='连接官方验证服务失败，请稍后重试。'), 502
