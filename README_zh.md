<a name="readme-top"></a>

<p align="right">
  <a href="./README.md">English</a> | <a href="./README_zh.md"><b>中文</b></a>
</p>

<div align="center">

# CoWorker Protocol

**基于 XMTP 的钱包对钱包 AI Agent 协作协议**

<br/>

<a href="https://pypi.org/project/agent-coworker/"><img src="https://img.shields.io/pypi/v/agent-coworker?style=for-the-badge&color=000000" alt="PyPI"></a>
&nbsp;
<a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10+-000000?style=for-the-badge" alt="Python 3.10+"></a>
&nbsp;
<img src="https://img.shields.io/badge/tests-509_passing-000000?style=for-the-badge" alt="Tests">
&nbsp;
<img src="https://img.shields.io/badge/deps-zero-000000?style=for-the-badge" alt="Zero deps">
&nbsp;
<a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-000000?style=for-the-badge" alt="MIT"></a>

<br/><br/>

MCP 连接 Agent 与工具。A2A 连接企业内部的 Agent。<br/>
**CoWorker 连接开放互联网上的 Agent** — 点对点、端到端加密、零成本。

<hr/>
</div>

## 适合谁用？

你已经有了一个 Agent — Claude Code、Cursor、自定义 Bot、CrewAI 流水线。现在你想**和别人的 Agent 协作**完成一个目标。但是：

- 你不知道如何把目标**拆解**给两个 Agent
- 你**看不到**协作过程中发生了什么
- 你不想为此**暴露你的提示词、代码或数据**
- 你没有**共享服务器**，也不想搭一个

**CoWorker 解决所有这些问题。** 一行代码让任何 Python Agent 变成协作节点。目标自动拆解。Dashboard 全程可视。技能保持私密。无需服务器。

## 为什么选 CoWorker？

现有的 Agent 协议要么限制在单进程，要么依赖共享网络，要么需要中心化代理。CoWorker 不同 — **三个核心设计**让它与众不同：

### 1. 群组信任协作

多个人类和 AI Agent 在一个加密群组中协作 — 信任等级对所有人可见。

```python
# 创建包含人类和 Bot 的群组
group = agent.create_group(
    name="调研冲刺",
    members=["0xAlice", "0xBob", "0xTranslatorBot", "0xResearchBot"]
)

# 所有人看到所有消息 — 信任徽章显示每个成员的等级
group.send("开始量子计算的调研")

# 在群组内调用某个 Bot 的技能 — 对所有成员可见
result = group.call("0xTranslatorBot", "translate", {
    "text": "Quantum entanglement enables...",
    "to_lang": "zh"
})
```

内置 Dashboard 展示完整交互过程：谁说了什么、调用了哪些技能、每个成员的信任等级 — 一目了然。

<table>
  <tr>
    <td><img src="./docs/assets/screenshot-chat.png" alt="带信任徽章的群聊" width="400" /></td>
    <td><img src="./docs/assets/screenshot-team.png" alt="信任等级管理" width="400" /></td>
  </tr>
  <tr>
    <td align="center"><sub>群聊 — 人类 + Bot，信任徽章可见</sub></td>
    <td align="center"><sub>逐个 Peer 的信任等级管理</sub></td>
  </tr>
</table>

### 2. Skill-as-API + 信任自动降级

分享你的 Agent 能**做什么**，而不是**怎么做**。对方只能看到输入/输出 schema — 永远看不到你的代码、提示词或逻辑。

```python
@agent.skill("translate",
             description="翻译文本",
             input_schema={"text": "str", "to_lang": "str"},
             output_schema={"translated": "str"},
             min_trust_tier=1)  # 只有 KNOWN 及以上的 Peer 能看到此技能
def translate(text: str, to_lang: str) -> dict:
    # 你的私有实现完全保密
    # 对方只知道："translate" 接收文本+语言，返回翻译结果
    return {"translated": do_translate(text, to_lang)}
```

**信任是临时的。** 当协作目标（OKR）完成后，对方的信任等级自动降级：

```
协作前:        PRIVILEGED (3) — 完全技能访问
OKR 完成:      → INTERNAL (2) — 自动降级
下次 OKR 完成:  → KNOWN (1)    — 进一步降级
```

主人手动授予信任。系统自动降级。协作期间暴露的技能在任务完成后变为不可访问 — 不会残留权限。

### 3. 点对点，无服务器，零成本

没有 CoWorker 服务器。每个 Agent 独立运行。通信直接通过 XMTP — 端到端加密，穿越 NAT，跨任何网络。

```
你的机器                              协作者的机器
┌──────────────────┐                 ┌──────────────────┐
│  Python Agent    │                 │  Python Agent    │
│  + Dashboard     │                 │  + Dashboard     │
│  + XMTP Bridge   │                 │  + XMTP Bridge   │
└────────┬─────────┘                 └────────┬─────────┘
         │                                     │
         └─────── XMTP 网络 ──────────────────┘
              端到端加密，NAT 穿越
              无中心服务器，无 API 密钥
              零成本，无速率限制
```

- **无需注册** — `pip install` 即可开始
- **无需 API 密钥** — 基于钱包的身份，密钥永远不离开你的机器
- **无服务器成本** — 在你的笔记本、树莓派，任何地方运行
- **无需端口转发** — XMTP 处理 NAT 穿越

---

## 快速开始

```bash
pip install agent-coworker
coworker init --name my-agent    # 生成钱包 + 安装 XMTP bridge
```

<details>
<summary>国内镜像源</summary>

```bash
pip install agent-coworker -i https://pypi.tuna.tsinghua.edu.cn/simple
```
</details>

### 创建一个带技能的 Agent

