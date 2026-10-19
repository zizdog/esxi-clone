# esxi-clone

ESXi 单机（无 vCenter）**链接克隆**工具。

把一台装好系统的虚拟机当母盘，几秒钟克隆出 N 台，每台只占几十 MB —— 共用母盘、增量写入，不是整盘复制。

```
母盘 11.6 GB  ──┬── win11-01   84 MB
                ├── win11-02   84 MB
                └── ... 每台 2~4 秒建好
```

## 安装

```bash
curl -fsSL https://mirror.zizdog.com:8888/others/esxi-clone-1.0.2.tar.gz | tar xz -C /tmp && bash /tmp/esxi-clone/install.sh
```

装完直接运行 `esxi-clone`。安装器会创建 `~/.esxi-clone`、准备独立的 Python 环境、安装依赖，并生成命令。

其他安装方式：

```bash
# 从 GitHub（无需 git）
curl -fsSL https://codeload.github.com/zizdog/esxi-clone/tar.gz/refs/heads/main | tar xz -C /tmp && bash /tmp/esxi-clone-main/install.sh

# 从 GitHub（需要 git）
git clone https://github.com/zizdog/esxi-clone.git && cd esxi-clone && bash install.sh

# 指定安装目录
ESXI_CLONE_DIR=/opt/esxi-clone bash install.sh

# 国内 pip 源
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash install.sh
```

## 常用命令

| 命令 | 作用 |
|---|---|
| `esxi-clone` | 交互式：列虚拟机 → 选母盘 → 填数量 → 自动克隆 |
| `esxi-clone --list` | 列出所有虚拟机 |
| `esxi-clone --base win11 --count 20 --yes` | 免交互批量克隆 20 台 |
| `esxi-clone --base win11 --count 5 --prefix lab- --start 10` | 自定义前缀和起始编号 |
| `esxi-clone --check --prefix win11-` | 检查克隆是否共用同一父盘、链是否完整 |
| `esxi-clone --delete win11-05` | 安全删除（注销 + 只删自身目录） |
| `esxi-clone --host 192.168.1.2 --user root --save` | 保存连接信息，下次免输入 |
| `esxi-clone --power-on` | 创建后立即开机 |
| `esxi-clone --help` | 查看全部参数 |
| `esxi-clone --version` | 查看版本 |

## 首次运行

```text
$ esxi-clone
ESXi 地址（IP 或域名）：192.168.1.2
用户名 [root]：
root@192.168.1.2 的密码：
保存到本地配置（不含密码）？ (Y/n) y

  #  虚拟机            电源        磁盘          快照  可作母盘
  1  win10          poweredOff  20.0 GB ×1      0
  2  win11          poweredOff  20.0 GB ×1      1    ✔
请输入母盘编号（#）：2
要克隆几台 [20]：20
命名前缀 [win11-]：
将创建 20 台：win11-21 … win11-40
确认开始？ (Y/n)
```

配置保存在 `~/.config/esxi-clone/config.json`（权限 600，默认不存密码）。

## 免交互 / CI

```bash
export ESXI_HOST=192.168.1.2
export ESXI_USER=root
export ESXI_PASSWORD='你的密码'
esxi-clone --base win11 --count 20 --prefix ci- --yes
```

| 环境变量 | 说明 |
|---|---|
| `ESXI_HOST` | ESXi 地址 |
| `ESXI_USER` | 用户名，默认 `root` |
| `ESXI_PASSWORD` | 密码（不落盘） |
| `ESXI_SSH_PORT` | SSH 端口，默认 22 |
| `ESXI_CLONE_DIR` | 安装目录（仅安装器使用） |
| `ESXI_CLONE_BASE` | 下载源（仅安装器使用） |

密码**只从交互输入或 `ESXI_PASSWORD` 环境变量读取，不会写入磁盘**。`--save` 只保存地址和用户名。

## 创建时自动做了什么

1. 校验母盘：必须关机、单磁盘、本身不是链接克隆；
2. 没有快照就自动打一个 `base-for-clone`（不含内存）；
3. 复制母盘的 seSparse 增量盘，父路径改成相对路径；
4. 改写 vmx：换名字、换磁盘名、删 UUID/MAC（避免冲突）；
5. 注册进清单，必要时立即开机。

目标 ESXi 的 SSH 未开启时，工具会询问并尝试通过 API 启动 `TSM-SSH`。

## SSH 是必需的

克隆过程要在 ESXi 本地完成文件操作（复制增量盘、读描述符、删除目录），
这些能力 vSphere API 并不提供，所以工具通过 SSH 执行 `cp` / `cat` / `du` / `rm` / `vmkfstools`。

生产环境建议按需开关：

```bash
# 图形界面：主机 → 管理 → 服务 → TSM-SSH → 启动 / 停止
# 命令行（在 ESXi 上）：
/etc/init.d/SSH start
/etc/init.d/SSH stop
```

用完记得关掉。工具检测到 SSH 关闭时会询问是否通过 API 临时启动。

## 注意事项

- **删除克隆必须用 `esxi-clone --delete`**。在 vSphere 界面点“删除”或用 `Destroy_Task`，ESXi 会把整条磁盘链删掉，包括所有克隆共用的母盘，全部克隆一起报废。
- 母盘是单点：不要删母盘、不要删母盘快照、不要对母盘做快照合并。
- 克隆机的计算机名 / SID 与母盘相同，生产使用前请 sysprep 或逐台改名。
- 增量盘只增不减，长期运行的机器建议转成独立盘（`vmkfstools -i` 导出）。

## 卸载

```bash
curl -fsSL https://mirror.zizdog.com:8888/others/esxi-clone-1.0.2.tar.gz | tar xz -C /tmp && bash /tmp/esxi-clone/install.sh --uninstall
```

或手动：

```bash
rm -rf ~/.esxi-clone ~/.config/esxi-clone
rm -f /usr/local/bin/esxi-clone ~/.local/bin/esxi-clone
```

## 环境要求

- Python 3.8+
- 依赖：`pyvmomi`、`paramiko`（安装器自动装）
- ESXi 6.7 / 7.0 / 8.0（已在 8.0.3 实测）
- 操作机：macOS / Linux / Windows(WSL)

## License

[MIT](LICENSE)
