# v1.8.1 · 修复移动权益协议配置缺失

修复 1.8.0 打包版打开「新青年瑞幸」或「AI 豆」时，因缺少加密协议配置而无法查询的问题。Windows 单文件版、文件夹版和安卓独立 APK 均内置所需配置，新设备无需手动补文件。

- 内置新青年兑换券查询、活动验证码、兑换及 AI 豆查询、领取所需的五个接口协议配置，全部由 Python 读取和计算。
- 优先使用数据目录中的 `mobile-rights/gateway.json` 自定义配置；该文件不存在时使用内置 `mobile_rights_gateway.json`。自定义配置错误会明确提示，不静默替换。
- 修正加密配置报错中遗留的旧文件名。

协议配置仅包含加密参数，不包含开发者账号、Cookie、登录凭据、数据库或任务记录。沿用 1.8.0 的功能与会话复用规则，不自动发送活动短信或重放领取请求。

## 下载文件

- `LingxiWorkspace-1.8.1-Android-arm64.apk`：Android 7.0+ ARM64，手机独立运行。
- `LingxiWorkspace-1.8.1-Windows-x64.exe`：Windows 10/11 x64，单文件运行。
- `LingxiWorkspace-1.8.1-Windows-x64.zip`：完整解压后运行 `LingxiWorkspace.exe`，保留 `_internal` 目录。
- `SHA256SUMS.txt`：上述文件的 SHA-256 校验值。

Windows 更新前先从托盘退出旧版，再运行新版；账号、任务和配置保存在 `%LOCALAPPDATA%\LingxiWorkspace`，覆盖程序文件不会清除数据。安卓沿用原签名，请直接覆盖安装，不要先卸载。

定时任务需要设备开机、联网且服务持续运行；安卓休眠或系统后台限制可能延迟执行。活动资格、库存与实际到账以官方结果为准。
