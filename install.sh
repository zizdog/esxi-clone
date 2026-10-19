#!/usr/bin/env bash
# esxi-clone 安装器
#
#   一键安装： curl -fsSL https://mirror.zizdog.com:8888/others/esxi-clone-1.0.2.tar.gz \
#                | tar xz -C /tmp && bash /tmp/esxi-clone/install.sh
#   指定目录： ESXI_CLONE_DIR=/opt/esxi-clone curl -fsSL ... | bash
#   国内镜像： PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple curl -fsSL ... | bash
#   卸载：     curl -fsSL ... | bash -s -- --uninstall
#
set -euo pipefail

VERSION="1.0.2"
BASE_URL="${ESXI_CLONE_BASE:-https://mirror.zizdog.com:8888/apps/esxi-clone}"
GITHUB_RAW="${ESXI_CLONE_GITHUB:-https://raw.githubusercontent.com/zizdog/esxi-clone/main}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd || true)"
INSTALL_DIR="${ESXI_CLONE_DIR:-$HOME/.esxi-clone}"
MIN_PY="3.8"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

download() {  # download <url> <dest>
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --connect-timeout 15 "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$1"
    else
        die "需要 curl 或 wget"
    fi
}

# ---------------------------------------------------------------- 卸载
if [ "${1:-}" = "--uninstall" ]; then
    for link in /usr/local/bin/esxi-clone "$HOME/.local/bin/esxi-clone"; do
        [ -L "$link" ] && rm -f "$link" && ok "已移除 $link"
    done
    [ -d "$INSTALL_DIR" ] && rm -rf "$INSTALL_DIR" && ok "已删除 $INSTALL_DIR"
    say "卸载完成"
    exit 0
fi

say ""
say "ESXi 链接克隆工具 · 安装器 v${VERSION}"
say "----------------------------------------"
say "安装目录：$INSTALL_DIR"
say ""

# ---------------------------------------------------------------- Python
command -v python3 >/dev/null 2>&1 || die "未找到 python3，请先安装（apt install python3 / brew install python3）"
PY_VER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
python3 - <<'PY' || die "需要 Python ${MIN_PY}+，当前 ${PY_VER}"
import sys
sys.exit(0 if sys.version_info >= (3, 8) else 1)
PY
ok "python3 $PY_VER"

# ---------------------------------------------------------------- venv
mkdir -p "$INSTALL_DIR"
VENV="$INSTALL_DIR/venv"
if [ ! -x "$VENV/bin/python" ]; then
    if python3 -m venv "$VENV" 2>/dev/null; then
        ok "已创建虚拟环境"
    else
        rm -rf "$VENV"
        die "创建虚拟环境失败。Debian/Ubuntu 请先安装：sudo apt install -y python3-venv"
    fi
else
    ok "复用已有虚拟环境"
fi

# ---------------------------------------------------------------- 依赖
"$VENV/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true
say "  安装依赖 pyvmomi、paramiko …"
if ! "$VENV/bin/python" -m pip install -q --disable-pip-version-check pyvmomi paramiko 2>/dev/null; then
    warn "默认源安装失败，改用清华镜像重试"
    "$VENV/bin/python" -m pip install -q --disable-pip-version-check \
        -i "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
        pyvmomi paramiko || die "依赖安装失败，请检查网络或设置 PIP_INDEX_URL"
fi
ok "依赖就绪"

# ---------------------------------------------------------------- 主程序
if [ -n "${SCRIPT_DIR:-}" ] && [ -f "$SCRIPT_DIR/esxi-clone.py" ]; then
    say "  使用安装包内的 esxi-clone.py …"
    cp "$SCRIPT_DIR/esxi-clone.py" "$INSTALL_DIR/esxi-clone.py"
else
    say "  下载 esxi-clone.py …"
    if ! download "$BASE_URL/esxi-clone.py" "$INSTALL_DIR/esxi-clone.py" 2>/dev/null; then
        warn "镜像站不可用，改为从 GitHub 下载"
        download "$GITHUB_RAW/esxi-clone.py" "$INSTALL_DIR/esxi-clone.py" \
            || die "下载失败，请检查网络，或用 ESXI_CLONE_BASE 指定下载源"
    fi
fi
SIZE="$(wc -c < "$INSTALL_DIR/esxi-clone.py" | tr -d ' ')"
if [ "$SIZE" -lt 5000 ] || ! grep -q "esxi-clone" "$INSTALL_DIR/esxi-clone.py"; then
    die "下载的 esxi-clone.py 不完整（${SIZE} 字节），请检查 $BASE_URL/esxi-clone.py 是否存在"
fi
chmod 644 "$INSTALL_DIR/esxi-clone.py"
ok "主程序 ${SIZE} 字节"

# ---------------------------------------------------------------- 启动器
cat > "$INSTALL_DIR/esxi-clone" <<EOF
#!/bin/sh
exec "$VENV/bin/python" "$INSTALL_DIR/esxi-clone.py" "\$@"
EOF
chmod 755 "$INSTALL_DIR/esxi-clone"
ok "启动器：$INSTALL_DIR/esxi-clone"

# ---------------------------------------------------------------- PATH
LINKED=""
if [ -w /usr/local/bin ] 2>/dev/null; then
    ln -sf "$INSTALL_DIR/esxi-clone" /usr/local/bin/esxi-clone 2>/dev/null && LINKED="/usr/local/bin/esxi-clone"
elif [ -d "$HOME/.local/bin" ]; then
    ln -sf "$INSTALL_DIR/esxi-clone" "$HOME/.local/bin/esxi-clone" 2>/dev/null && LINKED="$HOME/.local/bin/esxi-clone"
fi

say ""
say "----------------------------------------"
say "安装完成。运行："
if [ -n "$LINKED" ]; then
    case ":$PATH:" in
        *":$(dirname "$LINKED"):"*) say "    esxi-clone" ;;
        *) say "    export PATH=\"$(dirname "$LINKED"):\$PATH\""
           say "    esxi-clone" ;;
    esac
else
    say "    $INSTALL_DIR/esxi-clone"
fi
say ""
say "首次运行会询问 ESXi 地址、账号和密码（可保存到 ~/.config/esxi-clone/config.json）。"
say "查看帮助：esxi-clone --help"
say ""
