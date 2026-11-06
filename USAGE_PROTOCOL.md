# APP 余量查询协议

## 范围与证据

实现位于 `usage_query.py`，只通过 Python 复用所选共享账号的 APP 凭据查询，不执行 JavaScript、浏览器、WebView、短信登录或主动换票。`AppLoginSession` 负责项目已有的 APP 请求封装、AES 与签名；本模块不另建认证域。

协议来源是此前保存的官方 RN 包 `analysis/phone-bundles/marginQuery-1786347501.jsbundle`，SHA-256 为 `dc823e87b9565dd38f118d2d3b307d796603627a15a83a885b7c50ff49a00824`。该缓存包不是本次从手机私有目录新读取的文件，也不能仅凭静态包证明当前服务或手机运行时的返回内容。

精确选取完整 Metro 模块，保留源文件行号、字节范围与单模块 SHA-256：`analysis/usage-protocol/source-manifest.json`。未执行模块初始化代码。REA 留存记录：

- 核心请求、汇总与单位转换：`ev_1c8084fc08676c0ad08fbeb797ced3c5378b7e1cd535dde4d3a8bbf18409aa86`，摘要 `ev_4506112349de93e07c03ca579e450ed97a367b17ce7b6ef93dc6859779ba7e49`。所选 9 个 JavaScript 文件均静态解析成功。
- 共享会话、明细、有效期与说明：`ev_bfb4961692a0ef583165a2a7934f839bb873ad89c214bf8c3e6a8f375572d80d`，摘要 `ev_d161f914bcf096d472db7b112182d4e655fd204690044471c532f39bc4b96c5e`。新增 6 个文件均静态解析成功。
- 完整导出：`analysis/usage-protocol/rea-evidence.json`。REA 的 Metro 动态模块解析与控制流语义仍有未知项；这里的字段映射是对原始完整模块的局部静态核对，不声称观察到了这些模块在当前手机执行。

## 请求与响应

APP 网关为 `https://client.app.coc.10086.cn`。下表请求体均加上所选账号的 `cellNum`，该字段来自共享凭据快照，不接受前端任意手机号。公共合并行为由原包第 88 行模块 `2608101538988` 给出。

| 接口 | 路径 | 附加请求参数 | 主要响应字段 | 原包行号 |
| --- | --- | --- | --- | --- |
| 20085 余量 | `/biz-orange/BH/newPlanRemainQry/getNewPlanRemainQry` | 无 | `rspBody.newPlanRemainQryRes` | 215、216 |
| 20066 套餐说明 | `/biz-orange/BN/userPlanQry/getUserPlanQryNew` | 无 | `rspBody.planQryInfos` | 203、204 |
| 20064 国际及港澳台用量 | `/biz-orange/BH/countryFlowQuery/getFlowInfoQry` | `currentPeriodStartTm`、`currentPeriodEndTm`，当月首日 00:00:00 至末日 23:59:59，格式 `YYYYMMDDHHmmss` | `flowPacketUsageInfoList`、`standardTariffUsageInfoList` | 218、219 |
| 20062 共享用量 | `/biz-orange/BH/userShareInfoQry/getUserShareInfo` | 无 | `rspBody.userShareInfoRes` | 225、226 |

四项查询在一次用户请求内共用一个 HTTP transport；下一次主动刷新使用新的 transport 和最新凭据快照。主余量查询失败直接返回明确错误。辅助查询失败保留已经拿到的主余量，并写入 `notes`；官方限流或恢复后仍需登录时，不继续发送剩余辅助请求。

## 字段与单位

20085 的流量分组为 `planRemianFlowListRes.planRemianFlowRes`，`flowtype` 分别是 `0` 国内通用、`1` 定向、`2` 国内其他、`3` WLAN。汇总使用 `flowSumNum`、`flowUsdNum`、`flowRemainNum`；套餐明细为 `planRemianFlowInfoRes[].flowInfoRes[]`。

语音为 `planRemianVoiceListRes.planRemianVoiceInfoRes`，`voicetype` 为 `0` 国内通用、`1` 国内其他、`2` 国际及港澳台。汇总使用 `voiceSumNum`、`voiceUsdNum`、`voiceRemainNum`；套餐明细为 `planRemianVoiceRes[].voiceInfoRes[]`。

