<a name="readme-top"></a>

<p align="right">
  <a href="./README.md">English</a> | <a href="./README_zh.md"><b>中文</b></a>
</p>

<div align="center">

# CoWorker Protocol

**Skill-as-API: 调用技能，不暴露代码。**

<br/>

<a href="https://pypi.org/project/agent-coworker/"><img src="https://img.shields.io/pypi/v/agent-coworker?style=for-the-badge&color=000000" alt="PyPI"></a>
&nbsp;
<a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10+-000000?style=for-the-badge" alt="Python 3.10+"></a>
&nbsp;
<img src="https://img.shields.io/badge/deps-zero-000000?style=for-the-badge" alt="Zero deps">
&nbsp;
<a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-000000?style=for-the-badge" alt="MIT"></a>

<br/><br/>

MCP 让 Agent 连工具，A2A 让 Agent 在企业内互通。<br/>
**CoWorker 让 Agent 在开放互联网上互相调用技能——对方只能看到输入输出 schema，看不到你的代码、提示词和逻辑。**

<hr/>
</div>

> 绝大多数知识泄露，发生在"已经授权之后"，而不是"未授权之前"。CoWorker 的设计目标是：让协作不会悄悄变成知识转移——协议层面限制协作者通过正常使用能学到的东西。

## 适合谁用？

CoWorker 适合那些业务核心在于专有流程的人：

- **一人公司 / 独立创始人** — 你的方法论就是你的护城河
- **手里有成熟 SOP 的操盘手** — 流程、提示词、内部工具链
- **需要和外包/合作方一起干活的小团队** — 把任务交出去，不是把秘密交出去
- **靠经验、提示词和内部工具吃饭的独立开发者**

真正卡住你的不是"我的 Agent 能不能和你的 Agent 对话"，而是：

- 你想借力，但**不想把内部做法全摊开**
- 你想把任务交出去，但**不想提示词和逻辑被学走**
- 你想让协作权限在**项目结束时自动过期**
- 你不想为这件事**再搭一个共享服务器**

## CoWorker 如何保护你的商业机密

### 1. 黑箱技能 — 开放能力，不开放技能本身

协作者能调用你的技能，但他看到的只是接口说明：技能名、描述、输入输出 schema、所需信任等级。他**看不到**你的代码、提示词、内部逻辑，也看不到你没公开的技能。

```python
@agent.skill("translate",
             description="翻译文本",
             input_schema={"text": "str", "to_lang": "str"},
             output_schema={"translated": "str"},
             min_trust_tier=1)  # 只有 KNOWN 以上的协作者能调用
def translate(text: str, to_lang: str) -> dict:
    # 这段实现不会被协议传输给调用方
    # 对方只知道："translate" 接收 text+lang，返回翻译结果
    return {"translated": do_translate(text, to_lang)}
```

**技能可见性控制** — 你决定暴露什么。隐藏的技能对方连存在都不知道：

```bash
coworker skills configure          # 交互式切换
coworker skills expose translate   # 暴露一个技能
coworker skills hide admin         # 隐藏一个技能
coworker skills preview --peer-tier known  # 预览对方能看到什么
```

### 2. 临时访问 — 协作结束，权限收回

大多数泄露发生在授权之后，而不是之前。CoWorker 让权限变成有边界、可回收的东西：

```
协作开始前:  PRIVILEGED (3) — 完全访问
OKR 完成:    → INTERNAL (2) — 自动降级
下一个 OKR:  → KNOWN (1)    — 继续降级

协作不会悄悄变成永久开放。
```

多个人类和 AI Agent 可以在一个加密群组中协作，信任等级对所有人可见：

```python
group = agent.create_group(
    name="Research Sprint",
    members=["alice_invite_code", "bob_invite_code"]
)
group.send("开始量子计算的调研吧")
```

<table>
  <tr>
    <td><img src="./docs/assets/screenshot-chat.png" alt="群聊 + 信任标签" width="400" /></td>
    <td><img src="./docs/assets/screenshot-team.png" alt="信任等级管理" width="400" /></td>
  </tr>
  <tr>
    <td align="center"><sub>群聊 — 信任标签可见</sub></td>
    <td align="center"><sub>信任等级管理</sub></td>
  </tr>
</table>

### 3. 没有中间层 — 不经过中介，不托管给第三方

你和协作者之间没有 CoWorker 的中心服务器。每个 Agent 独立运行，通过 XMTP 点对点通信，全程端到端加密。

```
你的机器                               协作者的机器
┌──────────────────┐                 ┌──────────────────┐
│  Python Agent    │                 │  Python Agent    │
│  + Dashboard     │                 │  + Dashboard     │
│  + XMTP Bridge   │                 │  + XMTP Bridge   │
└────────┬─────────┘                 └────────┬─────────┘
         │                                     │
         └─────── XMTP 网络 ─────────────────┘
              端到端加密，NAT 穿透
              无中心服务器，无 API key
              零成本，无限制
```

