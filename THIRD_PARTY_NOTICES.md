# Third-party components

The portable package includes Python, Flask and dependencies, curl_cffi, cryptography, Waitress, pystray, Pillow, Node.js, Playwright Core, and Chrome for Testing.

Component license texts are preserved in `_internal/third-party-notices`, `_internal/recharge/node_modules/playwright-core/LICENSE`, and the bundled Chrome directory. Python license text is included by PyInstaller's runtime collection. Chrome component credits are available by opening `chrome://credits` in the bundled browser.

- Python: https://www.python.org/psf/license/
- PyInstaller (bootloader exception): https://pyinstaller.org/en/stable/license.html
- Node.js: https://github.com/nodejs/node/blob/main/LICENSE
- Playwright Core: https://github.com/microsoft/playwright/blob/main/LICENSE
- Chrome for Testing: https://googlechromelabs.github.io/chrome-for-testing/

Build-time versions, download locations, and SHA-256 checksums of the bundled external runtimes are recorded in `build_tools/runtime-lock.json` in the source repository. The release archive checksum is supplied separately as `SHA256SUMS.txt`.
