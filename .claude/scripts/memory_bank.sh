#!/usr/bin/env bash
# Memory Bank 交接脚本 v2：SessionStart / PreCompact / SessionEnd 三个 hook 共用。
#
# 用法：
#   memory_bank.sh start    # SessionStart —— 输出 维护指令+HANDOFF+SCOPE(+压缩快照) 到 stdout，注入为会话上下文
#   memory_bank.sh archive  # PreCompact   —— 压缩前备份 HANDOFF，并把 transcript 尾部写为 latest-snapshot.md
#   memory_bank.sh end      # SessionEnd   —— 先备份，再从 transcript 提取尾部 + git 事实，重写 HANDOFF.md
#
# 原则：HANDOFF 每次会话重写（可丢弃），DECISIONS 只追加（永不改历史）。
# SYSTEM / DECISIONS 体积大且少变，不在此自动注入，由下个会话按需读取。
set -euo pipefail

MB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HANDOFF="$MB_DIR/HANDOFF.md"
SCOPE="$MB_DIR/SCOPE.md"
HANDOFFS_DIR="$MB_DIR/handoffs"
SNAPSHOT="$HANDOFFS_DIR/latest-snapshot.md"

# 从 Claude Code transcript 中提取最近一段 user/assistant 文本，作为「上次在哪」的粗摘录。
# content 可能是字符串或 content-block 数组，两种都处理；transcript 缺失或提取失败返回空串。
# 同时被 archive（PreCompact 快照）和 end（HANDOFF 重写）复用，避免两份重复逻辑。
extract_tail() {
  local transcript="$1" limit="${2:-2500}"
  [[ -n "$transcript" && -f "$transcript" ]] || { echo ""; return 0; }
  jq -r '
    select(.type == "user" or .type == "assistant")
    | select(.message != null)
    | .message.role as $r
    | (.message.content
        | if type == "array" then (map(select(.type == "text") | .text) | join("\n")) else . end)
    | "### \($r)\n\(.)\n"
  ' "$transcript" 2>/dev/null | tail -c "$limit" || true
}

case "${1:-}" in
  start)
    # SessionStart：官方文档要求 hook 输出 JSON（hookSpecificOutput.additionalContext）
    # 才能注入上下文，纯文本 stdout 会被忽略。只自动加载易变的小文件（HANDOFF + SCOPE）。
    #
    # stdin 含 source 字段（startup | resume | clear | compact）。SessionStart 会在 compact
    # 之后重新触发——此时若 PreCompact 刚写的快照比 HANDOFF 新（Claude 未及主动维护），
    # 把快照一并注入，让压缩后的上下文携带「压缩瞬间状态」而非「会话开始状态」。
    input="$(cat)" || input="{}"
    source="$(printf '%s' "$input" | jq -r '.source // empty' 2>/dev/null || true)"

    directive="# 维护指令（本会话必须遵守）
1. 每完成一个里程碑/任务，更新 .claude/HANDOFF.md 的「下一步做什么」段（简洁、带优先级）
2. 感知到上下文接近满、将被压缩或要切窗口时，先把当前状态写入「上次在哪」段
3. 字段约定：目标 / 当前层与任务 / 改动文件 / 待决问题 / 下一步 / 风险
4. 不要删除或改写「自动生成」行与「当前状态快照」段（由 hook 管理）"

    handoff_block="## HANDOFF.md — 上次在哪、下一步做什么

$(cat "$HANDOFF" 2>/dev/null || echo '（尚无 HANDOFF.md，本会话为首次运行）')"

    # 快照只在「比 HANDOFF 新」时注入（prefer 更新者）：Claude 主动维护的语义内容优先于原始尾部。
    # stat -f %m 是 macOS BSD 语法；GNU/Linux 需换成 stat -c %Y。
    snapshot_block=""
    if [[ "$source" == "compact" && -f "$SNAPSHOT" && -f "$HANDOFF" ]]; then
      if [[ "$(stat -f %m "$SNAPSHOT" 2>/dev/null)" -gt "$(stat -f %m "$HANDOFF" 2>/dev/null)" ]]; then
        snapshot_block="## 压缩前快照（PreCompact 自动生成）

$(cat "$SNAPSHOT" 2>/dev/null || true)"
      fi
    fi

    scope_block="## SCOPE.md — 项目做什么、不做什么

$(cat "$SCOPE" 2>/dev/null || echo '（尚无 SCOPE.md）')"

    hint="> 按需读取：架构/约束/环境 → .claude/SYSTEM.md；「为什么这么设计」→ .claude/DECISIONS.md"

    context="$directive