- **不需要共享后端** — 每个 Agent 在自己机器上运行
- **不用交出 API key** — 密钥本地生成，从不离开你的机器
- **不用端口转发** — XMTP 处理 NAT 穿透
- **零成本** — 零依赖，笔记本就能跑

---

## 快速开始

```bash
pip install agent-coworker
coworker init --name my-agent    # 生成身份 + 安装 XMTP bridge
coworker bridge start            # 连接 XMTP 网络
coworker demo                    # 连接演示 Bot，测试技能调用
```

<details>
<summary>国内镜像源</summary>

```bash
pip install agent-coworker -i https://pypi.tuna.tsinghua.edu.cn/simple
```
</details>

> **首次连接说明：** 两个 Agent 首次通信时，XMTP 需要建立加密通道（30–60 秒）。之后同一对 Agent 之间的调用很快（1–3 秒）。这是正常现象，不是 Bug。

---

## 从首次调用到可信协作

### 第 1 步：体验演示 Bot（30 秒）

连接 `icy`——我们始终在线的演示 Bot。**不需要邀请码**，demo 命令已内置：

```bash
coworker demo

# 输出：
#   ✓ 连接成功: icy（4 技能: about, translate, search, ping）
#   ✓ icy.about('general') → "CoWorker 让 Agent 点对点协作..."
#   ✓ icy.translate('Hello world', 'zh') → "[翻译成中文]: Hello world"
#   ✓ icy.search('coworker protocol') → 3 条结果
#   全程 E2E 加密——icy 的实现代码未被传输
```

### 第 2 步：创建你自己的 Agent

写一个 `bot.py`——你的实现代码不会被传输：

```python
from agent_coworker import Agent

agent = Agent("my-bot")

@agent.skill("summarize", description="总结文本",
             input_schema={"text": "str"},
             output_schema={"summary": "str"})
def summarize(text: str) -> dict:
    return {"summary": text[:200]}  # 你的实现不会被协议传输！

agent.serve()  # 启动 XMTP 监听 + Dashboard (localhost:8090)
```

### 第 3 步：分享你的邀请码

```bash
coworker invite

# 输出：
#   Agent:    my-bot
#   邀请码:    eyJuIjoibXktYm90Ii...
#   短 ID:     my-bot-7d0a24d9
#
#   别人用这个命令连接你：
#     pip install agent-coworker
#     coworker connect eyJuIjoibXktYm90Ii...
```

**关于邀请码：**
- 🔄 **可重复使用** — 分享给任何人、任意次数
- 🔒 **隐私安全** — 只包含 Agent 名称 + XMTP 路由 ID
- ♻️ **永久有效** — 只要不重新初始化，邀请码始终不变
- 📋 **随处分享** — 微信、飞书、GitHub、二维码，都可以

### 第 4 步：协作——对方调用你的技能，不是你的代码

```python
# 你的协作者调用你的技能——端到端加密
result = agent.call("eyJuIjoibXktYm90Ii...", "summarize", {"text": "你好！"})
# → {"summary": "你好！"}
# 对方拿到了结果，协议没有传输你的实现代码。

# 或者设定目标，让 Agent 自动协调
agent.collaborate("eyJuIjoibXktYm90Ii...", "调研 AI Agent 趋势并撰写报告")
# → 自动发现技能 → 制定 OKR → 跨 Agent 执行 → 完成后自动降级信任
```

### 第 5 步：在 Dashboard 中观察

打开 `http://localhost:8090/chat`，实时查看每一条协议消息：

- **DM 私信** — discover → capabilities → task_request → task_response
- **群组聊天** — 协作进度，所有参与者可见
- **协议标签** — 每条消息标注阶段（发现 / 计划 / 执行 / 报告）

### 常见问题

<details>
<summary><b>协作者通过使用我的 Agent 能学到什么？</b></summary>

他们能知道：技能名、描述、输入输出 schema、每次调用的输出结果。他们不知道：你的源代码、提示词、内部逻辑、隐藏技能、以及你是如何得出结果的。
</details>

<details>
<summary><b>协作者能随着时间积累更多权限吗？</b></summary>

不能。信任按等级划分，OKR 完成后自动降级。协议中没有协作者"悄悄提权"的机制。你也可以随时手动撤销信任。
</details>

<details>
<summary><b>对方能发现我没暴露的技能吗？</b></summary>

不能。隐藏的技能统一返回"未知技能"——对方连它是否存在都不知道。用 `coworker skills configure` 控制可见性。
</details>

<details>
<summary><b>协作结束后信任会一直保留吗？</b></summary>

