#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ENV_FILE="$APP_DIR/deploy/.env"
PYTHON="$APP_DIR/.venv/bin/python"
BASE_PYTHON="${TA_PYTHON_BIN:-python3}"
WORKER_SERVICE=openai-a-worker.service
WEB_SERVICE=openai-a-web.service

die() { printf '错误：%s\n' "$*" >&2; exit 1; }

load_config() {
    [[ -f "$ENV_FILE" ]] || die "先运行 bash deploy/manage.sh setup"
    set -a
    . "$ENV_FILE"
    set +a
    : "${TA_MARKET_DIR:?Missing TA_MARKET_DIR}"
    : "${TA_STATE_DIR:?Missing TA_STATE_DIR}"
    : "${TA_BACKUP_DIR:?Missing TA_BACKUP_DIR}"
    TA_MARKET_DB="$TA_MARKET_DIR/market_full.duckdb"
    TA_STATE_DB="$TA_STATE_DIR/state.sqlite"
    TA_PORT="${TA_WEB_PORT:-80}"
    TA_HOST=0.0.0.0
    TA_OBSERVE_ONLY="${TA_OBSERVE_ONLY:-0}"
    export TA_MARKET_DB TA_STATE_DB TA_PORT TA_HOST TA_OBSERVE_ONLY
}

check_python() {
    command -v "$BASE_PYTHON" >/dev/null || die "未找到 $BASE_PYTHON；请安装 Python 3.11 至 3.14"
    "$BASE_PYTHON" -c 'import sys; assert (3, 11) <= sys.version_info[:2] < (3, 15)' || \
        die "需要 Python 3.11 至 3.14；可用 TA_PYTHON_BIN 指定已安装的解释器"
}

check_venv() {
    [[ -x "$PYTHON" ]] || die "缺少 Python 虚拟环境；先运行 bash deploy/manage.sh setup"
}

check_data() {
    [[ -s "$TA_MARKET_DB" && -s "$TA_STATE_DB" ]] || \
        die "数据库未创建；先运行 bash deploy/manage.sh initialize"
}

check_systemd() {
    command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] || \
        die "这台 Linux 服务器未运行 systemd；当前管理脚本需要 systemd 管理常驻服务"
    command -v sudo >/dev/null || [[ "$(id -u)" -eq 0 ]] || \
        die "安装系统服务需要 sudo 权限"
}

