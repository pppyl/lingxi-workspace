# Third-party components

The portable package includes Python, Flask and dependencies, curl_cffi, cryptography, Waitress, pystray, Pillow, Node.js, and Playwright Core. No browser is bundled. The UI opens in the user's default browser; the activity component uses an already-installed Chrome or Edge with an isolated session.

Component license texts are preserved in `_internal/third-party-notices` and `_internal/recharge/node_modules/playwright-core/LICENSE`. Python license text is included with the runtime notices.

- Python: https://www.python.org/psf/license/
- PyInstaller (bootloader exception): https://pyinstaller.org/en/stable/license.html
- Node.js: https://github.com/nodejs/node/blob/main/LICENSE
- Playwright Core: https://github.com/microsoft/playwright/blob/main/LICENSE

Build-time versions, download locations, and SHA-256 checksums of the bundled external runtimes are recorded in `build_tools/runtime-lock.json` in the source repository. The release archive checksum is supplied separately as `SHA256SUMS.txt`.