$handoff_block

$snapshot_block

$scope_block

$hint"
    jq -n --arg context "$context" \
      '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $context}}'
    ;;

  archive)
    # PreCompact：压缩会丢掉会话内对交接的维护。先把当前 HANDOFF 存档，
    # 再把 transcript 尾部写为快照——压缩后 SessionStart(source=compact) 会把
    # 比 HANDOFF 更新的快照注入新上下文。HANDOFF 本体不动：它是 Claude 语义维护的，
    # 原始尾部只作为兜底进压缩后上下文，不污染交接文件。
    input="$(cat)" || input="{}"
    transcript="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null || true)"

    mkdir -p "$HANDOFFS_DIR"
    if [[ -f "$HANDOFF" ]]; then
      cp "$HANDOFF" "$HANDOFFS_DIR/HANDOFF-$(date +%Y%m%d-%H%M%S).md"
    fi

    tail_text="$(extract_tail "$transcript")"
    if [[ -n "${tail_text//[[:space:]]/}" ]]; then
      # 整体覆盖，只留最新一份快照，防止无限膨胀。
      {
        echo "压缩前快照（PreCompact $(date '+%Y-%m-%d %H:%M:%S')）"
        echo ""
        echo "$tail_text"
      } > "$SNAPSHOT"
    fi
    ;;

  end)
    # SessionEnd：先备份（短会话可能从未触发 PreCompact，旧 HANDOFF 也要留档），
    # 再从 transcript 提取尾部 + git 事实，重写 HANDOFF。stdin 形如
    #   {"session_id":"...","transcript_path":"...","reason":"..."}
    input="$(cat)" || input="{}"
    session_id="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)"
    transcript="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
    reason="$(printf '%s' "$input" | jq -r '.reason // empty' 2>/dev/null || true)"

    mkdir -p "$HANDOFFS_DIR"
    if [[ -f "$HANDOFF" ]]; then
      cp "$HANDOFF" "$HANDOFFS_DIR/HANDOFF-$(date +%Y%m%d-%H%M%S).md"
    fi

    tail_text="$(extract_tail "$transcript" 3000)"
    if [[ -z "${tail_text//[[:space:]]/}" ]]; then
      # 空会话/无 transcript——不覆盖现有 HANDOFF，避免用垃圾覆盖好内容；顺手清掉 stale 快照。
      rm -f "$SNAPSHOT" 2>/dev/null || true
      exit 0
    fi

    # 确定性 git 事实：新窗口无需重新探索就能知道分支/改动/测试进度。best-effort，失败不阻塞 hook。
    # 注意 set -o pipefail 下，git status 若失败会导致整个管道非零退出，必须用 || echo 兜底。
    git_branch="$(git branch --show-current 2>/dev/null || echo 'N/A')"
    git_head="$(git rev-parse --short HEAD 2>/dev/null || echo 'N/A')"
    git_uncommitted="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ' || echo 0)"
    git_log="$(git log --oneline -5 2>/dev/null || echo 'N/A')"

    {
      echo "# HANDOFF.md"
      echo ""
      # 注意：macOS 系统 bash 3.2 会把 $var 后紧跟的多字节字符吞进变量名，
      # 因此 $session_id 必须用 ${} 包裹，否则报 "unbound variable"。
      echo "自动生成：$(date '+%Y-%m-%d %H:%M:%S')；session=${session_id}；reason=${reason:-unknown}"
      echo ""
      echo "## 上次在哪（会话尾部摘录）"
      echo ""
      echo "${tail_text}"
      echo ""
      echo "## 当前状态快照（git 事实，自动附加，勿改）"
      echo ""
      echo "- 分支：${git_branch}"
      echo "- HEAD：${git_head}"
      echo "- 未提交改动：${git_uncommitted} 个文件"
      echo "- 最近 5 条提交："
      echo "${git_log}"
      echo ""
      echo "## 下一步做什么"
      echo ""
      echo "（Claude 主动维护：目标 / 当前层与任务 / 改动文件 / 待决问题 / 下一步 / 风险）"
    } > "$HANDOFF"

    # 快照已并入新 HANDOFF，失效；下次 start 的 mtime 比对也不该命中旧快照。
    rm -f "$SNAPSHOT" 2>/dev/null || true
    ;;

  *)
    echo "memory_bank.sh: 未知子命令 '$*'（可选 start / archive / end）" >&2
    exit 1
    ;;
esac