短彩信为 `planRemianMSGInfoListRes`，汇总 `planRemianMSGRes` 的 `mSGType=0/1` 表示短信/彩信，数量使用 `mSGSum`、`mSGuserd`、`mSGresSum`；明细 `planRemianMSGInfoRes[].msgInfoRes[]`。其他资源保留 `planRemianOtherInfoRes`。各类套餐外记录使用 `outPlanInfoRes[].usageAmount`，作为独立明细展示；通用概览的已用量按官方汇总逻辑纳入明确的套餐外用量。

| 适用接口 | 官方单位码与含义 |
| --- | --- |
| 20085 | `01` 分钟、`02` 条、`03` MB、`04` GB、`05` 次、`06` 天；套餐外另兼容官方的 `1` MB |
| 20066 | 流量 `03` MB、`05` GB；资源类型 `0` 为通话、`1/4` 为流量、`2` 为短信 |
| 20064 | 流量字段直接为 MB，通话字段为分钟；另有天数维度 |
| 20062 | `01` 分钟、`03` KB、`04` MB、`05` GB |

三套单位字典不可混用。保留官方数字字符串精度和真实零，缺失显示 `null`。不为缺失字段补零，不根据总量反算余量，不相加通用与定向套餐，不给重叠资源制造总数。仅在官方汇总逻辑要求纳入套餐外流量时，以精确 Decimal 按 `1GB=1024MB` 统一单位。有限套餐的原始 MB/GB 不做有损的小数截断。

界面另用 Python Decimal 生成 `display`，各用量按大小显示 KB/MB/GB，最多两位小数向零截断，正值小于最小展示精度时显示 `<0.01`，避免错误显示为未使用。原始数字和单位不变，可展开“查看原始用量”。`remaining_percent` 只由同一项目已返回且有效的总量、剩余量计算；缺失、不限量、总量为零或剩余超出范围时不生成进度条，不推算外层套餐的汇总量。单资源套餐在界面合并展示名称与资源用量；多资源仍分别显示，重复范围、有效期和说明仅显示一次，差异字段保留。长说明默认折叠。

`N` 及原包 `is20085BigData`/`is20062BigData` 明确识别的上限哨兵保留为 `unlimited=true`，不展示为可用有限总量。主卡共享明细中的成员手机号掩码显示；副卡仅展示当前所选号码的成员用量。不同官方查询的汇总与明细分别保留，不跨套餐、范围或账号合并。

`isMultiTerm=0/1` 表示可共享/不可共享，`flowContinueFlag=0` 表示上月结转，`gATFlag=0` 表示国内含港澳台。`validDate`、`expireDate`、`expireFlag`、日租/产品累计维度、业务说明与适用 APP 范围均保留。20066 是套餐说明，不作为实时剩余量；20064 资源包的流量、语音、天数及激活时间分开保留。

## 本地 API 与凭据边界

`GET /api/usage/query?login_id=...` 返回 `overview`、`groups`、`notes`、`queried_at` 和公开 `account`。`overview` 可以有多条相同 `key`，由 `name`/`scope` 区分通用、定向、其他等范围。`groups[].items[].children` 保留套餐和资源层级。

同一账号的并发查询合并为一个 Future，调用方拿到独立的结果副本。不同账号使用不同凭据快照与 transport。普通查询不登录、不初始化，不因凭据修订号变化而续期。缺失 Cookie 或官方明确的 `400007`、`400009`、`410000`、`420000` 只允许在当前只读请求中通过传入的共享恢复回调恢复一次；回调复用平台现有的同账号恢复合并能力，恢复后重读凭据并核对号码。认证仍失败、需人工验证或官方限流时返回提示。`228000` 等普通业务失败不自动转换身份，不发送短信。

## 已验证与未验证

`python -m unittest tests.test_usage_query -v` 覆盖数值和单位、通用/定向隔离、零/缺失/无限量、共享成员与副卡隔离、日租和累计维度、请求参数与连接生命周期、并发合并、凭据恢复上限、账号切换拒绝、限流不重试及 Cookie 回写。合成响应测试验证 Python 映射和边界，不能替代所选有效号码的官方只读实测。

2026-11-06 主任务通过普通 ADB 打开尾号 6815 的官方余量页，确认页面分为流量、通话、短彩信、其他，展示通用/定向/其他流量及共享包有效期，未使用注入或抓包。另以本地已有效登录的尾号 1621 进行一次纯 Python 只读查询：20085、20066、20062 返回 `000000`，得到流量、通话、短信及套餐明细；国际及港澳台接口返回 `213080`（该产品不存在或已下架），主余量保留并附明确提示。两号不同，这次验证证明当前主查询接口可用，不作为同号数值一致性证明；没有批量查号、登录或发送短信。
