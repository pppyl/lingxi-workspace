"""Desktop curl transport; Android uses native TLS/HTTP2 with the same Python API."""
import os

if os.environ.get('LINGXI_ANDROID') != '1':
    from curl_cffi import requests, CurlHttpVersion
    from curl_cffi.requests.exceptions import RequestException
else:
    import httpx
    import json
    from types import SimpleNamespace
    from java import jclass

    RequestException = httpx.HTTPError

    class CurlHttpVersion:
        V1_1 = 1
        V2_0 = 2

    class NativeBody(httpx.SyncByteStream):
        def __init__(self, result):
            self.result = result

        def __iter__(self):
            try:
                while True:
                    block = self.result.read()
                    if block is None:
                        break
                    yield bytes(block)
            except jclass('java.io.IOException') as error:
                raise httpx.ReadError('Android network read failed') from error

        def close(self):
            self.result.close()

    class NativeTransport(httpx.BaseTransport):
        def __init__(self, http2):
            self.http2 = http2

        def handle_request(self, request):
            timeout = request.extensions.get('timeout', {})
            try:
                result = jclass('cn.pppyl.lingxiworkspace.NativeHttp').request(
                    request.method, str(request.url), json.dumps(list(request.headers.multi_items())),
                    request.read(), self.http2, int(timeout.get('connect') or 10), int(timeout.get('read') or 30))
            except jclass('java.io.IOException') as error:
                raise httpx.ConnectError('Android network connection failed', request=request) from error
            return httpx.Response(result.status(), headers=json.loads(str(result.headers())), stream=NativeBody(result))

    class Cookies(httpx.Cookies):
        def set(self, name, value, domain='', path='/', secure=False):
            super().set(name, value, domain=domain, path=path)
            cookie = next(cookie for cookie in self.jar
                          if cookie.name == name and cookie.domain == domain and cookie.path == path)
            cookie.secure = secure

    class Session:
        def __init__(self, **_desktop_options):
            self.clients = {version: httpx.Client(transport=NativeTransport(version == 2), follow_redirects=False)
                            for version in (1, 2)}
            self.headers = httpx.Headers()
            self.cookies = Cookies()

        def request(self, method, url, *, timeout=30, allow_redirects=True, stream=False,
                    http_version=2, headers=None, **kwargs):
            client = self.clients[http_version]
            client.cookies = self.cookies
            merged = httpx.Headers(self.headers)
            if headers:
                merged.update(headers)
            if isinstance(timeout, tuple):
                timeout = httpx.Timeout(timeout[1], connect=timeout[0])
            if isinstance(kwargs.get('data'), (str, bytes)):
                kwargs['content'] = kwargs.pop('data')
            req = client.build_request(method, url, headers=merged, timeout=timeout, **kwargs)
            response = client.send(req, stream=stream, follow_redirects=allow_redirects)
            self.cookies.update(client.cookies)
            # curl exposes get_list; httpx names the same operation get_list too.
            return response

        def get(self, url, **kwargs):
            return self.request('GET', url, **kwargs)

        def post(self, url, **kwargs):
            return self.request('POST', url, **kwargs)

        def close(self):
            for client in self.clients.values():
                client.close()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    requests = SimpleNamespace(Session=Session, Cookies=Cookies, RequestsError=RequestException)
