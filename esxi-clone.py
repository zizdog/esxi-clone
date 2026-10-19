#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""esxi-clone —— ESXi 单机链接克隆工具（免 vCenter）

    esxi-clone                 交互式：列出虚拟机 → 选母盘 → 填数量 → 自动克隆
    esxi-clone --list          只列虚拟机
    esxi-clone --check         检查克隆的父盘链是否完整
    esxi-clone --base win11 --count 20 --yes
    esxi-clone --delete win11-05
    esxi-clone --host 192.168.1.2 --user root --save

密码只从交互输入或环境变量 ESXI_PASSWORD 读取，永远不会写入磁盘。

依赖：pyvmomi、paramiko（install.sh 会自动装好）
原理：复制母盘快照的 seSparse 增量盘，让每台克隆机共享只读母盘，写入只落自己的增量盘。
安全：删除克隆必须走本工具的 --delete（Unregister + 只删自身目录），
      绝不能用 vSphere 界面的“删除”，否则会把共享母盘一起删掉。
"""

__version__ = "1.0.2"

import argparse
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
import time
from getpass import getpass

try:
    import paramiko
    from pyVmomi import vim
    from pyVim.connect import SmartConnect, Disconnect
except ImportError as exc:                       # pragma: no cover
    sys.exit(f"缺少依赖 {exc.name}，请先运行 install.sh，或在 venv 中 pip install pyvmomi paramiko")

CONFIG_PATH = os.path.expanduser("~/.config/esxi-clone/config.json")
SNAP_NAME = "base-for-clone"

# vmx 中必须删掉/重写的键：UUID、MAC 等，否则克隆机会和母盘冲突
DROP_KEYS = {
    "uuid.bios", "uuid.location", "vc.uuid", "vmci0.id",
    "ethernet0.generatedAddress", "ethernet0.address", "ethernet0.addressType",
    "sched.swap.derivedName", "migrate.hostlog",
    "vm.genid", "vm.genidX", "guestInfo.detailed.data",
    "vmxstats.filename", "extendedConfigFile", "nvram", "displayName",
    "vmx.log.fileName",
}

AUTO_YES = False


# ----------------------------------------------------------------- 输出
def hr(title=""):
    print("\n" + "=" * 70)
    if title:
        print(title)
        print("=" * 70)


def info(msg):
    print(msg)


def warn(msg):
    print(f"[警告] {msg}")


def die(msg, code=1):
    print(f"[错误] {msg}")
    sys.exit(code)


def ask(prompt, default=""):
    if AUTO_YES:
        return str(default)
    tip = f"{prompt} [{default}]：" if default != "" else f"{prompt}："
    try:
        val = input(tip).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消")
        sys.exit(1)
    return val or str(default)


def ask_yes(prompt, default=True):
    if AUTO_YES:
        return default
    return ask(prompt + (" (Y/n)" if default else " (y/N)"), "Y" if default else "N").lower().startswith("y")


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


# ----------------------------------------------------------------- 路径
def split_ds(path):
    """'[datastore1] win11/win11.vmx' -> ('datastore1', 'win11/win11.vmx')"""
    m = re.match(r"\[(.+?)\]\s+(.+)", path or "")
    if not m:
        raise RuntimeError(f"无法解析数据存储路径: {path}")
    return m.group(1), m.group(2)


def ds_root(ds):
    return f"/vmfs/volumes/{ds}"


# ----------------------------------------------------------------- 凭据
def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            cfg = json.load(fh)
    except Exception:
        return {}
    if "password" in cfg:                     # 兼容旧版本：清掉曾经保存的密码
        cfg.pop("password", None)
        try:
            save_config(cfg, quiet=True)
            print("[提示] 检测到旧版本保存过的密码，已从配置文件中删除")
        except Exception:
            pass
    return cfg


def save_config(cfg, quiet=False):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    old_mask = os.umask(0o077)
    try:
        with open(CONFIG_PATH, "w") as fh:
            json.dump(cfg, fh, indent=2)
    finally:
        os.umask(old_mask)
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)
    if not quiet:
        info(f"已保存连接信息到 {CONFIG_PATH}（不含密码）")


def resolve_credentials(args):
    """密码只存在于内存（或调用者自己设的环境变量），绝不写入磁盘。"""
    cfg = load_config()
    host = args.host or os.environ.get("ESXI_HOST") or cfg.get("host")
    user = args.user or os.environ.get("ESXI_USER") or cfg.get("user") or "root"
    ssh_port = args.ssh_port or int(os.environ.get("ESXI_SSH_PORT") or cfg.get("ssh_port") or 22)

    if not host:
        if AUTO_YES:
            die("非交互模式请用 --host 或环境变量 ESXI_HOST 指定地址")
        host = ask("ESXi 地址（IP 或域名）")
    if not user:
        user = ask("用户名", "root")

    password = os.environ.get("ESXI_PASSWORD")
    if password:
        info("使用环境变量 ESXI_PASSWORD 中的密码")
    else:
        try:
            password = getpass(f"{user}@{host} 的密码：")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            sys.exit(1)
        if not password:
            die("密码不能为空")

    if args.save:
        cfg.pop("password", None)
        cfg.update({"host": host, "user": user, "ssh_port": ssh_port})
        save_config(cfg)
    return host, user, password, ssh_port


# ----------------------------------------------------------------- 客户端
class Esxi:
    def __init__(self, host, user, password, ssh_port=22):
        self.host = host
        self.user = user
        self.password = password
        self.ssh_port = ssh_port
        self.si = None
        self._ssh = None
        self._sftp = None

    # ---- vSphere API
    def api(self):
        if self.si is None:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                self.si = SmartConnect(host=self.host, user=self.user,
                                       pwd=self.password, port=443, sslContext=ctx)
            except Exception as exc:
                die(f"连接 vSphere API 失败：{exc}")
        return self.si

    def find(self, vim_type):
        content = self.api().content
        seen, out = set(), []
        stack = [content.rootFolder]
        while stack:
            obj = stack.pop()
            if obj is None or obj._moId in seen:
                continue
            seen.add(obj._moId)
            if isinstance(obj, vim_type):
                out.append(obj)
            if isinstance(obj, vim.Datacenter):
                stack += [f for f in (obj.hostFolder, obj.vmFolder,
                                      obj.networkFolder, obj.datastoreFolder) if f]
            for attr in ("childEntity", "vm", "host"):
                val = getattr(obj, attr, None)
                if isinstance(val, list):
                    stack += [x for x in val if isinstance(x, vim.ManagedEntity)]
        return out

    def wait(self, task, label="task", timeout=3600):
        start = time.time()
        while True:
            st = task.info.state
            if st == vim.TaskInfo.State.success:
                return task.info.result
            if st == vim.TaskInfo.State.error:
                err = task.info.error
                raise RuntimeError(f"{label} 失败：{getattr(err, 'msg', err)}")
            if time.time() - start > timeout:
                raise TimeoutError(f"{label} 超时")
            time.sleep(1.5)

    # ---- SSH（paramiko，无需 sshpass）
    def ssh(self, cmd, timeout=None):
        if self._ssh is None:
            self._connect_ssh()
        stdin, stdout, stderr = self._ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            raise RuntimeError(f"远程命令失败({rc}): {cmd}\n{out}{err}")
        return out

    def _connect_ssh(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(self.host, port=self.ssh_port, username=self.user,
                           password=self.password, timeout=10,
                           banner_timeout=15, auth_timeout=15, look_for_keys=False,
                           allow_agent=False)
        except Exception as exc:
            if not self._try_enable_ssh():
                die(f"SSH 连接失败：{exc}\n"
                    "  请在 ESXi 上开启 SSH：主机 → 管理 → 服务 → TSM-SSH → 启动")
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(self.host, port=self.ssh_port, username=self.user,
                           password=self.password, timeout=10, banner_timeout=15,
                           auth_timeout=15, look_for_keys=False, allow_agent=False)
        self._ssh = client

    def _try_enable_ssh(self):
        """SSH 没开时，尝试通过 API 启动 TSM-SSH。"""
        try:
            host = self.find(vim.HostSystem)[0]
            svc = host.configManager.serviceSystem
            state = {s.key: s.running for s in svc.serviceInfo.service}.get("TSM-SSH")
            if state:
                return False
            print("检测到 SSH 未开启，尝试通过 API 启动 TSM-SSH…")
            if not AUTO_YES and not ask_yes("可以开启 ESXi 的 SSH 服务吗？", True):
                return False
            svc.Start(id="TSM-SSH")
            time.sleep(2)
            return True
        except Exception:
            return False

    def sftp_put(self, text, remote_path):
        if self._ssh is None:
            self._connect_ssh()
        if self._sftp is None:
            self._sftp = self._ssh.open_sftp()
        with self._sftp.open(remote_path, "w") as fh:
            fh.write(text)
        self._sftp.chmod(remote_path, 0o644)

    def close(self):
        try:
            if self._sftp:
                self._sftp.close()
            if self._ssh:
                self._ssh.close()
        finally:
            if self.si is not None:
                try:
                    Disconnect(self.si)
                except Exception:
                    pass


# ----------------------------------------------------------------- 清单
def list_vms(esxi):
    rows = []
    for vm in sorted(esxi.find(vim.VirtualMachine), key=lambda v: v.name):
        conf = vm.config
        if conf is None:
            rows.append({"name": vm.name, "vm": vm, "ok": False, "power": "invalid",
                         "disk": "-", "snap": 0, "disks": 0})
            continue
        disks = [d for d in conf.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]
        snaps = 0
        if vm.snapshot:
            def count(lst):
                return sum(1 + count(s.childSnapshotList) for s in lst)
            snaps = count(vm.snapshot.rootSnapshotList)
        rows.append({
            "name": vm.name, "vm": vm, "ok": True,
            "power": str(vm.runtime.powerState).replace("VirtualMachinePowerState.", ""),
            "disk": f"{fmt_size(sum(d.capacityInKB for d in disks) * 1024)} ×{len(disks)}",
            "disks": len(disks), "snap": snaps,
        })
    return rows


def print_vms(rows):
    print(f"{'#':>3}  {'虚拟机':24s} {'电源':11s} {'磁盘':15s} {'快照':5s} 可作母盘")
    print("-" * 78)
    for i, r in enumerate(rows, 1):
        if not r["ok"]:
            note = "配置异常"
        elif r["disks"] != 1:
            note = "多磁盘，暂不支持"
        elif r["power"] == "poweredOn":
            note = "需先关机"
        else:
            note = "✔"
        print(f"{i:>3}  {r['name']:24s} {r['power']:11s} {r['disk']:15s} {r['snap']:^5} {note}")


def pick_base(rows):
    while True:
        raw = ask("请输入母盘编号（#）")
        if not raw.isdigit():
            print("请输入数字")
            continue
        idx = int(raw)
        if not 1 <= idx <= len(rows):
            print(f"编号范围 1~{len(rows)}")
            continue
        r = rows[idx - 1]
        if not r["ok"]:
            print("该虚拟机配置异常")
            continue
        if r["disks"] != 1:
            print("该虚拟机有多块磁盘，暂不支持")
            continue
        return r


# ----------------------------------------------------------------- 母盘准备
def assert_not_linked_clone(esxi, vm):
    disks = [d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]
    if len(disks) != 1:
        return
    ds, disk_rel = split_ds(disks[0].backing.fileName)
    folder = posixpath.dirname(disk_rel)
    desc = esxi.ssh(f"cat {ds_root(ds)}/{disk_rel} 2>/dev/null || true")
    parent_cid = parent_hint = ""
    for line in desc.splitlines():
        t = line.strip()
        if t.startswith("parentCID="):
            parent_cid = t.split("=", 1)[1].strip()
        elif t.startswith("parentFileNameHint="):
            parent_hint = t.split("=", 1)[1].strip().strip('"')
    if not parent_cid or parent_cid.lower() == "ffffffff":
        return
    parent_rel = posixpath.normpath(posixpath.join(folder, parent_hint)) if parent_hint else ""
    if posixpath.dirname(parent_rel) == folder:
        return
    warn(f"{vm.name} 的父盘是 {parent_rel}，不在它自己的目录里。")
    info("       说明它本身就是链接克隆，继续克隆会叠出多层增量链。")
    info("       建议：换一台真正的母盘，或先对这台执行 detach。")
    if not ask_yes("仍要继续？", False):
        print("已取消")
        sys.exit(1)


def power_off(esxi, vm):
    if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOn:
        return
    info(f"\n{vm.name} 正在运行，链接克隆要求母盘关机。")
    if not ask_yes("现在优雅关机？"):
        die("已取消")
    if vm.guest.toolsRunningStatus == "guestToolsRunning":
        vm.ShutdownGuest()
        for _ in range(120):
            if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOn:
                break
            time.sleep(2)
    if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
        info("优雅关机超时，强制关机…")
        esxi.wait(vm.PowerOffVM_Task(), "powerOff")
    info(f"已关机：{vm.name}")


def ensure_snapshot(esxi, vm):
    if vm.snapshot and vm.snapshot.currentSnapshot:
        names = [s.name for s in vm.snapshot.rootSnapshotList]
        info(f"\n已有快照：{', '.join(names)}")
        return
    info(f"\n{vm.name} 还没有快照。链接克隆需要先打一个快照，把母盘变成只读基础盘。")
    if not ask_yes(f"现在创建快照 {SNAP_NAME}？（不含内存）"):
        die("已取消")
    esxi.wait(vm.CreateSnapshot_Task(name=SNAP_NAME, description="linked-clone base",
                                     memory=False, quiesce=False), "创建快照")
    info(f"快照已创建：{SNAP_NAME}")


def inspect_base(esxi, vm):
    disks = [d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]
    if len(disks) != 1:
        raise RuntimeError(f"母盘有 {len(disks)} 块磁盘，暂不支持")
    ds, vmx_rel = split_ds(vm.config.files.vmPathName)
    folder = posixpath.dirname(vmx_rel)
    ads, active_rel = split_ds(disks[0].backing.fileName)
    if ads != ds or posixpath.dirname(active_rel) != folder:
        raise RuntimeError("磁盘不在母盘目录或不在同一数据存储，暂不支持")

    desc = esxi.ssh(f"cat {ds_root(ds)}/{active_rel}")
    vmx_text = esxi.ssh(f"cat {ds_root(ds)}/{vmx_rel}")
    info_d = {"ds": ds, "folder": folder, "vmx_rel": vmx_rel,
              "active_rel": active_rel, "vmx_text": vmx_text}

    token = None
    for line in desc.splitlines():
        t = line.strip()
        if t.startswith("CID="):
            info_d["cid"] = t.split("=", 1)[1].strip()
        elif t.startswith("parentCID="):
            info_d["parent_cid"] = t.split("=", 1)[1].strip()
        elif t.startswith("createType="):
            info_d["create_type"] = t.split("=", 1)[1].strip().strip('"')
        elif t.startswith("parentFileNameHint="):
            info_d["parent_hint"] = t.split("=", 1)[1].strip().strip('"')
        elif t.startswith("RW "):
            parts = t.split()
            info_d["sectors"] = parts[1]
            token = parts[2]
            info_d["extent"] = t.split('"')[1]
        elif t.startswith("ddb.longContentID"):
            info_d["long_cid"] = t.split("=", 1)[1].strip().strip('"')
        elif t.startswith("ddb.grain"):
            info_d["grain"] = t.split("=", 1)[1].strip().strip('"')

    for key in ("parent_hint", "extent", "parent_cid", "create_type", "sectors"):
        if not info_d.get(key):
            raise RuntimeError(f"增量盘描述符缺少 {key}，母盘可能不是快照状态")
    info_d["extent_token"] = token or "SESPARSE"
    info_d["extent_rel"] = posixpath.join(folder, info_d["extent"])

    active_base = posixpath.basename(active_rel)
    for line in vmx_text.splitlines():
        if "=" in line and line.split("=", 1)[1].strip().strip('"').split("/")[-1] == active_base:
            info_d["disk_vmx_key"] = line.split("=", 1)[0].strip()
            break
    if "disk_vmx_key" not in info_d:
        raise RuntimeError(f"在 {vm.name}.vmx 里找不到磁盘 {active_base} 的配置行")

    base_rel = posixpath.normpath(posixpath.join(folder, info_d["parent_hint"]))
    base_desc = esxi.ssh(f"cat {ds_root(ds)}/{base_rel} 2>/dev/null || true")
    if not base_desc.strip():
        raise RuntimeError(f"父基础盘不存在：{ds_root(ds)}/{base_rel}")
    base_cid = ""
    for line in base_desc.splitlines():
        if line.startswith("CID="):
            base_cid = line.split("=", 1)[1].strip()
    if base_cid and base_cid != info_d["parent_cid"]:
        raise RuntimeError(f"父盘 CID({base_cid}) 与增量盘 parentCID({info_d['parent_cid']}) 不一致")

    info_d["base_disk_rel"] = base_rel
    info_d["base_cid"] = base_cid or info_d["parent_cid"]
    try:
        info_d["delta_bytes"] = int(esxi.ssh(f"wc -c < {ds_root(ds)}/{info_d['extent_rel']}").strip())
    except Exception:
        info_d["delta_bytes"] = 0
    return info_d


# ----------------------------------------------------------------- 克隆
def build_descriptor(name, base):
    suffix = base["extent"].rsplit("-", 1)[-1]
    return f'''# Disk DescriptorFile
version=1
encoding="UTF-8"
CID={hashlib.md5(name.encode()).hexdigest()[:8]}
parentCID={base["base_cid"]}
createType="{base["create_type"]}"
parentFileNameHint="{{PARENT}}"
# Extent description
RW {base["sectors"]} {base["extent_token"]} "{name}-{suffix}"

# The Disk Data Base 
#DDB

ddb.grain = "{base.get("grain", 8)}"
ddb.longContentID = "{base.get("long_cid", "")}"
'''


def rewrite_vmx(text, name, disk_key):
    out = []
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip()
        if key in DROP_KEYS or key == disk_key:
            continue
        if key.startswith(("vmotion.", "checkpoint.")) or key.endswith((".redo", ".lck")):
            continue
        out.append(line)
    out += [f'displayName = "{name}"',
            f'nvram = "{name}.nvram"',
            f'extendedConfigFile = "{name}.vmxf"',
            f'{disk_key} = "{name}.vmdk"',
            'ethernet0.addressType = "generated"']
    return "\n".join(out) + "\n"


def assert_name_free(esxi, name, ds):
    path = f"[{ds}] {name}/{name}.vmx"
    for vm in esxi.find(vim.VirtualMachine):
        try:
            if vm.config and vm.config.files.vmPathName == path:
                raise RuntimeError(f"[{ds}] {name}/ 已被虚拟机「{vm.name}」占用")
        except RuntimeError:
            raise
        except Exception:
            continue


def register_vm(esxi, name, ds):
    host = esxi.find(vim.HostSystem)[0]
    pool = host.parent.resourcePool
    folder = esxi.api().content.rootFolder.childEntity[0].vmFolder
    return esxi.wait(folder.RegisterVM_Task(path=f"[{ds}] {name}/{name}.vmx", name=name,
                                            asTemplate=False, pool=pool, host=host),
                     f"register {name}", timeout=300)


def create_one(esxi, base, name):
    ds = base["ds"]
    root = ds_root(ds)
    dest = f"{root}/{name}"
    parent = posixpath.relpath(base["base_disk_rel"], name)
    desc = build_descriptor(name, base).replace("{PARENT}", parent)
    vmx = rewrite_vmx(base["vmx_text"], name, base["disk_vmx_key"])
    suffix = base["extent"].rsplit("-", 1)[-1]

    t0 = time.time()
    esxi.ssh(f"""set -e
