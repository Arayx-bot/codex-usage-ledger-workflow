# Codex 用量账本（独立包）

读取使用者明确提供的本机Codex用量记录，生成本地统计看板，并记录用户明确提供的已付款 AI 费用与退款。用量部分只保留Token、项目、时间、模型及额度快照，不保存消息正文；实际支出保存用户要求记下的商家、用途、原币金额、分类、日期与项目。金额分摊是管理估算，不是官方账单。输出默认private，不自动上传。

Python 3.9+标准库及系统时区数据库；macOS已验证，其他Unix未实机验证，Windows因fcntl不支持。无需GPU、Node或API Key；缺环境说明官方安装来源与网络/空间影响，按使用者选择安装，不静默下载。下载后在包根运行，无需安装第三方Python包。

对能加载SKILL.md及本机文件的AI说：“使用codex-usage-ledger，只读检查我指定的本机采集状态。”Codex可使用自己的Skill目录软链接；其他宿主自动发现未实测。

```sh
python3 app.py --help
python3 app.py --codex-home <自己明确授权的来源目录> --data-dir <用户工作目录>/ledger status
python3 app.py --codex-home <自己明确授权的来源目录> --data-dir <用户工作目录>/ledger collect
python3 -B tests/test_public.py
```

status不创建数据库；collect本地增量写库，不调用模型、不改原始记录。默认来源CODEX_HOME或用户目录.codex，默认数据在Application Support（macOS）或XDG路径，不能把源码公开许可当成读取他人记录的授权。打开看板用open；自定义来源/数据用serve并显式传全局参数。服务只绑定127.0.0.1，会写本机运行状态；不属于严格只读。首次本机数据未采集时报告未知，不用真实资料补测。

install-schedule/remove-schedule仅macOS，写LaunchAgent及可删除的本机字节码；只有本人对该次范围明确授权后执行，不能为了验证安装计划。月费留空直到使用者填写，不推算官方费用或额度。真实配置、数据库、原始会话和本机安装产物不包含在包里。

独立测试仅使用临时合成Token记录：状态不建库、增量去重、正文不持久化；不打开服务、不安装计划、不读取真实聊天。源码私有远程当前为空，既有同步记录差异保持待核对；本包按当前本地已登记源码冻结，未改历史或远程，实际发行前另行处理私有同步停点。MIT为候选许可，正式公开须确认精确清单。

## 实际支出

可以对 AI 说「把这笔已支付的 AI 工具费用记到看板」，提供金额、明确币种、商家和用途；日期未指定按北京时间今天，项目未指定归入共用/未分配。支持分类/项目汇总、退款冲减、误记作废并保留历史、CSV 导出。各币种独立统计，整数最小币种单位存储，不与 Token 估算分摊或月费预算重复相加。网页也可直接记账；打开页面约一分钟读取一次账本。

`expense-add` 的真实参数以 `python3 app.py expense-add --help` 为准。必须提供原币金额、币种、商家、用途和稳定 request-key；同请求重试不新增。同日同商家同金额的其他请求会提示核对，明确是另一笔才允许重复。`expense-list` 只读、不建库；`expense-void --id <编号> --reason <原因>` 保留原记录。退款传正金额、`--kind refund`。支持 CNY/USD/EUR/HKD/GBP/JPY/KRW/SGD/AUD/CAD，JPY/KRW 无小数、其余两位。聊天理解属于正常 AI 对话用量，本机记账统计不调用模型。不自动扫描聊天、扣款或读取支付账户。

公开包的 `python3 -B -m unittest discover -s tests -v` 仅用临时合成记录验证 Token 和费用，不向正式账本写测试付款。运行数据、月费配置与任何真实付款记录均不在发行文件中。
