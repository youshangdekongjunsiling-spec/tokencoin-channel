# TokenCoin 双向通道 Demo 0.4.0

双方安装同一个插件，复制邀请串即可连接，无需手动配置局域网或 VPN。推理经供应方节点，文件与工具在使用者电脑执行。

**本版为实验预发布。** 已接入按朋友分别计算的周比例估算、误差参考范围、到限暂停和官方任务账单替换。默认分享比例 10%。目前没有完整的真实校准样本，安装包不附带真实系数，因此真实百分比邀请尚不能启用。不得用测试参数开放真实共享。

## 下载与安装

下载 [v0.4.0 实验安装包](https://github.com/youshangdekongjunsiling-spec/tokencoin-channel/releases/tag/v0.4.0)，完整解压，保留隐藏的 `.agents` 目录。

- Windows x64：双击 `install-windows.cmd`。
- Apple 芯片 Mac：终端进入解压目录，执行 `bash install-macos.command`，使用原生 ARM64 Python。
- 双方需要 Python 3.10+ 和 Codex CLI。安装器校验固定版本 iroh 1.2.0 并配置插件。
- 重新打开 Codex，查找 TokenCoin 双向通道。宿主侧栏位置仍待实测；也可用包内 `start-panel-windows.cmd` / `bash start-panel-macos.command` 打开同一面板。

[最短操作指南](codex-channel/INSTALL.md) · [周额度估算说明](codex-channel/WEEKLY_BUDGET.md) · [高级命令行与历史验证](codex-channel/README.md)

## 连接与额度

1. 供应方在自己的面板完成 Codex 授权，选择快速连接并启动共享。
2. 百分比模式先导入用真实任务账单拟合并独立验证的校准文件，再选择分享比例和邀请期限。当前真实样本不足，程序会拒绝无参数的百分比邀请。
3. 生成邀请串并私下发送。朋友粘贴邀请后，选择自己电脑的工作目录和权限运行任务。
4. 反向共享时交换角色、另建独立邀请。双方均可撤销。

本人和各朋友的请求分别归属；估算不使用账号总表差额。完整账单按任务替换估算，重复导入不重复扣减。缺少用量时暂停，参考范围上端到限后暂停下一请求；已放行的一次生成可能超额。经验范围没有统计置信度或硬上界含义。

校准和账单目前由供应方手动导入，来源与适用账号需供应方核对。授权按固定截止时间到期，不自动按周续期；账号切换和官方周重置尚未自动识别。旧次数模式保留高级兼容测试，不能代替周比例。API Key 接入仍走高级 CLI。

```text
朋友 Codex CLI → 本机转接 → iroh 加密直连 / 中继 → 供应方网关 → 供应方模型授权
```

供应方能看到模型请求上下文。原始账号凭证留在供应方；邀请串包含受限通行证，请勿公开。公共中继适合 Demo，有限流且无可用性保证；双方均需在线。

## 验证与未完成项

- 本版 96 项本机合成与回归测试通过，无跳过：任务隔离、用量记录、校准、预算暂停、幂等对账、管理权限与快速连接。
- 浏览器核对默认 10%、未知用量显示及撤销操作；界面测试使用合成参数。
- 历史 Windows 真实 Codex CLI 已通过通道完成短句任务与隔离目录修复/测试。新增真实网关探针记录了完整 token 明细，但四个任务的官方周占比仍不可用。
- 真实预测准确度、完整账单闭环、macOS 实机和朋友异地双机均未验收。本机连接时延不能代替双机实测。

[本版证据](docs/weekly-meter-verification.json) · [快速连接历史证据](docs/verification.json)

```sh
python -B codex-channel/scripts/setup_quick_connect.py
python -B -m unittest discover -s codex-channel/tests -v
```

上游服务决定订阅可共享范围。发布包只含源码、说明和测试，不含账号、邀请、个人状态或上游二进制。