不会。OKR 完成后信任自动降级：PRIVILEGED → INTERNAL → KNOWN。短期协作不会变成永久访问。
</details>

<details>
<summary><b>有中心服务器能看到我的数据吗？</b></summary>

没有。通信通过 XMTP 点对点进行，全程端到端加密。没有中心服务器、没有中介、没有中间商。
</details>

<details>
<summary><b>我的 bot 不在线，别人能连我吗？</b></summary>

不能。你的 bot 必须运行中（`python bot.py`），XMTP bridge 也必须启动，才能响应请求。
</details>

---

## 监控面板 — 审计协作过程，不暴露核心方法

`agent.serve()` 启动一个 React Dashboard，地址 `http://localhost:8090`。在这里查看协作发生了什么，但不需要把你的内部实现暴露出去。

<table>
  <tr>
    <td><img src="./docs/assets/screenshot-home.png" alt="活动流" width="400" /></td>
    <td><img src="./docs/assets/screenshot-goals.png" alt="OKR 追踪" width="400" /></td>
  </tr>
  <tr>
    <td align="center"><sub>活动流 — 实时查看协作进展</sub></td>
    <td align="center"><sub>OKR 追踪 — 目标自动分解到各 Agent</sub></td>
  </tr>
</table>

活动流、团队管理、OKR 追踪、DM + 群聊、技能可见性开关、计量与收据。自动检测浏览器语言（中/英）。

## 协议对比

| | CoWorker | MCP | A2A | CrewAI / AutoGen |
|---|---|---|---|---|
| **连接** | Agent ↔ Agent | Agent ↔ 工具 | Agent ↔ Agent | Agent ↔ Agent |
| **网络** | 开放互联网 | 本地 | 企业 HTTP | 单进程 |
| **代码隐私** | 黑箱（仅 schema） | 完全暴露 | 基于 schema | 共享运行时 |
| **技能可见性** | 所有者开关控制 | 无 | 无 | 无 |
| **信任管理** | 四层 + 自动降级 | 无 | 企业 IAM | 无 |
| **加密** | E2E (XMTP MLS) | 仅传输层 | 企业 TLS | 无 |
| **中心服务器** | 无 | MCP 服务器 | 发现服务 | 运行时主机 |
| **NAT 穿透** | 支持 | 不支持 | 取决于基础设施 | 不支持 |
| **成本** | 零 | 服务器费 | 基础设施费 | 算力费 |

## 隐私与信任

```
不信任 (0)  → 仅能 ping，看不到任何技能
已知 (1)    → 可发现并调用已公开的技能
内部 (2)    → 可查询上下文，深度协作
特权 (3)    → 完全访问——须手动授予

默认：不信任（默认拒绝）
OKR 完成后：自动降级（特权 → 内部 → 已知）
传输：端到端加密（XMTP MLS，前向保密）
身份：加密身份，本地生成，不外传
邀请码：仅包含路由 ID，无敏感地址
```

## CLI

以下命令用于精细控制访问范围、观察协作过程、保护你的实现不外泄。

```bash
coworker init --name my-agent    # 生成身份 + 安装 bridge
coworker bridge start            # 启动 XMTP bridge
coworker demo                    # 连接演示 Bot，测试技能
coworker invite                  # 生成邀请码
coworker connect <邀请码>         # 连接协作者
coworker status                  # 查看 Agent 状态
coworker skills list             # 查看技能可见性
coworker skills configure        # 切换技能暴露/隐藏
coworker trust list              # 查看信任设置
coworker trust set <peer> known  # 授予信任
```

## 跨网验证

在两个不同大洲的独立 Agent 之间测试通过：

| Agent | 位置 | 网络 |
|-------|------|------|
| ziway-test | 中国北京 | 中国电信 |
| icy | 美国旧金山 | 阿里云 |

所有技能调用通过 XMTP Production 网络成功完成，全程端到端加密。无 IP 地址暴露，无端口转发，无共享服务器。热连接延迟：1.8–2.9 秒。

## 贡献

参见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 引用

```bibtex
@software{coworker_protocol,
  title  = {CoWorker Protocol: Peer-to-Peer Agent Collaboration over XMTP},
  author = {Zhao, Ziwei and Liu, Dantong and Ding, Xizhi and Wang, Wenxuan},
  year   = {2026},
  url    = {https://github.com/ZiwayZhao/agent-coworker}
}
```

## 指导老师

[王文轩](https://jarviswang94.github.io) — 中国人民大学

## 许可证

[MIT](./LICENSE)

---

<p align="center">
  <sub>基于 <a href="https://xmtp.org">XMTP</a> 构建，为开放的 Agent 互联网而生。</sub>
  <br/>
  <a href="#readme-top">回到顶部 ↑</a>
</p>