as_root() {
    if [[ "$(id -u)" -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

setup() {
    check_python
    if [[ ! -f "$ENV_FILE" ]]; then
        local data_root
        data_root="$(dirname "$APP_DIR")/openai-a-data"
        [[ "$data_root" != *"'"* ]] || die "目录路径不能包含单引号"
        cat > "$ENV_FILE" <<EOF
TA_MARKET_DIR='$data_root/market'
TA_STATE_DIR='$data_root/state'
TA_BACKUP_DIR='$data_root/backups'
TA_WEB_PORT=80
TA_OBSERVE_ONLY=0
EOF
        chmod 600 "$ENV_FILE"
    fi
    load_config
    mkdir -p "$TA_MARKET_DIR" "$TA_STATE_DIR" "$TA_BACKUP_DIR"
    if [[ ! -x "$PYTHON" ]]; then
        "$BASE_PYTHON" -m venv "$APP_DIR/.venv" || \
            die "创建虚拟环境失败；请为所用 Python 安装 venv/ensurepip 组件后重试"
    fi
    "$PYTHON" -m pip install -r "$APP_DIR/requirements.txt"
    "$PYTHON" -c 'import duckdb,pandas,akshare,baostock,fastapi,uvicorn' || \
        die "Python 依赖导入失败；请确认系统已安装 libgomp.so.1 等运行库"
    printf 'Python 依赖已安装。下一步：bash deploy/manage.sh initialize\n'
}

initialize() {
    load_config
    check_venv
    if command -v systemctl >/dev/null && systemctl is-active --quiet "$WORKER_SERVICE"; then
        die "先停止 worker，再执行历史数据初始化"
    fi
    "$PYTHON" "$APP_DIR/run.py" init
    printf '开始回填至少三年的全市场日线；这一步可能需要数小时。\n'
    "$PYTHON" "$APP_DIR/run.py" bootstrap
    printf '数据初始化完成。下一步：bash deploy/manage.sh start\n'
}

install_services() {
    check_systemd
    [[ "$APP_DIR" != *[[:space:]%]* && "$TA_MARKET_DB" != *[[:space:]%]* && \
       "$TA_STATE_DB" != *[[:space:]%]* ]] || \
        die "部署路径不能包含空格、换行或百分号"
    local service_user service_group worker_unit web_unit
    service_user="$(id -un)"
    service_group="$(id -gn)"
    worker_unit="$(mktemp)"
    web_unit="$(mktemp)"
    cat > "$worker_unit" <<EOF
[Unit]
Description=OPENAI-A trading worker
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$service_user
Group=$service_group
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
Environment=TA_MARKET_DB=$TA_MARKET_DB
Environment=TA_STATE_DB=$TA_STATE_DB
Environment=TA_OBSERVE_ONLY=$TA_OBSERVE_ONLY
ExecStart=$PYTHON $APP_DIR/run.py worker
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
EOF
    cat > "$web_unit" <<EOF
[Unit]
Description=OPENAI-A web and API
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$service_user
Group=$service_group
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
Environment=TA_STATE_DB=$TA_STATE_DB
Environment=TA_HOST=$TA_HOST
Environment=TA_PORT=$TA_PORT
ExecStart=$PYTHON $APP_DIR/run.py web
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=5
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
EOF
    as_root install -m 644 "$worker_unit" "/etc/systemd/system/$WORKER_SERVICE"
    as_root install -m 644 "$web_unit" "/etc/systemd/system/$WEB_SERVICE"
    rm -f -- "$worker_unit" "$web_unit"
    as_root systemctl daemon-reload
    as_root systemctl enable "$WORKER_SERVICE" "$WEB_SERVICE"
}

wait_healthy() {
    local attempt
    for attempt in {1..30}; do
        if "$PYTHON" -c \
            'import json,os,urllib.request; u="http://127.0.0.1:"+os.environ["TA_PORT"]+"/api/health"; assert json.load(urllib.request.urlopen(u,timeout=3))["worker_healthy"]' \
            >/dev/null 2>&1; then
            printf '网页与后台均已运行。访问：http://服务器公网IP:%s/\n' "$TA_PORT"
            return 0
        fi
        sleep 2
    done
    as_root systemctl --no-pager status "$WORKER_SERVICE" "$WEB_SERVICE" || true
    die "服务未在 60 秒内就绪；请运行 bash deploy/manage.sh status"
}

start() {
    load_config
    check_venv
    check_data
    "$PYTHON" "$APP_DIR/run.py" ready
    install_services
    as_root systemctl restart "$WORKER_SERVICE" "$WEB_SERVICE"
    wait_healthy
}

backup_data() {
    load_config
    check_venv
    check_data
    check_systemd
    local running=() service snapshot_name
    for service in "$WORKER_SERVICE" "$WEB_SERVICE"; do
        if systemctl is-active --quiet "$service"; then running+=("$service"); fi
    done
    if ((${#running[@]})); then as_root systemctl stop "${running[@]}"; fi
    snapshot_name="snapshot-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    if ! "$PYTHON" "$APP_DIR/run.py" snapshot --output "$TA_BACKUP_DIR/$snapshot_name"; then
        if ((${#running[@]})); then as_root systemctl start "${running[@]}" || true; fi
        die "备份失败；未完成的快照不可用于恢复"
    fi
    if ((${#running[@]})); then as_root systemctl start "${running[@]}"; fi
    printf '备份完成：%s/%s\n' "$TA_BACKUP_DIR" "$snapshot_name"
}

update() {
    load_config
    check_venv
    command -v git >/dev/null || die "未找到 Git"
    git -C "$APP_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || \
        die "应用目录不是 Git 仓库"
    backup_data
    git -C "$APP_DIR" pull --ff-only
    "$PYTHON" -m pip install -r "$APP_DIR/requirements.txt"
    install_services
    as_root systemctl restart "$WORKER_SERVICE" "$WEB_SERVICE"
    wait_healthy
}

status() {
    load_config
    check_systemd
    as_root systemctl --no-pager status "$WORKER_SERVICE" "$WEB_SERVICE" || true
    as_root journalctl --no-pager -n 30 -u "$WORKER_SERVICE" -u "$WEB_SERVICE" || true
}

case "${1:-}" in
    setup) setup ;;
    initialize) initialize ;;
    start) start ;;
    stop) check_systemd; as_root systemctl stop "$WORKER_SERVICE" "$WEB_SERVICE" ;;
    status) status ;;
    backup) backup_data ;;
    update) update ;;
    *)
        printf '用法：bash deploy/manage.sh {setup|initialize|start|stop|status|backup|update}\n' >&2
        exit 2
        ;;
esac
