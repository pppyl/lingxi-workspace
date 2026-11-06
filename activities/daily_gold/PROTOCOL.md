# 天天转金喜

2026-11-06 使用普通 ADB 在中国移动 APP 搜索结果进入活动，核对规则、首次参与协议与免费转盘；未使用注入、代理或浏览器业务执行。

官方入口：<https://wap.gz.10086.cn/activityWeb/static/templet/act/index.html?actId=V6WJp8>

官方公开 `/activityWeb/ac/nlInit?actId=V6WJp8` 返回组件定义、条件分支与规则。当前规则期限为 2026 年 8 月至 2027-03-31 23:59:59；每天免费一次，当日清零。后台按小时读取这份公开配置，更新截止时间、转盘组件与协议 ID，不为刷新配置登录账号。

静态分析使用 REA 6.4.0，Evidence `ev_9a0c482fa209f786d849cddecf2fc9af77cae4c6c2cfc574fc007b05e59e51fd`。分析目录 SHA-256 `cd97c9214118a63f51a39a76df317081708e83044169f5bdbac86f35a1dc37c2`；含官方 `app.1eb07fc19459fd1c003d.js`、manifest 与 HTML。这是静态调用结构证据，不代表运行结果。

## 已核对协议

- 认证：APP getBigNetToken → `/activityWeb/login` type 4。与每月话费券共用贵州活动认证域；实测同一 bearer 可读取 `/grcNew/initInfo` 与本活动查询。业务进度分别保存；有效凭据按官方到期信息复用，并合并并发初始化。
- 只读进度：GET `/actHandle/restParticipateNum`、`/actHandle/initActivities`。
- 首次参加：POST `/ac/sceneValid` 返回协议分支后，GET `/actHandle/agreementById/{agrId}`，使用本次返回的 `cfmLogId` POST `/actHandle/signAgree`。协议为免费营销活动，没有合约期或保底消费；不替用户订购中奖后的推荐业务。
- 转盘：重新 POST `/ac/sceneValid`，仅官方分支确认可抽奖且返回 `validCode` 时，GET `/actHandle/draw`，传本次组件 ID 和票据。该 GET 具有提交语义，网络或格式异常不能重放。
- 奖品：从官方 `actResource.resourceName`、接收时间解析；提交成功返回的 `prizeName` 是确认结果，发券记录延迟不能把已成功提交降为失败。

## 运行验证与限制

1621 账号的 Python 实测：剩余机会 1 → 签署当前活动协议 → 获得 1 元话费满减券 → 官方奖品列表显示该券、机会变为 0。手机 APP 流程也确认首次协议与转盘中奖结果。

默认北京时间 00:00:01 开始，每天最多尝试 5 次，失败后间隔 1 秒；三项均可设置。成功即停；晚启动和账号恢复会先查询官方进度。验证码/安全验证、限流、不符合资格和结果不确定均停止后台重复提交。轮询只读本地记录；过期或关闭功能停止请求，可通过平台管理永久删除模块。

此记录没有保证奖品或库存；如官方以后增加人工校验，界面会提示由用户在 APP 完成，不自动发送短信或绕过验证。
