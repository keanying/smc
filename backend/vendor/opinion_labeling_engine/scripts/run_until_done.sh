#!/usr/bin/env bash
# =============================================================================
# 持续标注，直到全部标完为止（Linux / macOS / Git Bash）
# -----------------------------------------------------------------------------
# 用法（在项目根目录执行）：
#     ./scripts/run_until_done.sh
#     ./scripts/run_until_done.sh --pending-flags 0 5
#     ./scripts/run_until_done.sh --scenic-id PFTSCA01009835
#
# 所有参数原样透传给 label_from_table.py。
#
# 它靠退出码判断要不要继续：
#     0 = 符合条件的数据全部标完了  → 结束
#     2 = 还有剩余（卡住/被中断）    → 等一会儿重跑，接着标
#     其它 = 配置或连接错误          → 停下来让人看，不要闷头重试
#
# 想挂后台跑一整晚：
#     nohup ./scripts/run_until_done.sh --pending-flags 0 5 > label.log 2>&1 &
#     tail -f label.log
# =============================================================================
set -uo pipefail

PYTHON="${PYTHON:-python3}"
REST_SECONDS="${REST_SECONDS:-60}"     # 两轮之间歇多久
MAX_ROUNDS="${MAX_ROUNDS:-200}"        # 防止配置有问题时无限循环，0 = 不限

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/label_from_table.py"

echo "命令：$PYTHON $SCRIPT $*"
echo "退出码 0=全部标完，2=还有剩余会自动重跑，其它=出错停下"

round=0
started=$(date +%s)

# Ctrl-C 时不要被循环吞掉
trap 'echo; echo "收到中断，停止重跑"; exit 130' INT TERM

while true; do
    round=$((round + 1))
    if [ "$MAX_ROUNDS" -gt 0 ] && [ "$round" -gt "$MAX_ROUNDS" ]; then
        echo "已经跑了 $MAX_ROUNDS 轮还没标完，先停下来看看是不是哪里不对" >&2
        exit 2
    fi

    echo
    echo "===== 第 $round 轮  $(date '+%Y-%m-%d %H:%M:%S') ====="
    "$PYTHON" "$SCRIPT" "$@"
    code=$?

    if [ "$code" -eq 0 ]; then
        elapsed=$(( $(date +%s) - started ))
        echo
        echo "全部标完了。共 $round 轮，耗时 $((elapsed / 3600)) 小时 $(((elapsed % 3600) / 60)) 分"
        exit 0
    fi

    if [ "$code" -ne 2 ]; then
        # 配置错、连不上库、连不上 Redis —— 重试解决不了，闷头重跑只会刷屏
        echo "退出码 $code，不是『还有剩余』，停止重跑。看上面的报错" >&2
        exit "$code"
    fi

    echo "还有剩余，${REST_SECONDS} 秒后接着跑……"
    sleep "$REST_SECONDS"
done