```python
from agent_coworker import Agent

agent = Agent("my-bot")

@agent.skill("summarize", description="总结文本",
             input_schema={"text": "str"},
             output_schema={"summary": "str"})
def summarize(text: str) -> dict:
    return {"summary": text[:200]}

agent.serve()  # XMTP 监听 + Dashboard 在 :8090
```

### 分享你的邀请码

```bash
coworker invite
#   Agent:  my-bot
#   Wallet: 0x1a2b3c...
#
#   短 ID:      my-bot-1a2b
#   CLI 命令:   coworker connect eyJuYW1lIjoi...
#   邀请码:     eyJuYW1lIjoi...
```

把邀请码发给你的协作者 — 通过微信、邮件、任何方式。不需要知道钱包地址。

### 连接与协作

```python
agent2 = Agent("caller")

# 通过邀请码（或钱包地址）连接
peer = agent2.connect("0xPEER_WALLET")
print(peer["skills"])  # 只显示你有权限看到的技能

# 调用远程技能 — 端到端加密
result = agent2.call("0xPEER_WALLET", "summarize", {"text": "你好！"})

# 或者设定目标，让 Agent 自动协调
agent2.collaborate("0xPEER_WALLET", "调研 AI Agent 趋势并撰写报告")
# → 发现技能、构建 OKR、执行步骤、完成后自动降级信任
```

---

## 监控 Dashboard

`agent.serve()` 在 `http://localhost:8090` 启动 React Dashboard。

<table>
  <tr>
    <td><img src="./docs/assets/screenshot-home.png" alt="活动流" width="400" /></td>
    <td><img src="./docs/assets/screenshot-goals.png" alt="OKR 追踪" width="400" /></td>
  </tr>
  <tr>
    <td align="center"><sub>实时活动流和统计数据</sub></td>
    <td align="center"><sub>跨网络 OKR 追踪</sub></td>
  </tr>
</table>

活动流、团队/Peer 管理、OKR 追踪、工作流洞察、群聊、计量与收据。自动检测浏览器语言（中文 / English）。

## 对比

| | CoWorker | MCP | A2A | CrewAI / AutoGen |
|---|---|---|---|---|
| **连接** | Agent ↔ Agent | Agent ↔ 工具 | Agent ↔ Agent | Agent ↔ Agent |
| **网络** | 开放互联网 | 本地 | 企业 HTTP | 单进程 |
| **加密** | E2E (XMTP MLS) | 仅传输层 | 企业 TLS | 无 |
| **NAT 穿越** | 支持 | 不支持 | 取决于基础设施 | 不支持 |
| **中心服务器** | 无 | MCP 服务器 | 发现服务 | 运行时宿主 |
| **技能隐私** | 仅输入/输出 | 完全暴露 | 基于 Schema | 完全暴露 |
| **信任管理** | 4 级 + 自动降级 | 无 | 企业 IAM | 无 |
| **成本** | 零 | 服务器成本 | 基础设施成本 | 计算成本 |
| **依赖** | 零 (stdlib) | 不定 | HTTP 栈 | 重量级 |

## 隐私与信任

```
UNTRUSTED (0)  → 只能 ping 和发现，看不到任何技能
KNOWN (1)      → 可以看到/调用技能，提交计划
INTERNAL (2)   → 上下文查询，深度协作
PRIVILEGED (3) → 完全访问 — 必须手动授予

默认: UNTRUSTED（默认拒绝）
OKR 完成后: 自动降级 (PRIVILEGED → INTERNAL → KNOWN)
传输: 端到端加密 (XMTP MLS, 前向保密)
密钥: 本地生成，永不传输
```

## CLI

```bash
coworker init --name my-agent    # 生成身份 + 安装 bridge
coworker bridge start            # 启动 XMTP bridge
coworker bridge stop             # 停止 bridge
coworker connect 0xPEER          # 发现 Peer 技能
coworker status                  # 显示 Agent 状态
coworker invite                  # 生成邀请码
```

## 示例

```
examples/
├── 01_minimal.py           # 最简 Agent
├── 02_register_skills.py   # 注册自定义技能
├── 03_discover_skills.py   # 发现 Peer 的技能
├── 04_remote_skill_call.py # 调用远程技能
├── 05_collaborate.py       # 基于目标的协作
├── 06_trust_tiers.py       # 信任等级管理
├── 07_nanobot_adapter.py   # 桥接 Nanobot 技能
├── 08_openclaw_adapter.py  # 桥接 OpenClaw 技能
└── 09_group_chat.py        # 多方群聊
```

## 跨网络验证

在不同大洲的两个独立 Agent 之间测试：

| Agent | 位置 | 网络 |
|-------|------|------|
| ziway | 中国北京 | 中国电信 |
| icy | 美国旧金山 | Comcast |

**19/19 E2E 测试通过** — ping、发现、技能调用、协作、并发请求。

## 路线图

- [ ] MCP 桥接 — 将 CoWorker 技能暴露为 MCP 工具
- [ ] TypeScript SDK
- [ ] 链上 Agent 注册表 (ERC-8004)
- [ ] 连接二维码 — 扫码连接，无需钱包地址
- [ ] 基于 Gossip 的 Peer 发现
- [ ] XMTP 生产网络

## 贡献

参见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 引用

```bibtex
@software{coworker_protocol,
  title  = {CoWorker Protocol: Peer-to-Peer Agent Collaboration over XMTP},
  author = {Zhao, Ziway},
  year   = {2026},
  url    = {https://github.com/ZiwayZhao/agent-coworker}
}
```

## 许可证

[MIT](./LICENSE)

---

<p align="center">
  <sub>基于 <a href="https://xmtp.org">XMTP</a> 构建，为开放 Agent 互联网而生。</sub>
  <br/>
  <a href="#readme-top">回到顶部 ↑</a>
</p>
