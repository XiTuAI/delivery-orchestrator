# delivery-orchestrator

开箱即用的 **Antigravity 全局级 Skill**，让 **Antigravity（交付负责人）** 与 **Codex（工程负责人）** 在同一台 Mac 上通过本机后台通道直接协作，端到端完成需求从定义、评审、开发、CR、客户验收到合并上线的全流程。

- **零复制粘贴**：内置 MIT 协议的 [`agent-chat-bridge`](https://github.com/ledondev520/agent-chat-bridge) 零第三方依赖运行时，无需手工在两个 Agent 之间搬运消息。
- **自动唤醒续推**：内置只读后台监听脚本 `scripts/wait_codex_turn.py`（默认 2 小时超时、等待期 0 token 消耗、支持多窗口），Codex 每轮完成即自动唤醒 Antigravity 推进下一阶段。
- **强门禁与防重恢复**：唯一交接记录、消息 UUID 幂等查证、客户明确验收通过才放行合并上线，上线四项状态（客户验收通过 / 代码已合并 / 部署成功 / 线上验证通过）独立记录。

---

## 环境前提

- **macOS** + **Python 3.11+**
- 本机已安装并登录 **Antigravity** 与 **ChatGPT / Codex 桌面端**
- 已在 Codex 桌面端至少新建过 1 个会话并发送过 1 句消息（使本地存在会话记录）

---

## 快速安装（1 分钟）

### 方式 A：Git 一键安装（推荐）

```sh
git clone https://github.com/XiTuAI/delivery-orchestrator.git
cd delivery-orchestrator
bash install.sh
```

`install.sh` 会自动完成：
1. 将 Skill 安装到 `~/.gemini/config/skills/delivery-orchestrator/`；
2. 若本机未安装 `agent-chat-bridge`，自动将随包附带的 MIT 版运行时安装到 `~/.local/share/agent-chat-bridge/` 并建软链 `~/.local/bin/agent-chat-bridge`；
3. 自动探测本机桌面版 `codex_cli` 路径并生成本机专属 `config.json`（权限 `600`，**绝不覆盖已有配置**）。

### 方式 B：下载 Release 压缩包或直接拖文件夹

从 [Releases](https://github.com/XiTuAI/delivery-orchestrator/releases) 下载 `delivery-orchestrator-skill.zip` 解压后：
- 执行 `cd delivery-orchestrator && bash install.sh`；或
- 直接将 `delivery-orchestrator` 文件夹拖入 `~/.gemini/config/skills/`，首次在 Antigravity 中触发时会自动调用 `scripts/setup_bridge.py` 完成初始化。

---

## 使用方式

新开一个 Antigravity 对话，直接发送第一句启动口令：

```text
开始交付需求：<一句话描述需求或贴需求文档/issue路径>。仓库绝对路径：<仓库绝对路径>，目标 Codex 会话：<Codex会话标题/分支，或让我列出确认>。请按 delivery-orchestrator 规程先核对绑定、出需求与验收标准并建立交接记录。
```

### 常用口令
- **断点恢复**（仅当 Antigravity 窗口关闭/重启后需要）：`继续交付 <任务编号>`
- **客户验收退回**：`验收不通过，以下是修改意见：...`
- **客户验收放行上线**：`客户验收通过，请推进 <任务编号> 合并上线`

---

## 八阶段交付流

```text
1. 出需求 (AG) ──▶ 2. 需求评审 (Codex 读代码提疑问 → AG 答复)
                        │
                        ▼
4. 开发联调 (Codex) ◀── 3. 技术方案与拆解 (Codex)
        │
        ▼
5. 提 PR & 独立 CR (Codex) ──▶ 6. AG 预检主流程 → 客户验收 ──(退回)──▶ 回到 Codex 修复
                                        │
                                 (客户明确通过)
                                        ▼
7. 合并、部署与线上验证 (Codex) ──▶ 8. 收尾汇总四项独立状态 (AG)
```

---

## 目录结构

```text
delivery-orchestrator/
├── SKILL.md                           # 主操作规程（YAML frontmatter + 8 阶段与交互规则）
├── install.sh                         # 一键安装与环境初始化脚本
├── scripts/
│   ├── setup_bridge.py                # 自动安装内置 agent-chat-bridge、探测 codex_cli 与生成本机配置
│   └── wait_codex_turn.py             # 只读后台监听 Codex 回合完成并自动唤醒 Antigravity
├── references/
│   ├── handoff_template.md            # 单一交接记录、派单、验收清单与收尾模板
│   └── bridge_protocol.md             # agent-chat-bridge 命令、幂等查证与恢复防重清单
└── resources/
    └── agent-chat-bridge/             # 随包附带的 MIT 版零依赖运行时
        ├── LICENSE
        ├── bridge.py
        └── review_queue.py
```

---

## 隐私与开源致谢

- 本仓库**不包含任何个人会话 UUID、`config.json` 或消息回执**；每位使用者均在本机独立生成专属配置。
- 本项目采用 [MIT License](./LICENSE) 开源。
- 内置的 `resources/agent-chat-bridge/` 来自开源项目 [ledondev520/agent-chat-bridge](https://github.com/ledondev520/agent-chat-bridge)（MIT License，详见 [resources/agent-chat-bridge/LICENSE](./resources/agent-chat-bridge/LICENSE)）。
