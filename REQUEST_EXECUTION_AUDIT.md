# 请求执行检查（2026-11-06）

本次检查范围为项目运行代码；构建脚本、协议分析脚本、前端测试使用的 Node.js 不计作业务运行依赖。未使用浏览器执行业务、未发送验证码、领取或支付。

## 已确认的运行路径

| 路径 | 执行方式 |
| --- | --- |
| 账单与余额 `bill_query.py` → `AppLoginSession.call` | Python 加密与 HTTP 请求，无浏览器／Node |
| APP 登录、灵犀、星动日、每月话费券、七日打卡、天天转金喜、微信签到 | 本次运行源码检索未发现调用浏览器／Node 执行业务的入口；各自 Python 客户端直接请求 |
| 周三充值日登录、资格、奖品和领取接口 | 共享 APP 模式使用 Python HTTP；仍保留历史浏览器登录模式 |
| 周三充值日 `refresh` 缺少 `constId` | **残留**：Windows `BrowserBridge` → Node.js／Playwright；Android `AndroidActivityBridge` → Java WebView |
| 卡券列表、用券业务请求 | Python HTTP；人工滑块继续由用户在官方验证组件操作 |
| 卡券兑换设备校验 | **残留**：前端 `prepareExchangeRisk` 执行官方 ConstID SDK，Python 转发绑定本次短信／兑换会话的 `c1` |
| Windows 默认浏览器窗口 | 仅显示本地前端，不属于计算业务参数的环境 |

## 账单切换耗时与已实施的修改

本地 `/bills`、账号列表、导航问题、功能开关和网络状态读取，单次实测约 1—31 毫秒。一次官方只读实测中，账单请求约 2508 毫秒，随后余额请求约 832 毫秒。

修改后，同一次查询的账单和余额复用 Python HTTP 连接池，保留顺序和 Cookie 轮转；查询结束关闭，不跨账号共享。随后单次实测为账单 2592 毫秒、余额 412 毫秒，均返回 `000000`。这只是两次样本，不能当作固定提速比例；官方账单响应仍占主要等待时间。

没有缓存替代主动查询，没有额外登录、初始化或重放业务提交，也没有改变抢购频率或定时。

## 设备校验 Python 化的进度与限制

`recharge/device.py`、`coupon_device.py` 是尚未接入生产业务的协议实现，已覆盖 JSON／CESU-8／自定义 Base64 编码、LID、域与账号绑定、真实 `c1.data` 的成功分支及并发复用。

对两个认证域各执行唯一一次不带账号凭据的匿名预检，官方均返回 HTTP 200、`status=-4`。此时 `data` 是服务器 LID，**不是**可用于领取或兑换的设备令牌。后续仍要求完整 `detect` 环境采集，该部分尚未验证完成。因此没有把这两个原型强行接入，避免新设备初始化失败、把 LID 当令牌或损坏当前可用的验证码流程。不能宣称平台已全部完成 Python 化。

现有成功钱包请求的 `param` 已用 Python 逐字节重编码核对；这证明已观测的预检分支，不证明新设备完整采集已经实现。

脱敏证据位于 `analysis/wednesday-device-python/protocol-findings.json`、`analysis/coupon-risk-sdk/python-protocol-evidence.json` 和 `analysis/coupon-risk-sdk/python-anonymous-prequest.json`。未保存或输出本次匿名标识、令牌或账单原始内容。
