# TokenCoin 双向通道 Demo 0.4.1

双方安装同一个插件，复制私有邀请串即可连接，无需手动配置局域网或 VPN。推理经供应方节点，文件和工具在使用者电脑执行。

本版新增临时试用保护：按供应方账号的官方整周剩余额度，保留30%。官方读数剩余不高于30%时拒绝新请求，并尽力停止正在传输的共享响应；读取失效时暂停。本人同时使用也会影响余量。官方数据有延迟，已接受的生成可能继续扣量，不能保证精确停在30.00%。

独立的朋友10%预算仍在真实校准中，未启用。临时账号余量保护与朋友独立分账是两种模式，不会用次数或虚构系数替代10%。

## 下载与安装

下载 [v0.4.1 实验安装包](https://github.com/youshangdekongjunsiling-spec/tokencoin-channel/releases/tag/v0.4.1)，完整解压，保留隐藏的 `.agents` 目录。

- Windows x64：双击 `install-windows.cmd`，然后 `start-panel-windows.cmd`。
- Apple 芯片 Mac：进入解压目录执行 `bash install-macos.command`，然后 `bash start-panel-macos.command`；使用原生 ARM64 Python。
- 需要 Python 3.10+ 和 Codex CLI。安装器校验固定版本 iroh 1.2.0。
- 重开 Codex 后也可查找 TokenCoin 双向通道。宿主侧栏位置尚未保证；独立面板提供相同功能。每台机器只打开一个面板。

[双人安装与试用指南](codex-channel/INSTALL.md) · [额度口径与边界](codex-channel/WEEKLY_BUDGET.md) · [高级命令行](codex-channel/README.md)

## 建立通道

1. 供应方在面板完成自己的 Codex 授权，选择快速连接与真实订阅，保持30%保底选中，启动共享。
2. 选择“临时试用·保留30%官方周余量”，生成邀请并私下交给朋友。朋友只需粘贴邀请，无需登录供应方账号。
3. 朋友选择独立工作目录，先只读运行“只回答连接成功”，通过后再在示例副本中测试修复与运行测试。
4. 两端保持在线。供应方随时可撤销；通道最晚在本次官方周重置或当前授权截止时到期，不自动续期。保护启动的最长授权为24小时。
5. 反向共享时交换角色，另建独立通道。每个朋友与本人测试的请求分别归属。

```text
朋友 Codex CLI → 本机转接 → iroh 加密直连 / 中继 → 供应方网关 → 供应方模型授权
```

供应方能看到模型请求上下文。账号凭证留在供应方；邀请串含受限通行证，请勿公开。公共中继适合开发试用，有限流且无可用性保证。

## 验证范围

- 109项 Windows 本机回归全部通过；覆盖预算阈值、读数异常、窗口变化、撤销、任务归属、在途响应取消及面板接口。阈值边界使用受控模拟官方数据；没有故意消耗真实账号到30%。
- 新版真实官方额度读取通过；独立本人通道的真实 Codex CLI完成读取文件、修复单行bug、运行3项测试，3次上游调用完成，测试文件未改。验收后测试通道已撤销。
- 网络测试包含本机双端点与公共中继连通。macOS实机和朋友异地双机仍待试用，不能把本机时延当作异地实测。
- 12项新校准任务已完成8训练/4独立验证采样，完整官方标签尚未返回；未附带真实拟合系数，未宣称独立10%预算完成。

[本版验收证据](docs/reserve-verification.json) · [此前额度实验](docs/weekly-meter-verification.json) · [快速连接历史证据](docs/verification.json)

```sh
python -B codex-channel/scripts/setup_quick_connect.py
python -B -m unittest discover -s codex-channel/tests -v
```

上游服务决定订阅可共享范围。包内只有源码、说明与测试，不含账号、邀请、个人聊天或上游二进制。
