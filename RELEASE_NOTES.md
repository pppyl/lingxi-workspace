# v2.2.5 · Android 与 Windows

- Windows 客户端通过独立的本机接口检查服务是否就绪，业务首页暂时无法渲染时仍可启动到托盘。自检和托盘控制请求直连本机，避免受系统代理影响。
- 修复 Windows 原端口已有本机监听时，全网卡监听仍绑定该端口导致启动自检超时的问题。客户端会改用空闲端口。
- 启动失败时释放本次监听端口并清理运行记录。日志记录实际启动版本、程序路径、数据路径和最近一次 HTTP 或连接错误。
- Windows ZIP 附带只读诊断脚本，方便核对客户端文件和启动日志。
- Android APK 同步升至 2.2.5，沿用原应用标识和签名。

本地回归覆盖 Python 主测试 1662 项（跳过 3 项）、活动测试 194 项和前端测试 301 项。Windows 验证了代理环境、端口占用和旧数据迁移后重启，Android 核对了应用版本、ARM64 架构和签名。

## 下载

- `LingxiWorkspace-2.2.5-Android-arm64.apk` 适用于 Android 7.0+ ARM64 设备。
- `LingxiWorkspace-2.2.5-Windows-x64.exe` 是 Windows x64 单文件版。
- `LingxiWorkspace-2.2.5-Windows-x64.zip` 是 Windows x64 文件夹版，完整解压后运行。
- `SHA256SUMS.txt` 提供三个安装包的 SHA-256 校验值。
