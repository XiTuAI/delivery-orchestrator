# `agent-chat-bridge` 交互与恢复操作清单

本机工具路径：`~/.local/bin/agent-chat-bridge`（安装目录 `~/.local/share/agent-chat-bridge`）。
> 为兼容未将 `~/.local/bin` 加入 `PATH` 的新机器，命令统一优先使用 `BRIDGE="${HOME}/.local/bin/agent-chat-bridge"`（或先运行内置初始化脚本）。

---

## 0. 新机器零配置检查与初始化

若本机尚未安装 `agent-chat-bridge` 或缺少 `config.json`，运行内置引导脚本（零第三方依赖，不覆盖已有配置）：

```sh
python3 ~/.gemini/config/skills/delivery-orchestrator/scripts/setup_bridge.py
```

---

## 1. 首次派单前的三要素核对（禁止猜“最近活跃”）

1. **真实仓库绝对路径**：确认与客户需求对应的项目根目录（绝对路径）。
2. **当前 Antigravity 会话 UUID**：从当前上下文 `Conversation ID` 获取。
3. **目标 Codex 会话 UUID**：
   - 先查交接记录中是否已有**已确认绑定**；有则直接复用。
   - 首次绑定时，只读列出本地未归档 Codex 会话供核对（核对 `cwd`、`git_branch`、`title`），**不要直接按最近活跃时间盲猜**；若存在多个候选且客户未指定，向客户确认目标会话后再记录到交接记录中：
     ```sh
     sqlite3 -readonly ~/.codex/state_5.sqlite \
       "SELECT id, cwd, git_branch, substr(replace(title, char(10), ' '), 1, 60) FROM threads WHERE archived=0 ORDER BY updated_at DESC LIMIT 10;"
     ```
   - 严禁在 skill、模板或默认脚本中硬编码任何测试会话 UUID。

---

## 2. 标准发送、查回执与读取命令

### 派单给 Codex（推荐先写入临时文件再用 `--file`，避免多行转义问题）
```sh
agent-chat-bridge send \
  --to codex \
  --chat <目标Codex会话UUID> \
  --from-side antigravity \
  --from-chat <当前AG会话UUID> \
  --file <派单消息文件路径>
```

> 单条消息上限为 **16384 字节（16 KiB UTF-8）**。需求文档、设计稿、长 diff 等一律传绝对路径。

### 幂等查证（超时或返回 `status: "unknown"` 时）
- **绝对不要生成新的消息 UUID 重发**。
- 先查原消息回执：
  ```sh
  agent-chat-bridge receipt <原消息UUID>
  ```
- 若确认未送达，必须携带**同一个 `--id <原消息UUID>`** 重试：
  ```sh
  agent-chat-bridge send \
    --id <原消息UUID> \
    --to codex \
    --chat <目标Codex会话UUID> \
    --from-side antigravity \
    --from-chat <当前AG会话UUID> \
    --file <原派单消息文件路径>
  ```
- 牢记：`queued`、`confirmed` 仅代表投递成功，**绝不代表 Codex 已完成开发/评审/部署**。

### 读取 Codex 最新输出
```sh
agent-chat-bridge read --side codex --chat <目标Codex会话UUID> --last 6
```

### 派单后立即挂载后台自动唤醒监听（必做）
每次 `send --to codex` 拿到回执 `<消息UUID>` 后，**立刻以 `run_command` 后台模式（`WaitMsBeforeAsync: 500`）启动监听**，随后结束本回合等待宿主自动唤醒（等待期间不轮询、零 token 消耗）：

```sh
python3 ~/.gemini/config/skills/delivery-orchestrator/scripts/wait_codex_turn.py \
  --codex-chat <目标Codex会话UUID> \
  --message-id <本次回执UUID> \
  --handoff <交接记录绝对路径> \
  --timeout 7200
```

- 该脚本只读监听 Codex 本地会话日志：先确认 `[agent-bridge:<本次回执UUID>]` 已被 Codex 接收执行，再等待 `task_complete` / `turn_aborted` / `task_aborted` 出现，自动打印 Codex 最终回复 JSON 并退出，从而触发 Antigravity 自动唤醒续推。
- **不会受单窗口限制**：即使本机同时打开多个 Antigravity 窗口（导致 `send --to antigravity` 不可用），后台监听依然 100% 稳定自动唤醒。
- 若返回 `status: "timeout"`，**严禁重新 `send`**，直接用同一个 `--message-id` 再次挂起 `wait_codex_turn.py` 即可。

### 双向通信策略
- **默认模式（后台监听自动唤醒 + 共享记录，推荐）**：Antigravity 派单并挂 `wait_codex_turn.py` 后台监听 → Codex 把详细问题与交付结果写入共享交接记录并在 Codex 会话简报 → `wait_codex_turn.py` 捕获 `task_complete` 自动唤醒 Antigravity → Antigravity 直接读记录继续下一阶段。
- **可选主动回传**：仅当不需要后台监听且项目已有受支持、获准使用的回传通道（单 Antigravity 后端下 `doctor` 通过）时，才让 Codex 主动 `send --to antigravity`（注意不要与后台监听同时触发造成重复唤醒）。

---

## 3. 会话恢复防重检查清单（“继续交付 `<任务编号>`”）

每次恢复会话时，必须按以下 4 步先核对再行动：

1. **读唯一交接记录**：确认当前阶段、绑定会话 UUID、最近一条派单 `<MSG_UUID>`、待答问题和四项上线状态。
2. **查最近派单回执（如状态未闭环）**：跑 `agent-chat-bridge receipt <MSG_UUID>`，确认上次消息是否已进入目标 Codex 会话，防止重复派单。
3. **读 Codex 会话最新进展**：跑 `agent-chat-bridge read --side codex --chat <目标Codex会话UUID> --last 6`，结合共享交接记录核对 Codex 实际进展。
4. **核对真实工程状态（进入阶段 5/6/7 时必查）**：
   - 阶段 5/6：核对分支与 PR 是否已存在，**禁止重复开 PR**。
   - 阶段 7：核对 PR 是否已合并、CI/部署是否已在运行或已完成，**禁止重复合并或重复触发部署**；同时再次核对交接记录中「客户验收通过」是否已有明确记录。
