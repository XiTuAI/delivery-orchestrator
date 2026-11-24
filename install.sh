#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DIR="$HOME/.gemini/config/skills/delivery-orchestrator"

echo "==> 安装 Antigravity 全局 Skill: delivery-orchestrator"
mkdir -p "$HOME/.gemini/config/skills"

if [ "$SRC_DIR" != "$TARGET_DIR" ]; then
  rm -rf "$TARGET_DIR"
  cp -R "$SRC_DIR" "$TARGET_DIR"
  rm -rf "$TARGET_DIR/.git" "$TARGET_DIR/__pycache__" "$TARGET_DIR/scripts/__pycache__" "$TARGET_DIR/resources/agent-chat-bridge/__pycache__"
  chmod +x "$TARGET_DIR/install.sh" "$TARGET_DIR/scripts/setup_bridge.py" "$TARGET_DIR/scripts/wait_codex_turn.py" "$TARGET_DIR/resources/agent-chat-bridge/bridge.py"
  echo "    已复制到 $TARGET_DIR"
else
  echo "    Skill 已位于 $TARGET_DIR"
fi

echo "==> 初始化并检查本机 agent-chat-bridge 环境"
if python3 "$TARGET_DIR/scripts/setup_bridge.py"; then
  echo ""
  echo "安装完成！请新开一个 Antigravity 对话，直接发送："
  echo '  开始交付需求：<需求描述>。仓库绝对路径：<仓库路径>，目标 Codex 会话：<会话标题或UUID>。请按 delivery-orchestrator 规程先核对绑定、出需求与验收标准并建立交接记录。'
else
  echo ""
  echo "Skill 已安装到 $TARGET_DIR，但本机前置环境尚有未就绪项（见上方 JSON 的 issues 字段）。补齐后重新运行："
  echo "  python3 \"$TARGET_DIR/scripts/setup_bridge.py\""
  exit 1
fi