rm -rf "{dest}"
mkdir -p "{dest}"
cp "{root}/{base['active_rel']}" "{dest}/{name}.vmdk"
cp "{root}/{base['extent_rel']}" "{dest}/{name}-{suffix}"
""", timeout=600)
    esxi.sftp_put(desc, f"{dest}/{name}.vmdk")
    esxi.sftp_put(vmx, f"{dest}/{name}.vmx")
    vm = register_vm(esxi, name, ds)
    if vm.name != name:
        esxi.wait(vm.Rename_Task(name), f"rename {name}")
    return vm, time.time() - t0


# ----------------------------------------------------------------- 删除 / 检查
def safe_delete(esxi, names, base_name=None):
    for name in names:
        if base_name and name == base_name:
            warn(f"拒绝删除母盘 {name}")
            continue
        vms = [v for v in esxi.find(vim.VirtualMachine) if v.name == name]
        if not vms:
            warn(f"找不到虚拟机 {name}")
            continue
        for vm in vms:
            ds = split_ds(vm.config.files.vmPathName)[0] if vm.config else None
            if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOff:
                esxi.wait(vm.PowerOffVM_Task(), "powerOff")
            vm.UnregisterVM()          # 关键：先注销，绝不用 Destroy_Task
            if ds:
                esxi.ssh(f"rm -rf {ds_root(ds)}/{name}")
            info(f"已删除 {name}（仅删除自身目录，母盘安全）")


def check_chains(esxi, prefix):
    vms = sorted(esxi.find(vim.VirtualMachine), key=lambda v: v.name)
    names, macs = {}, {}
    for vm in vms:
        names[vm.name] = names.get(vm.name, 0) + 1
        for d in vm.config.hardware.device if vm.config else []:
            if isinstance(d, vim.vm.device.VirtualEthernetCard):
                macs.setdefault(d.macAddress, []).append(vm.name)
    pending = macs.pop(None, [])
    info(f"虚拟机总数：{len(vms)}")
    info(f"重名：{ {k: v for k, v in names.items() if v > 1} or '无'}")
    info(f"MAC 冲突：{ {k: v for k, v in macs.items() if len(v) > 1} or '无'}")
    if pending:
        info(f"MAC 待首次开机生成：{len(pending)} 台（正常）")

    groups, broken = {}, []
    for vm in vms:
        if not vm.name.startswith(prefix):
            continue
        try:
            disk = next(d for d in vm.config.hardware.device
                        if isinstance(d, vim.vm.device.VirtualDisk))
            ds, disk_rel = split_ds(disk.backing.fileName)
        except Exception:
            continue
        desc = esxi.ssh(f"cat {ds_root(ds)}/{disk_rel} 2>/dev/null || true")
        hint = ""
        for line in desc.splitlines():
            if line.startswith("parentFileNameHint="):
                hint = line.split("=", 1)[1].strip().strip('"')
        if not hint:
            continue
        groups.setdefault(hint, []).append(vm.name)
        if esxi.ssh(f"test -f {ds_root(ds)}/{posixpath.normpath(posixpath.join(posixpath.dirname(disk_rel), hint))} "
                    "&& echo ok || echo missing").strip() != "ok":
            broken.append(vm.name)
    for hint, members in groups.items():
        info(f"链接克隆 {len(members)} 台共用父盘：{hint}（{members[0]} … {members[-1]}）")
    info(f"父盘缺失：{broken or '无（全部正常）'}")


# ----------------------------------------------------------------- 主流程
def cmd_create(esxi, args):
    rows = list_vms(esxi)
    info(f"共 {len(rows)} 台虚拟机\n")
    print_vms(rows)

    if args.base:
        match = [r for r in rows if r["name"] == args.base]
        if not match:
            die(f"找不到虚拟机 {args.base}")
        row = match[0]
    elif AUTO_YES:
        die("非交互模式请用 --base 指定母盘，例如 --base win11")
    else:
        row = pick_base(rows)
    vm = row["vm"]
    info(f"\n已选择母盘：{vm.name}")

    assert_not_linked_clone(esxi, vm)
    power_off(esxi, vm)
    ensure_snapshot(esxi, vm)
    base = inspect_base(esxi, vm)

    info("\n母盘信息")
    info(f"  目录        [{base['ds']}] {base['folder']}/")
    info(f"  基础盘      {base['base_disk_rel']}  (CID {base['base_cid']})")
    info(f"  活动增量盘  {base['active_rel']}")
    info(f"  增量大小    {fmt_size(base['delta_bytes'])}  ← 每台克隆机的初始体积")
    info(f"  每台父盘    {posixpath.relpath(base['base_disk_rel'], '<克隆目录>')}")

    if args.count is None:
        raw = ask("\n要克隆几台", "20")
        while not raw.isdigit() or int(raw) <= 0:
            raw = ask("请输入正整数", "20")
        count = int(raw)
    else:
        count = args.count
    prefix = args.prefix or (f"{vm.name}-" if args.count is not None else ask("命名前缀", f"{vm.name}-"))
    start = args.start
    pad = 2 if start + count - 1 < 100 else 3
    names = [f"{prefix}{i:0{pad}d}" for i in range(start, start + count)]

    existing = {r["name"] for r in rows}
    clash = [n for n in names if n in existing or n == vm.name]
    if clash:
        warn(f"名字已存在：{', '.join(clash[:6])}{' …' if len(clash) > 6 else ''}")
        if not ask_yes("跳过这些名字继续？", True):
            return
        names = [n for n in names if n not in existing and n != vm.name]
        if not names:
            die("没有可用名字")

    info(f"\n将创建 {len(names)} 台：{names[0]} … {names[-1]}")
    info(f"每台约 {fmt_size(base['delta_bytes'])}，合计约 {fmt_size(base['delta_bytes'] * len(names))}")
    if not ask_yes("确认开始？", True):
        return

    hr("开始克隆")
    ok, failed = [], []
    t_all = time.time()
    for i, name in enumerate(names, 1):
        try:
            assert_name_free(esxi, name, base["ds"])
            _, dt = create_one(esxi, base, name)
            size = esxi.ssh(f"du -sh {ds_root(base['ds'])}/{name} | cut -f1").strip()
            ok.append(name)
            print(f"[{i:>2}/{len(names)}] {name:24s} {dt:5.1f}s {size:>8s}", flush=True)
        except Exception as exc:
            failed.append((name, str(exc)[:200]))
            print(f"[{i:>2}/{len(names)}] {name:24s} 失败：{str(exc)[:150]}", flush=True)

    hr("完成")
    info(f"成功 {len(ok)} 台，失败 {len(failed)} 台，总耗时 {time.time()-t_all:.0f}s")
    for n, e in failed:
        info(f"  ✗ {n}: {e}")

    if ok and (args.power_on or ask_yes("\n是否立即开机？", False)):
        info("\n开机中…")
        for name in ok:
            try:
                vm2 = [v for v in esxi.find(vim.VirtualMachine) if v.name == name][0]
                esxi.wait(vm2.PowerOnVM_Task(), f"powerOn {name}", timeout=300)
                info(f"  {name} 已开机")
            except Exception as exc:
                warn(f"{name} 开机失败：{str(exc)[:120]}")

    info("\n下一步：")
    info("  * 克隆机的计算机名/SID 与母盘相同，请按需 sysprep 或逐台改名")
    info("  * 删除克隆：esxi-clone --delete <名字>   （不要用界面删除，会连母盘一起删）")
    info("  * 检查链：  esxi-clone --check")


def main():
    ap = argparse.ArgumentParser(
        prog="esxi-clone",
        description="ESXi 单机链接克隆工具（免 vCenter）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  esxi-clone                                 交互式创建
  esxi-clone --list                          列出虚拟机
  esxi-clone --base win11 --count 20 --yes   批量建 20 台
  esxi-clone --delete win11-05               安全删除
  esxi-clone --check                         检查父盘链
""")
    ap.add_argument("--host", help="ESXi 地址")
    ap.add_argument("--user", help="用户名，默认 root")
    ap.add_argument("--ssh-port", type=int, help="SSH 端口，默认 22")
    ap.add_argument("--save", action="store_true", help="保存地址/用户名（不保存密码）")
    ap.add_argument("--list", action="store_true", help="列出虚拟机")
    ap.add_argument("--check", action="store_true", help="检查克隆链完整性")
    ap.add_argument("--delete", nargs="+", metavar="NAME", help="安全删除克隆（注销 + 删自身目录）")
    ap.add_argument("--base", help="母盘虚拟机名称")
    ap.add_argument("--count", type=int, help="克隆数量")
    ap.add_argument("--prefix", help="命名前缀，默认 <母盘名>-")
    ap.add_argument("--start", type=int, default=1, help="起始编号，默认 1")
    ap.add_argument("--power-on", action="store_true", help="创建后立即开机")
    ap.add_argument("--yes", action="store_true", help="全部使用默认值，不再交互")
    ap.add_argument("--version", action="version", version=f"esxi-clone {__version__}")
    args = ap.parse_args()

    global AUTO_YES
    AUTO_YES = bool(args.yes)

    host, user, password, ssh_port = resolve_credentials(args)
    esxi = Esxi(host, user, password, ssh_port)
    try:
        hr("ESXi 链接克隆")
        info(f"连接 {host} …")
        esxi.api()
        if args.list:
            rows = list_vms(esxi)
            info(f"共 {len(rows)} 台虚拟机\n")
            print_vms(rows)
            return
        if args.check:
            check_chains(esxi, args.prefix or "")
            return
        if args.delete:
            safe_delete(esxi, args.delete, args.base)
            return
        cmd_create(esxi, args)
    finally:
        esxi.close()


if __name__ == "__main__":
    main()
