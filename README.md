# TokenCoin 双向通道 Demo

同一个 Codex 插件，双方都能管理 **提供自己的调用额度** 和 **使用朋友的节点**。

![双向控制面板](docs/interface.png)

## 下载与安装

从 [Releases](https://github.com/youshangdekongjunsiling-spec/tokencoin-channel/releases) 下载 `tokencoin-channel-v0.2.1.zip`，完整解压，保留隐藏的 `.agents` 文件夹。

- **Windows**：双击 `install-windows.cmd`。
- **macOS**：终端进入解压目录，运行 `bash install-macos.command`。
- 需要 Python 3.10+ 和 Codex CLI。安装器会注册插件市场，并为本机选择 Python 路径。
- 重新打开 Codex，查找 **TokenCoin 双向通道**。插件声明官方全局侧栏和对话面板入口，位置由宿主版本决定。
- 如宿主未刷新，重启 Codex。也可用 `start-panel-windows.cmd` / `bash start-panel-macos.command` 打开相同的本机浏览器面板。一次保持一个控制面板进程。

[完整图形安装与双机连接步骤](codex-channel/INSTALL.md) · [高级命令行指南](codex-channel/README.md)

## 两个方向，分别授权

1. 双方先通过可信局域网或加密 VPN 连通。
2. 提供者在自己的面板登录，填写私网 IP、模型，启动共享并生成限时邀请。
3. 私下发送邀请文件；朋友导入后选择节点、工作目录与权限，运行 Codex 任务。
4. 需要反向使用时，朋友也发一份邀请给你。每个方向可以单独撤销。

下载或复制邀请等同授予限时调用权，请勿公开邀请内容。账号授权始终保留在提供者本机。提供者能看到经通道发送的上下文。

## 当前范围

- 图形面板支持 Codex 订阅适配器与 mock；API 路径保留高级 CLI 入口。
- 面板启动独立 Codex CLI 任务，不切换已有桌面对话的模型。
- 限额按模型请求尝试次数计算，一个开发任务可能含多次请求；不提供金额/token 硬限额。
- 撤销拒绝新请求，已受理请求可能完成。没有自动公网发现、打洞或中继服务。
- 已验证 Windows 本机真实开发调用；新面板双向 mock、授权边界、适配器生命周期已测；新面板真实中文 CLI 任务成功，退出码 0。macOS 和真实双机仍需实机测试。
- 订阅的可共享范围由上游服务规则决定，本项目不改变上游授权条款。
- CLIProxyAPI 固定版本由安装流程从[上游发布页](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.47)单独下载并校验；发行包不包含账号状态或适配器二进制。

## 开发验证

```sh
python -B -m unittest discover -s codex-channel/tests -v
```

入口：`scripts/dashboard.py` 管理本机节点，`scripts/mcp_server.py` 提供 MCP 侧栏，`scripts/channel.py` 转发推理。对应脚本均位于 `codex-channel/`。
