"""Recover public check-in assets with Scrapling, without executing a browser.

Install build_tools/requirements-analysis.txt before using this development tool.
No account cookies, SMS requests or check-in submissions are made here.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests
from scrapling import Selector

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from activities.web_checkin.client import ENTRY, ORIGIN, WEB_UA


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'analysis/web-checkin-public')
    output = parser.parse_args().output
    output.mkdir(parents=True, exist_ok=True)
    sources = []
    with requests.Session(impersonate='chrome', headers={'User-Agent':WEB_UA}) as client:
        index = client.get(ORIGIN+'/qwhdhub/qwhdmark/index.html', timeout=30)
        index.raise_for_status()
        page = Selector(index.content, url=index.url)
        (output/'index.html').write_bytes(index.content)
        for source in page.css('script::attr(src)').getall():
            url = urljoin(index.url, source)
            if urlsplit(url).hostname != 'qwhdcdn.cmcc-cs.cn' or '/index.' not in url:
                continue
            response = client.get(url, timeout=30)
            response.raise_for_status()
            name = Path(urlsplit(url).path).name
            (output/name).write_bytes(response.content)
            sources.append({'url':url, 'file':name, 'sha256':hashlib.sha256(response.content).hexdigest()})
    manifest = {'entry':ENTRY, 'parser':'Scrapling Selector', 'browser_executed':False, 'sources':sources}
    (output/'sources.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == '__main__':
    main()
