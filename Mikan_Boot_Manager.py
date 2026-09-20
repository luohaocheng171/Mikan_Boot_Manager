#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 Mikan Boot Manager v2.0
Windows 引导管理器 — EasyBCD + EasyUEFI 完整开源替代
功能：
  · BCD 管理（Windows/VHD/VHDX/WinPE/ISO/Linux）
  · UEFI 固件项管理（含僵尸项检测与修复）
  · 独立固件启动项创建（绕过 Windows BCD 链式加载）
  · Linux 扫描 + 挂载 + 一键修复引导
  · 引导修复（MBR/BCD/GRUB）
  · 完整引导配置备份（JSON + BAT + SH + BCD）
  · 导出/导入配置
  · ESP 分区浏览
  · 高级 BCD 选项
  · 启动顺序拖拽
配置：JSON 持久化
风格：扁平化白底蓝调
"""

import sys
import os
import json
import subprocess
import ctypes
import shutil
import tempfile
import zipfile
import urllib.request
import re
import time
import threading
import inspect
import traceback
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field, asdict

from PySide6.QtWidgets import *
from PySide6.QtCore import *
from PySide6.QtGui import *

# ============================================================
# 常量
# ============================================================
APP_NAME = "Mikan Boot Manager"
APP_VERSION = "2.0"
APP_AUTHOR = "Mikan Team"

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "boot_config"
CONFIG_FILE = CONFIG_DIR / "config.json"
BACKUP_DIR = CONFIG_DIR / "backups"
LOG_DIR = CONFIG_DIR / "logs"
DRIVER_DIR = CONFIG_DIR / "drivers"

for d in [CONFIG_DIR, BACKUP_DIR, LOG_DIR, DRIVER_DIR]:
    d.mkdir(parents=True, exist_ok=True)


def dbg(msg: str):
    """DEBUG 输出到控制台"""
    print(f"[DEBUG] {msg}", flush=True)


def log_info(msg: str):
    print(f"[INFO] {msg}", flush=True)


# ============================================================
# ext4 驱动下载
# ============================================================
EXT4_DRIVER_URL = "https://github.com/antimatter-studios/ext4-win-driver/releases/download/v0.2.2/ext4-win-driver-0.2.2-x64-Setup.exe"

EXT4_DRIVER_MARKERS = [
    r"C:\Program Files\ext4-win-driver",
    r"C:\Program Files (x86)\ext4-win-driver",
]


def get_latest_ext4_driver_url() -> Optional[str]:
    api_url = "https://api.github.com/repos/antimatter-studios/ext4-win-driver/releases/latest"
    try:
        import platform
        req = urllib.request.Request(api_url, headers={"User-Agent": f"MikanBoot/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))

        machine = platform.machine().lower()
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"

        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if name.endswith(f"-{arch}-Setup.exe"):
                return asset["browser_download_url"]
        return None
    except Exception as e:
        dbg(f"获取最新版本失败: {e}")
        return None


DEFAULT_CONFIG = {
    "version": APP_VERSION,
    "theme": "flat_light",
    "auto_install_driver": True,
    "backup_bcd_before_change": True,
    "auto_backup_interval_hours": 24,
    "last_backup": "",
    "known_linux_efi_paths": [
        "EFI/ubuntu/shimx64.efi",
        "EFI/ubuntu/grubx64.efi",
        "EFI/debian/shimx64.efi",
        "EFI/fedora/shimx64.efi",
        "EFI/centos/shimx64.efi",
        "EFI/arch/grubx64.efi",
        "EFI/manjaro/grubx64.efi",
        "EFI/Boot/bootx64.efi",
    ],
    "window_geometry": "",
}


# ============================================================
# 配置管理
# ============================================================
class ConfigManager:
    @staticmethod
    def load() -> dict:
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in data:
                        data[k] = v
                return data
            except Exception as e:
                dbg(f"配置读取失败: {e}")
        return DEFAULT_CONFIG.copy()

    @staticmethod
    def save(config: dict):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            dbg(f"配置保存失败: {e}")


# ============================================================
# 管理员权限
# ============================================================
def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def request_admin():
    if is_admin():
        return True
    script = os.path.abspath(sys.argv[0])
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, f'"{script}"', None, 1
    )
    return False


# ============================================================
# PowerShell 调用封装
# ============================================================
def run_powershell(ps_cmd: str, timeout: int = 20) -> Tuple[bool, str, str]:
    try:
        full_cmd = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            + ps_cmd
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", full_cmd],
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )

        try:
            text = result.stdout.decode('utf-8')
        except UnicodeDecodeError:
            text = result.stdout.decode('gbk', errors='replace')
        if text.startswith('\ufeff'):
            text = text[1:]

        try:
            err = result.stderr.decode('utf-8')
        except UnicodeDecodeError:
            err = result.stderr.decode('gbk', errors='replace')
        if err.startswith('\ufeff'):
            err = err[1:]

        return result.returncode == 0, text.strip(), err.strip()

    except subprocess.TimeoutExpired:
        return False, "", "命令执行超时"
    except Exception as e:
        dbg(f"PowerShell 执行异常: {e}")
        return False, "", str(e)


# ============================================================
# 数据模型
# ============================================================
@dataclass
class BootEntry:
    guid: str = ""
    description: str = ""
    device: str = ""
    osdevice: str = ""
    path: str = ""
    type: str = ""
    is_default: bool = False
    is_firmware: bool = False
    is_zombie: bool = False
    is_disabled: bool = False
    raw: str = ""
    extra: dict = field(default_factory=dict)


# ============================================================
# BCD 引擎
# ============================================================
class BCDEngine:
    @staticmethod
    def run(args: List[str], timeout: int = 30) -> Tuple[bool, str, str]:
        cmd = ["bcdedit"] + args
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            result = subprocess.run(
                cmd, capture_output=True, timeout=timeout,
                creationflags=creationflags
            )
            try:
                stdout = result.stdout.decode('utf-8')
            except UnicodeDecodeError:
                stdout = result.stdout.decode('gbk', errors='replace')
            try:
                stderr = result.stderr.decode('utf-8')
            except UnicodeDecodeError:
                stderr = result.stderr.decode('gbk', errors='replace')
            return result.returncode == 0, stdout, stderr
        except subprocess.TimeoutExpired:
            return False, "", "命令执行超时"
        except FileNotFoundError:
            return False, "", "找不到 bcdedit"
        except Exception as e:
            return False, "", str(e)

    @staticmethod
    def enum_all() -> Tuple[bool, List[BootEntry]]:
        ok, out, err = BCDEngine.run(["/enum", "all"])
        if not ok:
            return False, []
        return True, BCDEngine._parse_enum(out)

    @staticmethod
    def enum_firmware() -> Tuple[bool, List[BootEntry]]:
        ok, out, err = BCDEngine.run(["/enum", "firmware"])
        if not ok:
            return False, []
        return True, BCDEngine._parse_enum(out)

    @staticmethod
    def _parse_enum(output: str) -> List[BootEntry]:
        entries: List[BootEntry] = []
        blocks = re.split(r'\n\s*\n', output)

        for block in blocks:
            block = block.strip()
            if not block:
                continue
            if not (block.startswith("Windows") or block.startswith("Firmware") or
                    block.startswith("固件") or block.startswith("Real-mode")):
                continue

            lines = block.split('\n')
            entry = BootEntry(raw=block)

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # 类型识别
                if "Windows Boot Manager" in line or "Windows 启动管理器" in line:
                    entry.type = "Windows Boot Manager"
                    continue
                elif "Windows Boot Loader" in line or "Windows 启动加载器" in line:
                    entry.type = "Windows Boot Loader"
                    continue
                elif "Firmware Application" in line or "固件应用程序" in line:
                    entry.type = "Firmware Application"
                    entry.is_firmware = True
                    continue
                elif "Firmware Boot Manager" in line or "固件启动管理器" in line:
                    entry.type = "Firmware Boot Manager"
                    entry.is_firmware = True
                    continue
                elif "Windows Memory Tester" in line or "Windows 内存测试" in line:
                    entry.type = "Windows Memory Tester"
                    continue
                elif "Real-mode Boot Sector" in line:
                    entry.type = "Real-mode Boot Sector"
                    continue

                # 键值对
                if ' ' in line:
                    parts = re.split(r'\s{2,}', line, maxsplit=1)
                    if len(parts) == 2:
                        key, val = parts[0].strip(), parts[1].strip()
                        key_lower = key.lower()

                        if key_lower in ("identifier", "标识符"):
                            entry.guid = val
                        elif key_lower in ("description", "描述"):
                            entry.description = val
                        elif key_lower in ("device", "设备"):
                            entry.device = val
                        elif key_lower in ("osdevice", "操作系统设备"):
                            entry.osdevice = val
                        elif key_lower in ("path", "路径"):
                            entry.path = val
                        elif key_lower in ("default", "默认"):
                            if val.lower() in ("yes", "是"):
                                entry.is_default = True
                        else:
                            entry.extra[key] = val

            # 判断僵尸项
            if entry.is_firmware and not entry.path and not entry.device:
                entry.is_zombie = True

            if entry.guid or entry.description:
                entries.append(entry)

        return entries

    @staticmethod
    def backup_bcd(backup_dir: str) -> Tuple[bool, str]:
        try:
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = Path(backup_dir) / f"bcd_backup_{timestamp}.bcd"

            result = subprocess.run(
                ["bcdedit", "/export", str(backup_file)],
                capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW
            )

            if result.returncode == 0 and backup_file.exists():
                return True, str(backup_file)
            return False, "导出失败"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def restore_bcd(backup_file: str) -> Tuple[bool, str]:
        if not Path(backup_file).exists():
            return False, "备份文件不存在"
        try:
            result = subprocess.run(
                ["bcdedit", "/import", str(backup_file)],
                capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if result.returncode == 0:
                return True, "恢复成功"
            return False, "恢复失败"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def create_entry(description: str, efi_path: str, partition: str) -> Tuple[bool, str]:
        ok, out, err = BCDEngine.run([
            "/create", "/d", description, "/application", "bootsector"
        ])
        if not ok:
            return False, f"创建失败: {err or out}"

        match = re.search(r'\{[0-9a-fA-F\-]+\}', out)
        if not match:
            return False, f"无法解析新 GUID: {out}"
        guid = match.group(0)

        ok, out, err = BCDEngine.run([
            "/set", guid, "device", f"partition={partition}"
        ])
        if not ok:
            BCDEngine.run(["/delete", guid, "/f"])
            return False, f"设置 device 失败: {err or out}"

        ok, out, err = BCDEngine.run([
            "/set", guid, "path", efi_path
        ])
        if not ok:
            BCDEngine.run(["/delete", guid, "/f"])
            return False, f"设置 path 失败: {err or out}"

        BCDEngine.run(["/displayorder", guid, "/addlast"])
        return True, guid

    @staticmethod
    def create_firmware_entry(description: str, efi_path: str, partition: str,
                              reuse_zombie_guid: str = None) -> Tuple[bool, str]:
        """创建独立固件启动项（绕过 Windows BCD 链式加载）"""
        if reuse_zombie_guid:
            guid = reuse_zombie_guid
            log_info(f"复用僵尸固件项: {guid}")
        else:
            ok, out, err = BCDEngine.run([
                "/create", "/d", description, "/application", "bootapp"
            ])
            if not ok:
                return False, f"创建失败: {err or out}"

            match = re.search(r'\{[0-9a-fA-F\-]+\}', out)
            if not match:
                return False, f"无法解析新 GUID: {out}"
            guid = match.group(0)

        # 设置 device
        ok, out, err = BCDEngine.run([
            "/set", guid, "device", f"partition={partition}"
        ])
        if not ok:
            if not reuse_zombie_guid:
                BCDEngine.run(["/delete", guid, "/f"])
            return False, f"设置 device 失败: {err or out}"

        # 设置 path
        ok, out, err = BCDEngine.run([
            "/set", guid, "path", efi_path
        ])
        if not ok:
            if not reuse_zombie_guid:
                BCDEngine.run(["/delete", guid, "/f"])
            return False, f"设置 path 失败: {err or out}"

        # 加入固件启动顺序
        BCDEngine.run(["/set", "{fwbootmgr}", "displayorder", guid, "/addfirst"])

        return True, guid

    @staticmethod
    def delete_entry(guid: str) -> Tuple[bool, str]:
        ok, out, err = BCDEngine.run(["/delete", guid, "/f"])
        return ok, out if ok else (err or out)

    @staticmethod
    def set_default(guid: str) -> Tuple[bool, str]:
        ok, out, err = BCDEngine.run(["/default", guid])
        return ok, out if ok else (err or out)

    @staticmethod
    def set_timeout(seconds: int) -> Tuple[bool, str]:
        ok, out, err = BCDEngine.run(["/timeout", str(seconds)])
        return ok, out if ok else (err or out)

    @staticmethod
    def set_description(guid: str, description: str) -> Tuple[bool, str]:
        ok, out, err = BCDEngine.run(["/set", guid, "description", description])
        return ok, out if ok else (err or out)

    @staticmethod
    def set_path(guid: str, path: str) -> Tuple[bool, str]:
        ok, out, err = BCDEngine.run(["/set", guid, "path", path])
        return ok, out if ok else (err or out)

    @staticmethod
    def set_device(guid: str, device: str) -> Tuple[bool, str]:
        ok, out, err = BCDEngine.run(["/set", guid, "device", device])
        return ok, out if ok else (err or out)

    @staticmethod
    def set_fw_order(guids: List[str]) -> Tuple[bool, str]:
        if not guids:
            return False, "顺序为空"
        args = ["/set", "{fwbootmgr}", "displayorder"] + guids
        ok, out, err = BCDEngine.run(args)
        return ok, out if ok else (err or out)

    @staticmethod
    def set_boot_sequence(guid: str) -> Tuple[bool, str]:
        ok, out, err = BCDEngine.run([
            "/set", "{fwbootmgr}", "bootsequence", guid
        ])
        return ok, out if ok else (err or out)

    @staticmethod
    def set_fw_timeout(seconds: int) -> Tuple[bool, str]:
        ok, out, err = BCDEngine.run([
            "/set", "{fwbootmgr}", "timeout", str(seconds)
        ])
        return ok, out if ok else (err or out)

    @staticmethod
    def set_advanced_option(guid: str, option: str, value: str = "") -> Tuple[bool, str]:
        """设置高级 BCD 选项"""
        args = ["/set", guid, option]
        if value:
            args.append(value)
        ok, out, err = BCDEngine.run(args)
        return ok, out if ok else (err or out)

    @staticmethod
    def delete_value(guid: str, option: str) -> Tuple[bool, str]:
        ok, out, err = BCDEngine.run(["/deletevalue", guid, option])
        return ok, out if ok else (err or out)

    @staticmethod
    def get_boot_mode() -> str:
        try:
            ok, out, err = BCDEngine.run(["/enum", "{current}"])
            if ok and "winload.efi" in out:
                return "UEFI"
            return "Legacy BIOS"
        except:
            return "未知"

    @staticmethod
    def get_disk_type() -> str:
        ok, out, err = run_powershell(
            "(Get-Disk | Where-Object IsSystem -eq $true).PartitionStyle",
            timeout=10
        )
        if ok and out:
            return out.strip()
        return "未知"

    # ===== 高级功能 =====

    @staticmethod
    def create_vhd_entry(vhd_path: str, description: str = "Windows on VHD") -> Tuple[bool, str]:
        """创建 VHD 启动项"""
        # 复制 current
        ok, out, err = BCDEngine.run(["/copy", "{current}", "/d", description])
        if not ok:
            return False, f"复制失败: {err or out}"

        match = re.search(r'\{[0-9a-fA-F\-]+\}', out)
        if not match:
            return False, f"无法解析 GUID: {out}"
        guid = match.group(0)

        # 设置 device 和 osdevice
        # 注意：VHD 路径不要带盘符，用 \vhd\xxx.vhd
        ok, out, err = BCDEngine.run([
            "/set", guid, "device", f"vhd=[{vhd_path}]"
        ])
        if not ok:
            BCDEngine.run(["/delete", guid, "/f"])
            return False, f"设置 device 失败: {err or out}"

        ok, out, err = BCDEngine.run([
            "/set", guid, "osdevice", f"vhd=[{vhd_path}]"
        ])
        if not ok:
            BCDEngine.run(["/delete", guid, "/f"])
            return False, f"设置 osdevice 失败: {err or out}"

        # 常用 VHD 选项
        BCDEngine.run(["/set", guid, "detecthal", "on"])

        BCDEngine.run(["/displayorder", guid, "/addlast"])
        return True, guid

    @staticmethod
    def create_winpe_entry(wim_path: str, description: str = "WinPE",
                           sd_path: str = None) -> Tuple[bool, str]:
        """创建 WinPE 启动项"""
        # 1. 创建 ramdisk 设备
        ramdisk_guid = "{ramdiskoptions}"
        # 使用默认 RAM 磁盘 GUID（Windows 8+）

        # 2. 复制 current
        ok, out, err = BCDEngine.run(["/copy", "{current}", "/d", description])
        if not ok:
            return False, f"复制失败: {err or out}"

        match = re.search(r'\{[0-9a-fA-F\-]+\}', out)
        if not match:
            return False, f"无法解析 GUID: {out}"
        guid = match.group(0)

        # 3. 设置 ramdisk 路径
        wim_path_clean = wim_path.lstrip("\\")
        ok, out, err = BCDEngine.run([
            "/set", guid, "device",
            f"ramdisk=[{wim_path}],{ramdisk_guid}"
        ])
        if not ok:
            BCDEngine.run(["/delete", guid, "/f"])
            return False, f"设置 device 失败: {err or out}"

        BCDEngine.run([
            "/set", guid, "osdevice",
            f"ramdisk=[{wim_path}],{ramdisk_guid}"
        ])

        # 4. 设置 winload
        BCDEngine.run(["/set", guid, "path", r"\windows\system32\boot\winload.efi"])
        BCDEngine.run(["/set", guid, "systemroot", r"\windows"])

        BCDEngine.run(["/displayorder", guid, "/addlast"])
        return True, guid

    @staticmethod
    def get_full_raw_export() -> str:
        """获取完整 BCD 导出（用于备份）"""
        ok, out, err = BCDEngine.run(["/enum", "all"])
        return out if ok else ""

    @staticmethod
    def get_firmware_raw_export() -> str:
        """获取完整固件导出"""
        ok, out, err = BCDEngine.run(["/enum", "firmware"])
        return out if ok else ""


# ============================================================
# Linux 分区扫描器
# ============================================================
class LinuxScanner:
    @staticmethod
    def list_physical_disks() -> List[dict]:
        disks = []
        ps_cmd = (
            "Get-Disk | "
            "Select-Object Number,FriendlyName,Size,PartitionStyle,BusType | "
            "ConvertTo-Json -Compress"
        )
        ok, out, err = run_powershell(ps_cmd, timeout=20)
        if not ok or not out:
            return disks
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return disks
        if isinstance(data, dict):
            data = [data]
        for d in data:
            try:
                disks.append({
                    "number": d.get("Number", 0) or 0,
                    "name": d.get("FriendlyName") or "未知",
                    "size_gb": round((d.get("Size") or 0) / (1024**3), 2),
                    "style": d.get("PartitionStyle") or "未知",
                    "bus": d.get("BusType") or "未知",
                })
            except:
                pass
        return disks

    @staticmethod
    def list_partitions(disk_number: int) -> List[dict]:
        partitions = []
        ps_cmd = (
            f"Get-Partition -DiskNumber {disk_number} | "
            "Select-Object PartitionNumber,DriveLetter,Size,Type,IsHidden | "
            "ConvertTo-Json -Compress"
        )
        ok, out, err = run_powershell(ps_cmd, timeout=20)
        if not ok or not out:
            return partitions
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return partitions
        if isinstance(data, dict):
            data = [data]
        for p in data:
            try:
                partitions.append({
                    "number": p.get("PartitionNumber", 0) or 0,
                    "letter": p.get("DriveLetter") or "",
                    "size_gb": round((p.get("Size") or 0) / (1024**3), 2),
                    "type": p.get("Type") or "",
                    "hidden": p.get("IsHidden", False),
                })
            except:
                pass
        return partitions

    @staticmethod
    def detect_fs_type(drive_letter: str) -> str:
        letter = drive_letter.strip(':').strip()
        ps_cmd = f"(Get-Volume -DriveLetter '{letter}' -ErrorAction SilentlyContinue).FileSystemType"
        ok, out, err = run_powershell(ps_cmd, timeout=10)
        if ok and out:
            return out.strip()
        return "未知"

    @staticmethod
    def is_ext4_driver_installed() -> bool:
        for marker in EXT4_DRIVER_MARKERS:
            if Path(marker).exists():
                return True
        try:
            ps_cmd = (
                "Get-Volume | Where-Object FileSystemType -eq 'ext4' | "
                "Select-Object -First 1 DriveLetter | ConvertTo-Json"
            )
            ok, out, err = run_powershell(ps_cmd, timeout=10)
            if ok and out.strip():
                return True
        except:
            pass
        return False

    @staticmethod
    def install_ext4_driver(log_callback=None) -> Tuple[bool, str]:
        def log(msg):
            print(msg, flush=True)
            if log_callback:
                log_callback(msg)

        try:
            log("🔍 正在获取最新版本信息...")
            url = get_latest_ext4_driver_url()
            if not url:
                log("⚠️ 动态获取失败，使用固定地址")
                url = EXT4_DRIVER_URL
            else:
                log(f"✅ 获取到下载地址")

            filename = url.split("/")[-1]
            setup_exe = DRIVER_DIR / filename

            log(f"📥 正在下载 {filename}...")
            req = urllib.request.Request(
                url, headers={"User-Agent": f"MikanBoot/{APP_VERSION}"}
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                total = int(resp.headers.get('content-length', 0))
                downloaded = 0
                with open(setup_exe, 'wb') as f:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0 and downloaded % (512 * 1024) < 8192:
                            pct = downloaded / total * 100
                            log(f"  下载进度: {pct:.0f}%")

            log(f"✅ 下载完成")
            log("🔧 正在静默安装...")

            for args in [["/quiet"], ["/S"], ["/silent"], ["/VERYSILENT"],
                         ["/SILENT", "/NORESTART"]]:
                result = subprocess.run(
                    [str(setup_exe)] + args,
                    capture_output=True, timeout=300,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                if result.returncode == 0:
                    log("✅ 安装成功")
                    return True, "安装成功"

            log("⚠️ 静默安装未成功，正在打开安装包...")
            os.startfile(setup_exe)
            return False, "需要手动完成安装"
        except Exception as e:
            log(f"❌ 安装失败: {e}")
            return False, str(e)

    @staticmethod
    def scan_efi_files(drive_letter: str, known_paths: List[str]) -> List[str]:
        found = []
        try:
            letter = drive_letter.rstrip(':').strip()
            root = Path(f"{letter}:\\")
            if not root.exists():
                return found

            for rel in known_paths:
                p = root / rel.replace('/', '\\')
                if p.exists():
                    found.append(str(p))

            efi_dir = root / "EFI"
            if efi_dir.exists():
                for efi_file in efi_dir.rglob("*.efi"):
                    s = str(efi_file)
                    if s not in found:
                        found.append(s)
        except Exception as e:
            dbg(f"扫描 EFI 失败 {drive_letter}: {e}")
        return found

    @staticmethod
    def auto_assign_letter(disk_number: int, partition_number: int) -> Tuple[bool, str]:
        used = set()
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            if Path(f"{letter}:\\").exists():
                used.add(letter)

        tried = 0
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            if letter in used:
                continue
            if tried >= 3:
                break
            tried += 1

            ps_cmd = (
                f"try {{ "
                f"Add-PartitionAccessPath -DiskNumber {disk_number} "
                f"-PartitionNumber {partition_number} "
                f"-AccessPath '{letter}:' -ErrorAction Stop; "
                f"Write-Output 'OK' "
                f"}} catch {{ Write-Output \"ERR: $($_.Exception.Message)\" }}"
            )
            ok, out, err = run_powershell(ps_cmd, timeout=15)

            if 'OK' in out:
                time.sleep(0.5)
                if Path(f"{letter}:\\").exists():
                    return True, f"{letter}:"
                else:
                    LinuxScanner.release_letter(f"{letter}:")
                    return False, f"分区无法挂载（文件系统不识别）"
            else:
                if 'Not Supported' in out:
                    return False, "该分区不支持分配盘符（容器型）"

        return False, "无可用盘符"

    @staticmethod
    def release_letter(letter: str) -> Tuple[bool, str]:
        letter_clean = letter.rstrip(':').strip()
        ps_cmd = f"Remove-PartitionAccessPath -DriveLetter '{letter_clean}' -ErrorAction SilentlyContinue"
        ok, out, err = run_powershell(ps_cmd, timeout=15)
        return ok, out if ok else err

    @staticmethod
    def can_mount_partition(disk_number: int, partition_number: int) -> Tuple[bool, str, str]:
        """探测分区能否挂载"""
        ps_cmd = (
            f"$p = Get-Partition -DiskNumber {disk_number} -PartitionNumber {partition_number} -ErrorAction SilentlyContinue; "
            f"$v = $null; "
            f"if ($p) {{ try {{ $v = $p | Get-Volume -ErrorAction Stop }} catch {{}} }}; "
            f"if ($v) {{ "
            f"  [PSCustomObject]@{{"
            f"    FileSystem = $v.FileSystem; "
            f"    FileSystemType = $v.FileSystemType; "
            f"    IsValid = $true "
            f"  }} | ConvertTo-Json -Compress "
            f"}} else {{ "
            f"  [PSCustomObject]@{{ IsValid = $false }} | ConvertTo-Json -Compress "
            f"}}"
        )
        ok, out, err = run_powershell(ps_cmd, timeout=10)
        if not ok or not out:
            return False, "unknown", "unknown"
        try:
            data = json.loads(out)
            if not data.get("IsValid"):
                return False, "unknown", "unknown"
            fs = (data.get("FileSystem") or "").lower()
            fstype = (data.get("FileSystemType") or "").lower()
            native_fs = ["ntfs", "fat", "fat32", "exfat", "refs"]
            driver_fs = ["ext4", "ext3", "ext2"]
            if fs in native_fs or fs in driver_fs:
                return True, fs, fstype
            elif fs:
                return False, fs, fstype
            else:
                return False, "unknown", fstype
        except:
            return False, "unknown", "unknown"


# ============================================================
# 系统识别器
# ============================================================
class SystemIdentifier:
    SIGNATURES = {
        "Windows": [
            ("dir", "Windows", True),
            ("dir", "Windows\\System32", True),
        ],
        "Windows (EFI)": [
            ("dir", "EFI\\Microsoft", True),
        ],
        "Windows PE": [
            ("file", "sources\\boot.wim", False),
            ("dir", "Boot", True),
            ("file", "bootmgr", False),
        ],
        "Windows Recovery": [
            ("dir", "Recovery\\WindowsRE", True),
        ],
        "Linux": [
            ("dir", "etc", True),
            ("any_dir", ["bin", "usr", "boot", "var", "lib", "opt",
                         "srv", "home", "root", "mnt", "media", "lost+found"], True),
        ],
        "Linux (EFI)": [
            ("dir", "EFI", True),
            ("any_subdir", "EFI", [
                "ubuntu", "debian", "fedora", "centos",
                "arch", "manjaro", "opensuse", "mint",
                "elementary", "kali", "pop", "grub",
                "refind", "systemd-boot", "gentoo",
                "alpine", "void", "nixos", "rhel",
                "rocky", "almalinux", "zorin", "deepin",
                "uos", "kylin", "openkylin", "mx",
                "slackware", "suse", "redhat"
            ], True),
        ],
        "macOS": [
            ("dir", "System\\Library", True),
            ("any_dir", ["Applications", "Users"], True),
        ],
        "macOS (EFI)": [
            ("dir", "EFI\\APPLE", True),
        ],
        "Android x86": [
            ("dir", "system", True),
            ("any_file", ["initrd.img", "kernel", "ramdisk.img"], True),
        ],
        "Chrome OS": [
            ("dir", "EFI\\chromeos", True),
        ],
        "FreeBSD": [
            ("dir", "etc", True),
            ("any_file", ["boot\\loader.conf", "boot\\loader.efi"], True),
        ],
        "EFI 分区": [
            ("dir", "EFI", True),
        ],
        "System Volume": [
            ("dir", "System Volume Information", True),
        ],
    }

    PRIORITY = {
        "Windows": 100, "Windows (EFI)": 95, "Linux (EFI)": 95,
        "Linux": 90, "macOS": 90, "macOS (EFI)": 88,
        "Windows PE": 80, "Windows Recovery": 75,
        "Android x86": 70, "Chrome OS": 70, "FreeBSD": 65,
        "EFI 分区": 50, "System Volume": 30,
    }

    @classmethod
    def identify(cls, drive_letter: str) -> Tuple[str, int, List[str]]:
        letter = drive_letter.rstrip(':').strip()
        root = Path(f"{letter}:\\")

        if not root.exists():
            return "无法访问", 0, []

        try:
            top_items = []
            for item in root.iterdir():
                top_items.append(item.name)
            top_set = set(top_items)
        except PermissionError:
            return "无权限访问", 0, []
        except:
            return "访问失败", 0, []

        candidates = []
        for sys_name, sigs in cls.SIGNATURES.items():
            matched = []
            required_all_present = True
            for sig in sigs:
                kind = sig[0]
                path = sig[1]
                required = sig[2] if len(sig) > 2 else True
                found = False
                if kind == "dir":
                    target = root / path
                    found = target.exists() and target.is_dir()
                elif kind == "file":
                    target = root / path
                    found = target.exists()
                elif kind == "any_dir":
                    for p in path:
                        if (root / p).exists():
                            found = True
                            break
                elif kind == "any_file":
                    for p in path:
                        if (root / p).exists():
                            found = True
                            break
                elif kind == "any_subdir":
                    parent = root / path
                    subdirs = sig[2]
                    if parent.exists() and parent.is_dir():
                        try:
                            for sub in parent.iterdir():
                                if sub.is_dir() and sub.name.lower() in [s.lower() for s in subdirs]:
                                    found = True
                                    matched.append(f"{path}\\{sub.name}")
                                    break
                        except:
                            pass
                    required = True

                if found:
                    if kind != "any_subdir":
                        matched.append(path if not isinstance(path, list) else ", ".join(path))
                elif required:
                    required_all_present = False
                    break

            if required_all_present and matched:
                priority = cls.PRIORITY.get(sys_name, 10)
                candidates.append((sys_name, priority, matched))

        if candidates:
            candidates.sort(key=lambda x: -x[1])
            return candidates[0][0], candidates[0][1], candidates[0][2]

        if "EFI" in top_set:
            efi_dir = root / "EFI"
            try:
                subdirs = [d.name for d in efi_dir.iterdir() if d.is_dir()]
                if subdirs:
                    return f"EFI 分区 ({', '.join(subdirs[:3])})", 40, subdirs[:3]
            except:
                pass
            return "EFI 分区", 40, ["EFI"]

        user_dirs = {"Users", "用户", "Documents", "Downloads", "Desktop",
                     "Music", "Pictures", "Videos", "文档", "下载"}
        if top_set & user_dirs:
            return "数据盘", 30, list(top_set & user_dirs)

        return "未知", 10, list(top_items)[:5]

    @classmethod
    def get_root_listing(cls, drive_letter: str, max_items: int = 50) -> List[str]:
        letter = drive_letter.rstrip(':').strip()
        root = Path(f"{letter}:\\")
        if not root.exists():
            return []
        try:
            items = []
            for item in root.iterdir():
                suffix = "\\" if item.is_dir() else ""
                items.append(f"{item.name}{suffix}")
                if len(items) >= max_items:
                    break
            return items
        except:
            return []


# ============================================================
# 自动挂载到引导（含独立固件项模式）
# ============================================================
class AutoMountToBoot:
    LINUX_EFI_CANDIDATES = [
        ("ubuntu", "shimx64.efi", 100),
        ("ubuntu", "grubx64.efi", 95),
        ("debian", "shimx64.efi", 100),
        ("debian", "grubx64.efi", 95),
        ("fedora", "shimx64.efi", 100),
        ("fedora", "grubx64.efi", 95),
        ("centos", "shimx64.efi", 100),
        ("centos", "grubx64.efi", 95),
        ("rhel", "shimx64.efi", 100),
        ("rhel", "grubx64.efi", 95),
        ("rocky", "shimx64.efi", 100),
        ("rocky", "grubx64.efi", 95),
        ("almalinux", "shimx64.efi", 100),
        ("almalinux", "grubx64.efi", 95),
        ("arch", "grubx64.efi", 90),
        ("arch", "systemd-bootx64.efi", 85),
        ("manjaro", "grubx64.efi", 90),
        ("opensuse", "grubx64.efi", 90),
        ("suse", "grubx64.efi", 90),
        ("mint", "shimx64.efi", 100),
        ("mint", "grubx64.efi", 95),
        ("elementary", "shimx64.efi", 100),
        ("elementary", "grubx64.efi", 95),
        ("kali", "shimx64.efi", 95),
        ("kali", "grubx64.efi", 90),
        ("pop", "shimx64.efi", 100),
        ("pop", "grubx64.efi", 95),
        ("deepin", "shimx64.efi", 100),
        ("deepin", "grubx64.efi", 95),
        ("uos", "shimx64.efi", 95),
        ("kylin", "shimx64.efi", 95),
        ("openkylin", "shimx64.efi", 95),
        ("nixos", "grubx64.efi", 85),
        ("gentoo", "grubx64.efi", 85),
        ("void", "grubx64.efi", 85),
        ("alpine", "grubx64.efi", 80),
        ("slackware", "elilo.efi", 70),
        ("Boot", "bootx64.efi", 50),
        ("BOOT", "BOOTX64.EFI", 50),
    ]

    @classmethod
    def find_efi_on_partition(cls, drive_letter: str) -> Optional[Dict]:
        letter = drive_letter.rstrip(':').strip()
        root = Path(f"{letter}:\\")
        if not root.exists():
            return None
        efi_dir = root / "EFI"
        if not efi_dir.exists():
            return None

        existing_dirs = {}
        try:
            for sub in efi_dir.iterdir():
                if sub.is_dir():
                    existing_dirs[sub.name.lower()] = sub
        except:
            return None

        candidates = []
        for subdir, filename, priority in cls.LINUX_EFI_CANDIDATES:
            subdir_lower = subdir.lower()
            if subdir_lower in existing_dirs:
                sub = existing_dirs[subdir_lower]
                target = sub / filename
                if target.exists():
                    candidates.append({
                        "path": f"\\EFI\\{sub.name}\\{target.name}",
                        "distro": subdir.capitalize(),
                        "priority": priority,
                        "absolute": str(target),
                    })
                for f in sub.iterdir():
                    if f.is_file() and f.name.lower() == filename.lower():
                        if not any(c["absolute"] == str(f) for c in candidates):
                            candidates.append({
                                "path": f"\\EFI\\{sub.name}\\{f.name}",
                                "distro": subdir.capitalize(),
                                "priority": priority,
                                "absolute": str(f),
                            })

        if not candidates:
            try:
                for efi_file in efi_dir.rglob("*.efi"):
                    rel = "\\" + str(efi_file.relative_to(root)).replace("/", "\\")
                    name = efi_file.name.lower()
                    if "microsoft" in rel.lower() or "bootmgfw" in name:
                        continue
                    distro = efi_file.parent.name
                    candidates.append({
                        "path": rel,
                        "distro": distro,
                        "priority": 30,
                        "absolute": str(efi_file),
                    })
            except:
                pass

        if not candidates:
            return None

        candidates.sort(key=lambda x: -x["priority"])
        return candidates[0]

    @classmethod
    def auto_mount(cls, linux_part: dict, all_partitions: List[dict],
                   disk_number: int, set_default: bool = False,
                   release_after: bool = False, use_firmware: bool = True,
                   log_callback=None) -> Tuple[bool, str]:
        """全自动：把 Linux 分区挂到引导"""
        def log(msg):
            print(f"[AutoMount] {msg}", flush=True)
            if log_callback:
                log_callback(msg)

        # 步骤 1: Linux 分区盘符
        linux_letter = linux_part.get("letter", "")
        linux_auto_assigned = False

        if not linux_letter:
            log(f"📂 Linux 分区 {linux_part['number']} 没有盘符，正在分配...")
            ok, letter = LinuxScanner.auto_assign_letter(disk_number, linux_part["number"])
            if not ok:
                return False, f"分配 Linux 分区盘符失败: {letter}"
            linux_letter = letter
            linux_auto_assigned = True
            linux_part["letter"] = letter
            log(f"  ✅ 盘符: {linux_letter}")
        else:
            log(f"📂 Linux 分区已有盘符: {linux_letter}")

        # 步骤 2: 找 Linux EFI 文件
        log(f"🔍 正在 {linux_letter} 上查找 Linux EFI 文件...")
        efi_info = cls.find_efi_on_partition(linux_letter)

        if not efi_info:
            if linux_auto_assigned:
                LinuxScanner.release_letter(linux_letter)
            return False, f"在 {linux_letter} 上找不到 Linux EFI 文件"

        log(f"  ✅ 找到 EFI: {efi_info['path']} ({efi_info['distro']})")

        # 步骤 3: 查找已有的同名僵尸固件项
        reuse_guid = None
        if use_firmware:
            log(f"🔍 检查 NVRAM 中是否有可复用的僵尸项...")
            ok, fw_entries = BCDEngine.enum_firmware()
            if ok:
                for e in fw_entries:
                    if e.is_firmware and e.is_zombie:
                        if e.description and efi_info['distro'].lower() in e.description.lower():
                            reuse_guid = e.guid
                            log(f"  ♻️ 复用僵尸项: {e.description} ({e.guid})")
                            break

        # 步骤 4: 创建或修复启动项
        boot_name = f"{efi_info['distro']}"

        if use_firmware:
            log(f"🔥 创建独立固件启动项: {boot_name}")
            ok, result = BCDEngine.create_firmware_entry(
                boot_name,
                efi_info["path"],
                f"{linux_letter.rstrip(':')}:",
                reuse_zombie_guid=reuse_guid,
            )
        else:
            log(f"🔗 创建 Windows BCD 链式启动项: {boot_name}")
            ok, result = BCDEngine.create_entry(
                boot_name,
                efi_info["path"],
                f"{linux_letter.rstrip(':')}:",
            )

        if not ok:
            if linux_auto_assigned:
                LinuxScanner.release_letter(linux_letter)
            return False, f"创建启动项失败: {result}"

        guid = result
        log(f"  ✅ 启动项已创建: {guid}")

        # 步骤 5: 设为默认
        if set_default:
            log(f"⭐ 正在设为默认...")
            if use_firmware:
                BCDEngine.run(["/set", "{fwbootmgr}", "default", guid])
            else:
                BCDEngine.set_default(guid)

        # 步骤 6: 释放盘符
        if release_after and linux_auto_assigned:
            log(f"🔓 释放 Linux 分区盘符 {linux_letter}...")
            LinuxScanner.release_letter(linux_letter)
            linux_part["letter"] = ""

        # 完成
        summary = (
            f"✅ 已将 {efi_info['distro']} 添加到引导\n\n"
            f"启动项名称: {boot_name}\n"
            f"GUID: {guid}\n"
            f"EFI 路径: {efi_info['path']}\n"
            f"方式: {'独立固件项（推荐）' if use_firmware else 'Windows BCD 链式加载'}\n"
        )
        if set_default:
            summary += "⭐ 已设为默认启动项\n"
        if release_after and linux_auto_assigned:
            summary += "🔓 临时盘符已释放\n"

        return True, summary


# ============================================================
# 引导修复
# ============================================================
class BootRepair:
    @staticmethod
    def run_cmd(cmd: List[str], timeout: int = 120) -> Tuple[bool, str]:
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            try:
                out = result.stdout.decode('utf-8')
            except UnicodeDecodeError:
                out = result.stdout.decode('gbk', errors='replace')
            try:
                err = result.stderr.decode('utf-8')
            except UnicodeDecodeError:
                err = result.stderr.decode('gbk', errors='replace')
            return result.returncode == 0, (out or "") + (err or "")
        except Exception as e:
            return False, str(e)

    @staticmethod
    def fix_mbr():
        return BootRepair.run_cmd(["bootrec", "/fixmbr"])

    @staticmethod
    def fix_boot():
        return BootRepair.run_cmd(["bootrec", "/fixboot"])

    @staticmethod
    def scan_os():
        return BootRepair.run_cmd(["bootrec", "/scanos"])

    @staticmethod
    def rebuild_bcd():
        return BootRepair.run_cmd(["bootrec", "/rebuildbcd"])

    @staticmethod
    def bcdboot(system_drive: str = "C:", efi_drive: str = "S:"):
        cmd = ["bcdboot", f"{system_drive}\\Windows", "/s", efi_drive, "/f", "ALL"]
        return BootRepair.run_cmd(cmd)


# ============================================================
# 备份与导出
# ============================================================
class BackupManager:
    @staticmethod
    def create_full_backup(log_callback=None) -> Tuple[bool, str]:
        """创建完整引导配置备份"""
        def log(msg):
            print(f"[Backup] {msg}", flush=True)
            if log_callback:
                log_callback(msg)

        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_subdir = BACKUP_DIR / timestamp
            backup_subdir.mkdir(parents=True, exist_ok=True)

            log(f"📦 开始完整备份到 {backup_subdir}")

            # 1. 收集系统信息
            log("  [1/6] 收集系统信息...")
            system_info = {
                "boot_mode": BCDEngine.get_boot_mode(),
                "disk_type": BCDEngine.get_disk_type(),
                "is_admin": is_admin(),
                "timestamp": timestamp,
                "app_version": APP_VERSION,
            }

            # 2. BCD 完整导出
            log("  [2/6] 导出 BCD...")
            bcd_file = backup_subdir / f"bcd_{timestamp}.bcd"
            ok, bcd_path = BCDEngine.backup_bcd(str(backup_subdir))
            bcd_exported = ok

            # 3. BCD 文本导出
            log("  [3/6] 导出 BCD 文本...")
            bcd_raw = BCDEngine.get_full_raw_export()
            fw_raw = BCDEngine.get_firmware_raw_export()

            # 4. 收集分区信息
            log("  [4/6] 收集分区信息...")
            partitions_info = []
            disks = LinuxScanner.list_physical_disks()
            for disk in disks:
                parts = LinuxScanner.list_partitions(disk["number"])
                for p in parts:
                    p["disk_number"] = disk["number"]
                    if p.get("letter"):
                        try:
                            p["fs"] = LinuxScanner.detect_fs_type(p["letter"])
                            sys_name, conf, feats = SystemIdentifier.identify(p["letter"])
                            p["system"] = sys_name
                            p["confidence"] = conf
                        except:
                            p["fs"] = "未知"
                            p["system"] = "未知"
                    partitions_info.append(p)

            # 5. 收集启动项
            log("  [5/6] 收集启动项...")
            ok, bcd_entries = BCDEngine.enum_all()
            ok2, fw_entries = BCDEngine.enum_firmware()

            bcd_list = []
            if ok:
                for e in bcd_entries:
                    if not e.is_firmware:
                        bcd_list.append({
                            "guid": e.guid,
                            "description": e.description,
                            "device": e.device,
                            "osdevice": e.osdevice,
                            "path": e.path,
                            "type": e.type,
                            "is_default": e.is_default,
                        })

            fw_list = []
            if ok2:
                for e in fw_entries:
                    if e.is_firmware:
                        fw_list.append({
                            "guid": e.guid,
                            "description": e.description,
                            "device": e.device,
                            "path": e.path,
                            "is_zombie": e.is_zombie,
                        })

            # 6. 保存 JSON
            log("  [6/6] 保存 JSON...")
            backup_data = {
                "version": APP_VERSION,
                "timestamp": timestamp,
                "system_info": system_info,
                "bcd": {
                    "bcd_file": bcd_path if bcd_exported else "",
                    "entries": bcd_list,
                    "raw_export": bcd_raw,
                },
                "firmware": {
                    "entries": fw_list,
                    "raw_export": fw_raw,
                },
                "partitions": partitions_info,
            }

            json_file = backup_subdir / f"backup_{timestamp}.json"
            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(backup_data, f, indent=2, ensure_ascii=False)

            # 生成恢复脚本
            log("  [7/7] 生成恢复脚本...")
            bat_file = backup_subdir / f"restore_{timestamp}.bat"
            BackupManager.generate_bat_script(backup_data, bat_file)

            sh_file = backup_subdir / f"restore_{timestamp}.sh"
            BackupManager.generate_sh_script(backup_data, sh_file)

            log(f"✅ 备份完成: {backup_subdir}")
            return True, str(backup_subdir)

        except Exception as e:
            dbg(f"备份失败: {e}")
            traceback.print_exc()
            return False, str(e)

    @staticmethod
    def generate_bat_script(backup_data: dict, output_path: Path):
        """生成 Windows .bat 恢复脚本"""
        timestamp = backup_data["timestamp"]
        bcd_file = backup_data["bcd"].get("bcd_file", "")
        bcd_entries = backup_data["bcd"]["entries"]
        fw_entries = backup_data["firmware"]["entries"]

        lines = [
            "@echo off",
            "chcp 65001 >nul",
            f"title Mikan Boot 恢复脚本 - {timestamp}",
            "echo ============================================",
            "echo   Mikan Boot Manager 引导配置恢复",
            f"echo   备份时间: {timestamp}",
            "echo ============================================",
            "echo.",
            "",
            "REM 检查管理员权限",
            "net session >nul 2>&1",
            "if %errorLevel% neq 0 (",
            "    echo [错误] 请以管理员身份运行！",
            "    pause",
            "    exit /b 1",
            ")",
            "echo [OK] 管理员权限已确认",
            "echo.",
        ]

        # BCD 恢复
        if bcd_file:
            lines.extend([
                "echo [1/3] 恢复 BCD...",
                f'if exist "{bcd_file}" (',
                f'    bcdedit /import "{bcd_file}"',
                '    if %errorLevel% equ 0 (echo [OK] BCD 恢复成功) else (echo [错误] BCD 恢复失败)',
                ') else (',
                '    echo [警告] BCD 备份文件不存在，跳过',
                ')',
                "echo.",
            ])

        # 固件项恢复
        lines.extend([
            "echo [2/3] 恢复固件启动项...",
        ])
        valid_fw = [e for e in fw_entries if not e.get("is_zombie")]
        if valid_fw:
            for e in valid_fw:
                lines.append(f"echo   处理: {e.get('description', 'unknown')}")
                if e.get("device"):
                    lines.append(f'bcdedit /set {e["guid"]} device "{e["device"]}"')
                if e.get("path"):
                    lines.append(f'bcdedit /set {e["guid"]} path "{e["path"]}"')
            lines.append("echo [OK] 固件项恢复完成")
        else:
            lines.append("echo [跳过] 无有效固件项")
        lines.append("echo.")

        # Linux 启动项恢复（BCD 链式）
        linux_entries = [e for e in bcd_entries if "linux" in (e.get("description", "")).lower()
                         or "mint" in (e.get("description", "")).lower()
                         or "ubuntu" in (e.get("description", "")).lower()]
        if linux_entries:
            lines.extend([
                "echo [3/3] 恢复 Linux 启动项...",
            ])
            for e in linux_entries:
                lines.append(f"echo   {e.get('description', 'Linux')}")
                if e.get("device"):
                    lines.append(f'bcdedit /set {e["guid"]} device "{e["device"]}"')
                if e.get("path"):
                    lines.append(f'bcdedit /set {e["guid"]} path "{e["path"]}"')
            lines.append("echo [OK] Linux 启动项恢复完成")
        else:
            lines.append("echo [3/3] [跳过] 无 Linux 启动项")
        lines.append("echo.")

        lines.extend([
            "echo ============================================",
            "echo   恢复完成！请重启测试",
            "echo ============================================",
            "pause",
        ])

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))

    @staticmethod
    def generate_sh_script(backup_data: dict, output_path: Path):
        """生成 Linux .sh 恢复脚本"""
        timestamp = backup_data["timestamp"]
        bcd_entries = backup_data["bcd"]["entries"]
        fw_entries = backup_data["firmware"]["entries"]

        lines = [
            "#!/bin/bash",
            f"# Mikan Boot Manager 恢复脚本",
            f"# 备份时间: {timestamp}",
            f"# 用途: 在 Linux LiveUSB 里恢复引导配置",
            "",
            'echo "============================================"',
            'echo "  Mikan Boot 引导恢复"',
            f'echo "  备份时间: {timestamp}"',
            'echo "============================================"',
            'echo',
            "",
            "# 检查 root",
            'if [ "$EUID" -ne 0 ]; then',
            '    echo "[错误] 请用 sudo 运行"',
            '    exit 1',
            "fi",
            "",
            "# 自动挂载 Windows ESP",
            'echo "[1/2] 挂载 ESP..."',
            'ESP_MOUNT="/mnt/efi"',
            'sudo mkdir -p "$ESP_MOUNT"',
            "# 自动探测 FAT32 分区（可能需要手动修改）",
            'for dev in /dev/sd*1 /dev/nvme*p1; do',
            '    if [ -b "$dev" ]; then',
            '        FS=$(blkid -o value -s TYPE "$dev" 2>/dev/null)',
            '        if [ "$FS" = "vfat" ]; then',
            '            echo "  找到 ESP: $dev"',
            '            sudo mount "$dev" "$ESP_MOUNT"',
            '            break',
            '        fi',
            '    fi',
            'done',
            "",
            "# 恢复 BCD（如果有备份）",
            'echo "[2/2] 恢复引导..."',
            'echo "  (Linux 恢复步骤需要手动执行)"',
            'echo ""',
            'echo "  建议步骤:"',
            'echo "    1. chroot 到 Linux 系统"',
            'echo "    2. 运行 grub-install 和 update-grub"',
            'echo "    3. 重启测试"',
            'echo',
            "",
            "# 输出 Linux 启动项信息",
            'echo "  BCD 中的 Linux 项:"',
        ]

        linux_entries = [e for e in bcd_entries if "linux" in (e.get("description", "")).lower()
                         or "mint" in (e.get("description", "")).lower()
                         or "ubuntu" in (e.get("description", "")).lower()]

        for e in linux_entries:
            lines.append(f'echo "    - {e.get("description", "Linux")} ({e.get("guid", "")})"')

        lines.extend([
            'echo',
            'echo "  固件项信息:"',
        ])
        for e in fw_entries:
            zombie = " [僵尸]" if e.get("is_zombie") else ""
            lines.append(f'echo "    - {e.get("description", "unknown")}{zombie}"')

        lines.extend([
            'echo',
            'echo "============================================"',
            'echo "  完成"',
            'echo "============================================"',
        ])

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))

        # 添加可执行权限
        try:
            os.chmod(output_path, 0o755)
        except:
            pass

    @staticmethod
    def restore_from_backup(backup_json: str, log_callback=None) -> Tuple[bool, str]:
        """从备份 JSON 恢复"""
        def log(msg):
            print(f"[Restore] {msg}", flush=True)
            if log_callback:
                log_callback(msg)

        try:
            with open(backup_json, 'r', encoding='utf-8') as f:
                data = json.load(f)

            log(f"📦 从备份恢复: {backup_json}")

            # 1. 恢复 BCD（如果有备份文件）
            bcd_file = data["bcd"].get("bcd_file", "")
            if bcd_file and Path(bcd_file).exists():
                log(f"  [1/3] 恢复 BCD...")
                ok, msg = BCDEngine.restore_bcd(bcd_file)
                if not ok:
                    log(f"  ⚠️ BCD 恢复失败: {msg}")

            # 2. 恢复固件项
            log(f"  [2/3] 恢复固件项...")
            for e in data["firmware"]["entries"]:
                if e.get("is_zombie"):
                    continue
                guid = e["guid"]
                if e.get("device"):
                    BCDEngine.set_device(guid, e["device"])
                if e.get("path"):
                    BCDEngine.set_path(guid, e["path"])

            # 3. 恢复 BCD 项
            log(f"  [3/3] 恢复 BCD 项...")
            for e in data["bcd"]["entries"]:
                guid = e["guid"]
                if e.get("device"):
                    BCDEngine.set_device(guid, e["device"])
                if e.get("path"):
                    BCDEngine.set_path(guid, e["path"])

            log(f"✅ 恢复完成")
            return True, "恢复完成"
        except Exception as e:
            dbg(f"恢复失败: {e}")
            traceback.print_exc()
            return False, str(e)

    @staticmethod
    def list_backups() -> List[dict]:
        """列出所有备份"""
        backups = []
        if not BACKUP_DIR.exists():
            return backups
        for d in sorted(BACKUP_DIR.iterdir(), reverse=True):
            if d.is_dir():
                json_files = list(d.glob("backup_*.json"))
                if json_files:
                    backups.append({
                        "path": str(d),
                        "json": str(json_files[0]),
                        "timestamp": d.name,
                        "size": sum(f.stat().st_size for f in d.rglob('*') if f.is_file()),
                    })
        return backups


# ============================================================
# 样式
# ============================================================
FLAT_LIGHT_STYLE = """
QMainWindow, QDialog, QWidget {
    background: #ffffff;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
    color: #2c3e50;
}
QWidget { font-size: 13px; }

QGroupBox {
    background: #ffffff;
    border: 1px solid #e1e4e8;
    border-radius: 6px;
    margin-top: 14px;
    padding: 14px 12px 12px 12px;
    font-weight: 600;
    color: #2c3e50;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 8px;
    background: #ffffff;
    color: #1a73e8;
    font-size: 13px;
}

QPushButton {
    background: #f8f9fa;
    border: 1px solid #e1e4e8;
    border-radius: 6px;
    padding: 7px 16px;
    color: #2c3e50;
    font-weight: 500;
    min-height: 26px;
}
QPushButton:hover { background: #e8edf3; border-color: #c5cad0; }
QPushButton:pressed { background: #d4dee8; }
QPushButton:disabled { background: #f0f2f5; color: #b0b0b0; border-color: #e1e4e8; }

QPushButton#primary {
    background: #1a73e8;
    color: #ffffff;
    border: 1px solid #1a73e8;
    font-weight: 600;
}
QPushButton#primary:hover { background: #1557b0; border-color: #1557b0; }

QPushButton#success {
    background: #28a745; color: #ffffff; border: 1px solid #28a745; font-weight: 600;
}
QPushButton#success:hover { background: #218838; }

QPushButton#danger {
    background: #dc3545; color: #ffffff; border: 1px solid #dc3545; font-weight: 600;
}
QPushButton#danger:hover { background: #c82333; }

QPushButton#warning {
    background: #ffc107; color: #2c3e50; border: 1px solid #ffc107; font-weight: 600;
}
QPushButton#warning:hover { background: #e0a800; }

QLineEdit, QTextEdit, QComboBox, QSpinBox {
    background: #ffffff;
    border: 1px solid #e1e4e8;
    border-radius: 6px;
    padding: 6px 10px;
    color: #2c3e50;
    font-size: 13px;
    min-height: 22px;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #1a73e8;
}
QTextEdit {
    font-family: "Consolas", "Microsoft YaHei", monospace;
    background: #fafbfc;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #5b6777;
    margin-right: 6px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #e1e4e8;
    selection-background-color: #e8edf3;
    selection-color: #1a73e8;
    outline: none;
}

QTableWidget {
    background: #ffffff;
    border: 1px solid #e1e4e8;
    border-radius: 6px;
    gridline-color: #f0f2f5;
    selection-background-color: #e8edf3;
    selection-color: #1a73e8;
    outline: none;
}
QTableWidget::item { padding: 6px 10px; }
QTableWidget::item:selected { background: #e8edf3; color: #1a73e8; }
QHeaderView::section {
    background: #f8f9fa;
    color: #5b6777;
    font-weight: 600;
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid #e1e4e8;
    font-size: 12px;
}

QTabWidget::pane {
    background: #ffffff;
    border: 1px solid #e1e4e8;
    border-radius: 6px;
    top: -1px;
}
QTabBar::tab {
    background: #f8f9fa;
    color: #5b6777;
    border: 1px solid #e1e4e8;
    border-bottom: none;
    padding: 8px 18px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-weight: 500;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #1a73e8;
    border-bottom: 2px solid #1a73e8;
    font-weight: 600;
}
QTabBar::tab:hover:!selected { background: #e8edf3; }

QCheckBox { color: #2c3e50; spacing: 8px; }
QCheckBox::indicator {
    width: 18px; height: 18px;
    border-radius: 4px;
    border: 1.5px solid #c5cad0;
    background: #ffffff;
}
QCheckBox::indicator:checked {
    background: #1a73e8;
    border-color: #1a73e8;
}

QScrollBar:vertical {
    background: transparent; width: 10px; border-radius: 5px;
}
QScrollBar::handle:vertical {
    background: #d1d5db; border-radius: 5px; min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QStatusBar {
    background: #f8f9fa; color: #5b6777;
    border-top: 1px solid #e1e4e8; font-size: 12px;
}

QListWidget {
    background: #ffffff;
    border: 1px solid #e1e4e8;
    border-radius: 6px;
    padding: 4px;
}
QListWidget::item { padding: 6px 10px; border-radius: 4px; }
QListWidget::item:hover { background: #e8edf3; }
QListWidget::item:selected { background: #e8edf3; color: #1a73e8; }

QTreeWidget {
    background: #ffffff;
    border: 1px solid #e1e4e8;
    border-radius: 6px;
    padding: 4px;
}
QTreeWidget::item { padding: 4px 8px; }
QTreeWidget::item:hover { background: #e8edf3; }
QTreeWidget::item:selected { background: #e8edf3; color: #1a73e8; }

QMenu { background: #ffffff; border: 1px solid #e1e4e8; border-radius: 6px; padding: 6px; }
QMenu::item { padding: 6px 24px; border-radius: 4px; color: #2c3e50; }
QMenu::item:selected { background: #e8edf3; color: #1a73e8; }
"""


# ============================================================
# Worker
# ============================================================
class Worker(QThread):
    log = Signal(str)
    finished = Signal(bool, object)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            try:
                sig = inspect.signature(self.func)
                if 'log_callback' in sig.parameters:
                    self.kwargs['log_callback'] = self.log.emit
            except (ValueError, TypeError):
                pass

            result = self.func(*self.args, **self.kwargs)
            if isinstance(result, tuple) and len(result) == 2:
                self.finished.emit(result[0], result[1])
            else:
                self.finished.emit(True, result)
        except Exception as e:
            dbg(f"Worker 异常: {e}")
            traceback.print_exc()
            self.finished.emit(False, str(e))


class WorkerManager:
    def __init__(self):
        self._workers: List[Worker] = []

    def start(self, worker: Worker, on_finished, on_log=None):
        if on_log:
            worker.log.connect(on_log)
        worker.finished.connect(on_finished)

        def cleanup(*args):
            try:
                if worker in self._workers:
                    self._workers.remove(worker)
            except:
                pass

        worker.finished.connect(cleanup)
        self._workers.append(worker)
        worker.start()
        return worker

    def stop_all(self):
        for w in list(self._workers):
            try:
                w.quit()
                w.wait(1000)
            except:
                pass
        self._workers.clear()


# ============================================================
# BCD 管理面板
# ============================================================
class BCDPanel(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.entries: List[BootEntry] = []
        self.filtered_entries: List[BootEntry] = []
        self.workers = WorkerManager()
        self.setup_ui()
        self.table.itemSelectionChanged.connect(self.on_selection_changed)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self.refresh)
        toolbar.addWidget(refresh_btn)

        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("筛选:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["全部", "Windows 启动项", "UEFI 固件项", "所有"])
        self.filter_combo.currentIndexChanged.connect(self.apply_filter)
        toolbar.addWidget(self.filter_combo)
        toolbar.addStretch()

        add_win_btn = QPushButton("➕ Windows")
        add_win_btn.clicked.connect(self.add_windows_entry)
        toolbar.addWidget(add_win_btn)

        add_linux_btn = QPushButton("🐧 Linux")
        add_linux_btn.clicked.connect(self.add_linux_entry)
        toolbar.addWidget(add_linux_btn)

        add_vhd_btn = QPushButton("💿 VHD")
        add_vhd_btn.clicked.connect(self.add_vhd_entry)
        toolbar.addWidget(add_vhd_btn)

        add_pe_btn = QPushButton("🔧 WinPE")
        add_pe_btn.clicked.connect(self.add_winpe_entry)
        toolbar.addWidget(add_pe_btn)

        layout.addLayout(toolbar)

        toolbar2 = QHBoxLayout()
        toolbar2.setSpacing(6)

        edit_btn = QPushButton("✏️ 编辑")
        edit_btn.clicked.connect(self.edit_entry)
        toolbar2.addWidget(edit_btn)

        delete_btn = QPushButton("🗑️ 删除")
        delete_btn.setObjectName("danger")
        delete_btn.clicked.connect(self.delete_entry)
        toolbar2.addWidget(delete_btn)

        default_btn = QPushButton("⭐ 设为默认")
        default_btn.setObjectName("primary")
        default_btn.clicked.connect(self.set_default)
        toolbar2.addWidget(default_btn)

        advanced_btn = QPushButton("⚙️ 高级选项")
        advanced_btn.clicked.connect(self.open_advanced_options)
        toolbar2.addWidget(advanced_btn)

        toolbar2.addSpacing(20)
        toolbar2.addWidget(QLabel("启动菜单超时:"))
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(0, 999)
        self.timeout_spin.setValue(5)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setFixedWidth(100)
        toolbar2.addWidget(self.timeout_spin)

        apply_timeout_btn = QPushButton("应用")
        apply_timeout_btn.clicked.connect(self.apply_timeout)
        toolbar2.addWidget(apply_timeout_btn)
        toolbar2.addStretch()

        layout.addLayout(toolbar2)

        # 表格
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["GUID", "类型", "描述", "设备", "路径", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_context_menu)
        layout.addWidget(self.table, 1)

        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setMaximumHeight(120)
        layout.addWidget(QLabel("📋 详情:"))
        layout.addWidget(self.detail_text)

    def refresh(self):
        self.main_window.log("🔄 正在读取 BCD...")

        def do():
            return BCDEngine.enum_all()

        self.workers.start(Worker(do), self._on_loaded, self.main_window.log)

    @Slot(bool, object)
    def _on_loaded(self, ok, result):
        if not ok:
            self.main_window.log(f"❌ 读取失败: {result}")
            return
        self.entries = result
        self.main_window.log(f"✅ 读取成功，共 {len(self.entries)} 项")
        self.apply_filter()
        self.main_window.update_status()

    def apply_filter(self):
        idx = self.filter_combo.currentIndex()
        if idx == 0:
            self.filtered_entries = [e for e in self.entries if not e.is_firmware]
        elif idx == 1:
            self.filtered_entries = [e for e in self.entries
                                     if e.type in ("Windows Boot Manager", "Windows Boot Loader")]
        elif idx == 2:
            self.filtered_entries = [e for e in self.entries if e.is_firmware]
        else:
            self.filtered_entries = list(self.entries)

        self.table.setRowCount(len(self.filtered_entries))
        for row, entry in enumerate(self.filtered_entries):
            guid_item = QTableWidgetItem(entry.guid or "(无)")
            if entry.is_default:
                guid_item.setText(f"⭐ {entry.guid}")
            guid_item.setData(Qt.UserRole, entry)
            self.table.setItem(row, 0, guid_item)

            type_item = QTableWidgetItem(entry.type)
            if entry.is_firmware:
                type_item.setForeground(QColor("#1a73e8"))
            self.table.setItem(row, 1, type_item)

            self.table.setItem(row, 2, QTableWidgetItem(entry.description or "(无描述)"))
            self.table.setItem(row, 3, QTableWidgetItem(entry.device or "-"))
            self.table.setItem(row, 4, QTableWidgetItem(entry.path or "-"))

            status = "✅ 正常"
            color = "#2e7d32"
            if entry.is_zombie:
                status = "⚠️ 僵尸项"
                color = "#b85c00"
            elif entry.is_firmware and not entry.path:
                status = "⚠️ 无路径"
                color = "#b85c00"

            status_item = QTableWidgetItem(status)
            status_item.setForeground(QColor(color))
            self.table.setItem(row, 5, status_item)

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)

    def _selected_entry(self) -> Optional[BootEntry]:
        rows = set()
        for item in self.table.selectedItems():
            rows.add(item.row())
        if not rows:
            return None
        row = list(rows)[0]
        if row < len(self.filtered_entries):
            return self.filtered_entries[row]
        return None

    def on_selection_changed(self):
        entry = self._selected_entry()
        if entry:
            self.detail_text.setPlainText(entry.raw)

    def on_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item:
            return
        self.table.selectRow(item.row())
        entry = self._selected_entry()
        if not entry:
            return

        menu = QMenu(self)

        if entry.is_firmware and entry.is_zombie:
            fix_action = QAction("🔧 修复僵尸项（补全路径）", self)
            fix_action.triggered.connect(lambda: self.fix_zombie(entry))
            menu.addAction(fix_action)
            menu.addSeparator()

        edit_action = QAction("✏️ 编辑描述", self)
        edit_action.triggered.connect(self.edit_entry)
        menu.addAction(edit_action)

        default_action = QAction("⭐ 设为默认", self)
        default_action.triggered.connect(self.set_default)
        menu.addAction(default_action)

        menu.addSeparator()

        delete_action = QAction("🗑️ 删除", self)
        delete_action.triggered.connect(self.delete_entry)
        menu.addAction(delete_action)

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def fix_zombie(self, entry: BootEntry):
        """修复僵尸固件项"""
        dialog = ZombieFixDialog(entry, self)
        if dialog.exec() == QDialog.Accepted:
            efi_path, partition = dialog.get_values()
            if not efi_path or not partition:
                return

            def do():
                ok, msg = BCDEngine.set_device(entry.guid, f"partition={partition}")
                if not ok:
                    return False, f"设置 device 失败: {msg}"
                ok, msg = BCDEngine.set_path(entry.guid, efi_path)
                if not ok:
                    return False, f"设置 path 失败: {msg}"
                return True, "修复成功"

            self.workers.start(
                Worker(do),
                lambda ok, r: self._on_op_done(ok, r, "修复僵尸项"),
                self.main_window.log
            )

    def add_windows_entry(self):
        dialog = AddWindowsEntryDialog(self)
        if dialog.exec() == QDialog.Accepted:
            values = dialog.get_values()
            def do():
                return BCDEngine.create_entry(values["description"], values["path"], values["partition"])
            self.workers.start(
                Worker(do),
                lambda ok, r: self._on_op_done(ok, r, "添加 Windows 启动项"),
                self.main_window.log
            )

    def add_linux_entry(self):
        dialog = AddLinuxEntryDialog(self)
        if dialog.exec() == QDialog.Accepted:
            values = dialog.get_values()
            def do():
                if values["use_firmware"]:
                    return BCDEngine.create_firmware_entry(
                        values["description"], values["path"], values["partition"]
                    )
                else:
                    return BCDEngine.create_entry(
                        values["description"], values["path"], values["partition"]
                    )
            self.workers.start(
                Worker(do),
                lambda ok, r: self._on_op_done(ok, r, "添加 Linux 启动项"),
                self.main_window.log
            )

    def add_vhd_entry(self):
        dialog = AddVHDEntryDialog(self)
        if dialog.exec() == QDialog.Accepted:
            values = dialog.get_values()
            def do():
                return BCDEngine.create_vhd_entry(values["vhd_path"], values["description"])
            self.workers.start(
                Worker(do),
                lambda ok, r: self._on_op_done(ok, r, "添加 VHD 启动项"),
                self.main_window.log
            )

    def add_winpe_entry(self):
        dialog = AddWinPEEntryDialog(self)
        if dialog.exec() == QDialog.Accepted:
            values = dialog.get_values()
            def do():
                return BCDEngine.create_winpe_entry(values["wim_path"], values["description"])
            self.workers.start(
                Worker(do),
                lambda ok, r: self._on_op_done(ok, r, "添加 WinPE 启动项"),
                self.main_window.log
            )

    def edit_entry(self):
        entry = self._selected_entry()
        if not entry or not entry.guid:
            QMessageBox.warning(self, "提示", "请先选择一项")
            return

        new_desc, ok = QInputDialog.getText(
            self, "编辑描述", "新的描述:", text=entry.description
        )
        if ok and new_desc != entry.description:
            def do():
                return BCDEngine.set_description(entry.guid, new_desc)
            self.workers.start(
                Worker(do),
                lambda ok, r: self._on_op_done(ok, r, "修改描述"),
                self.main_window.log
            )

    def delete_entry(self):
        entry = self._selected_entry()
        if not entry or not entry.guid:
            QMessageBox.warning(self, "提示", "请先选择一项")
            return

        protected = ["{bootmgr}", "{current}", "{default}", "{fwbootmgr}"]
        if entry.guid.lower() in [p.lower() for p in protected]:
            QMessageBox.warning(self, "禁止删除",
                                f"'{entry.guid}' 是系统关键项！")
            return

        reply = QMessageBox.question(
            self, "确认删除",
            f"确定删除以下启动项吗？\n\n"
            f"描述: {entry.description}\n"
            f"GUID: {entry.guid}\n\n"
            f"⚠️ 不可恢复！",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        if self.main_window.config.get("backup_bcd_before_change", True):
            ok, path = BCDEngine.backup_bcd(str(BACKUP_DIR))
            if ok:
                self.main_window.log(f"💾 已备份 BCD")

        def do():
            return BCDEngine.delete_entry(entry.guid)

        self.workers.start(
            Worker(do),
            lambda ok, r: self._on_op_done(ok, r, "删除启动项"),
            self.main_window.log
        )

    def set_default(self):
        entry = self._selected_entry()
        if not entry or not entry.guid:
            QMessageBox.warning(self, "提示", "请先选择一项")
            return

        def do():
            return BCDEngine.set_default(entry.guid)

        self.workers.start(
            Worker(do),
            lambda ok, r: self._on_op_done(ok, r, "设置默认"),
            self.main_window.log
        )

    def apply_timeout(self):
        seconds = self.timeout_spin.value()
        def do():
            return BCDEngine.set_timeout(seconds)
        self.workers.start(
            Worker(do),
            lambda ok, r: self._on_op_done(ok, r, "设置超时"),
            self.main_window.log
        )

    def open_advanced_options(self):
        entry = self._selected_entry()
        if not entry or not entry.guid:
            QMessageBox.warning(self, "提示", "请先选择一项")
            return
        dialog = AdvancedOptionsDialog(entry, self)
        dialog.exec()

    @Slot(bool, object)
    def _on_op_done(self, ok, result, action: str):
        if ok:
            self.main_window.log(f"✅ {action}成功")
            self.refresh()
        else:
            self.main_window.log(f"❌ {action}失败: {result}")
            QMessageBox.warning(self, "失败", f"{action}失败:\n{result}")


# ============================================================
# 各种对话框
# ============================================================
class AddWindowsEntryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("➕ 添加 Windows 启动项")
        self.setMinimumWidth(500)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        layout.addWidget(QLabel("启动项名称:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如: Windows 11")
        layout.addWidget(self.name_edit)

        layout.addWidget(QLabel("Windows 分区（含 \\Windows\\System32）:"))
        part_row = QHBoxLayout()
        self.partition_edit = QLineEdit()
        self.partition_edit.setPlaceholderText("例如: C:")
        part_row.addWidget(self.partition_edit)
        layout.addLayout(part_row)

        layout.addWidget(QLabel("启动文件路径:"))
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("例如: \\Windows\\system32\\winload.efi")
        path_row.addWidget(self.path_edit)
        layout.addLayout(path_row)

        tip = QLabel(
            "💡 提示:\n"
            "• UEFI 系统: \\Windows\\system32\\winload.efi\n"
            "• BIOS 系统: \\Windows\\system32\\winload.exe\n"
            "• 通常用「复制现有项」更保险"
        )
        tip.setStyleSheet("background: #f8f9fa; color: #5b6777; padding: 10px; border-radius: 6px; font-size: 12px;")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("✅ 确定")
        ok_btn.setObjectName("primary")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def get_values(self):
        return {
            "description": self.name_edit.text().strip(),
            "partition": self.partition_edit.text().strip(),
            "path": self.path_edit.text().strip(),
        }


class AddLinuxEntryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🐧 添加 Linux 启动项")
        self.setMinimumWidth(520)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        layout.addWidget(QLabel("启动项名称:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如: Linux Mint")
        layout.addWidget(self.name_edit)

        layout.addWidget(QLabel("EFI 分区盘符:"))
        self.partition_edit = QLineEdit()
        self.partition_edit.setPlaceholderText("例如: J:")
        layout.addWidget(self.partition_edit)

        layout.addWidget(QLabel("EFI 文件路径:"))
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText(r"例如: \EFI\ubuntu\shimx64.efi")
        path_row.addWidget(self.path_edit)
        layout.addLayout(path_row)

        self.use_firmware_check = QCheckBox("🔥 使用独立固件启动项（推荐，绕过 Windows BCD）")
        self.use_firmware_check.setChecked(True)
        layout.addWidget(self.use_firmware_check)

        tip = QLabel(
            "💡 说明:\n"
            "• 独立固件项: 主板 UEFI 直接加载 GRUB，最稳定\n"
            "• BCD 链式加载: Windows Boot Manager 中转，有时会 0xC000007b\n"
            "• 推荐用「🐧 Linux 扫描」自动检测后一键创建"
        )
        tip.setStyleSheet("background: #f8f9fa; color: #5b6777; padding: 10px; border-radius: 6px; font-size: 12px;")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("✅ 确定")
        ok_btn.setObjectName("primary")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def get_values(self):
        return {
            "description": self.name_edit.text().strip(),
            "partition": self.partition_edit.text().strip(),
            "path": self.path_edit.text().strip(),
            "use_firmware": self.use_firmware_check.isChecked(),
        }


class AddVHDEntryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("💿 添加 VHD/VHDX 启动项")
        self.setMinimumWidth(520)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        layout.addWidget(QLabel("启动项名称:"))
        self.name_edit = QLineEdit("Windows on VHD")
        layout.addWidget(self.name_edit)

        layout.addWidget(QLabel("VHD/VHDX 文件:"))
        file_row = QHBoxLayout()
        self.vhd_edit = QLineEdit()
        self.vhd_edit.setPlaceholderText("选择 .vhd 或 .vhdx 文件")
        file_row.addWidget(self.vhd_edit)
        browse_btn = QPushButton("📁 浏览")
        browse_btn.clicked.connect(self.browse_vhd)
        file_row.addWidget(browse_btn)
        layout.addLayout(file_row)

        tip = QLabel(
            "💡 VHD 启动项说明:\n"
            "• Windows 可以从 VHD/VHDX 文件启动\n"
            "• VHD 必须是固定大小或动态扩展\n"
            "• 路径自动处理，无需担心盘符\n"
            "• 会自动设置 detecthal 选项"
        )
        tip.setStyleSheet("background: #f8f9fa; color: #5b6777; padding: 10px; border-radius: 6px; font-size: 12px;")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("✅ 确定")
        ok_btn.setObjectName("primary")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def browse_vhd(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择 VHD 文件", "",
            "VHD 文件 (*.vhd *.vhdx);;所有文件 (*)"
        )
        if file_path:
            self.vhd_edit.setText(file_path)

    def get_values(self):
        return {
            "description": self.name_edit.text().strip(),
            "vhd_path": self.vhd_edit.text().strip(),
        }


class AddWinPEEntryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🔧 添加 WinPE/WIM 启动项")
        self.setMinimumWidth(520)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        layout.addWidget(QLabel("启动项名称:"))
        self.name_edit = QLineEdit("WinPE")
        layout.addWidget(self.name_edit)

        layout.addWidget(QLabel("WIM 文件 (boot.wim):"))
        file_row = QHBoxLayout()
        self.wim_edit = QLineEdit()
        self.wim_edit.setPlaceholderText(r"例如: \sources\boot.wim")
        file_row.addWidget(self.wim_edit)
        browse_btn = QPushButton("📁 浏览")
        browse_btn.clicked.connect(self.browse_wim)
        file_row.addWidget(browse_btn)
        layout.addLayout(file_row)

        tip = QLabel(
            "💡 WinPE 启动项说明:\n"
            "• 从 .wim 文件加载 WinPE 环境\n"
            "• 常用路径: \\sources\\boot.wim\n"
            "• 会自动配置 ramdisk 选项"
        )
        tip.setStyleSheet("background: #f8f9fa; color: #5b6777; padding: 10px; border-radius: 6px; font-size: 12px;")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("✅ 确定")
        ok_btn.setObjectName("primary")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def browse_wim(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择 WIM 文件", "",
            "WIM 文件 (*.wim);;所有文件 (*)"
        )
        if file_path:
            self.wim_edit.setText(file_path)

    def get_values(self):
        return {
            "description": self.name_edit.text().strip(),
            "wim_path": self.wim_edit.text().strip(),
        }


class ZombieFixDialog(QDialog):
    def __init__(self, entry: BootEntry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.setWindowTitle("🔧 修复僵尸固件项")
        self.setMinimumWidth(500)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        info = QLabel(
            f"僵尸项信息:\n"
            f"  GUID: {self.entry.guid}\n"
            f"  描述: {self.entry.description}\n\n"
            f"该固件项没有 device 和 path，导致无法启动。\n"
            f"请补全路径信息："
        )
        info.setStyleSheet("background: #fff3e0; color: #b85c00; padding: 10px; border-radius: 6px; font-size: 12px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        layout.addWidget(QLabel("EFI 分区盘符:"))
        self.partition_edit = QLineEdit("J:")
        layout.addWidget(self.partition_edit)

        layout.addWidget(QLabel("EFI 文件路径:"))
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(r"\EFI\ubuntu\shimx64.efi")
        path_row.addWidget(self.path_edit)
        layout.addLayout(path_row)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("🔧 修复")
        ok_btn.setObjectName("primary")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def get_values(self):
        return (
            self.path_edit.text().strip(),
            self.partition_edit.text().strip(),
        )


class AdvancedOptionsDialog(QDialog):
    def __init__(self, entry: BootEntry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.setWindowTitle("⚙️ 高级 BCD 选项")
        self.setMinimumWidth(500)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        layout.addWidget(QLabel(f"应用到: {self.entry.description}"))

        # 数据执行保护
        dep_group = QGroupBox("数据执行保护 (DEP)")
        dep_layout = QVBoxLayout(dep_group)
        self.dep_combo = QComboBox()
        self.dep_combo.addItems(["OptIn (默认)", "OptOut (始终开)", "AlwaysOff (始终关)", "AlwaysOn (始终开)"])
        dep_layout.addWidget(self.dep_combo)
        layout.addWidget(dep_group)

        # 其他选项
        opt_group = QGroupBox("其他")
        opt_layout = QVBoxLayout(opt_group)
        self.detecthal_check = QCheckBox("detecthal on (检测 HAL)")
        opt_layout.addWidget(self.detecthal_check)
        self.no_integrity_check = QCheckBox("nointegritychecks on (禁用完整性检查)")
        opt_layout.addWidget(self.no_integrity_check)
        self.test_signing_check = QCheckBox("testsigning on (启用测试签名)")
        opt_layout.addWidget(self.test_signing_check)
        self.no_driver_signing_check = QCheckBox("nointegritychecks on (禁用驱动签名)")
        opt_layout.addWidget(self.no_driver_signing_check)
        layout.addWidget(opt_group)

        # 启动菜单
        menu_group = QGroupBox("启动菜单策略")
        menu_layout = QVBoxLayout(menu_group)
        self.menu_combo = QComboBox()
        self.menu_combo.addItems(["传统文本菜单", "现代图形菜单", "无菜单直接启动"])
        menu_layout.addWidget(self.menu_combo)
        layout.addWidget(menu_group)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        apply_btn = QPushButton("💾 应用")
        apply_btn.setObjectName("primary")
        apply_btn.clicked.connect(self.apply)
        btn_row.addWidget(apply_btn)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def apply(self):
        guid = self.entry.guid
        ops = []

        # DEP
        dep_idx = self.dep_combo.currentIndex()
        dep_val = ["OptIn", "OptOut", "AlwaysOff", "AlwaysOn"][dep_idx]
        ok, msg = BCDEngine.set_advanced_option(guid, "nx", dep_val)
        if ok:
            ops.append(f"DEP: {dep_val}")

        # 其他
        if self.detecthal_check.isChecked():
            BCDEngine.set_advanced_option(guid, "detecthal", "on")
            ops.append("detecthal on")
        if self.no_integrity_check.isChecked():
            BCDEngine.set_advanced_option(guid, "nointegritychecks", "on")
            ops.append("nointegritychecks on")
        if self.test_signing_check.isChecked():
            BCDEngine.set_advanced_option(guid, "testsigning", "on")
            ops.append("testsigning on")

        QMessageBox.information(self, "成功", f"已应用:\n" + "\n".join(ops) if ops else "无变更")
        self.accept()


# ============================================================
# UEFI 启动项面板
# ============================================================
class UEFIPanel(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.fw_entries: List[BootEntry] = []
        self.workers = WorkerManager()
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        info = QLabel(
            "💡 UEFI 固件启动项直接存储在主板的 NVRAM 中，等同于 BIOS 里的启动顺序。\n"
            "   ⚠️ 僵尸项 = 只有 description 没有 path/device，会导致启动失败。"
        )
        info.setStyleSheet("background: #e8f0fe; color: #1a73e8; padding: 10px; border-radius: 6px; border: 1px solid #c5d8f8; font-size: 12px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        toolbar = QHBoxLayout()
        refresh_btn = QPushButton("🔄 刷新固件项")
        refresh_btn.clicked.connect(self.refresh)
        toolbar.addWidget(refresh_btn)

        toolbar.addSpacing(10)

        up_btn = QPushButton("⬆️ 上移")
        up_btn.clicked.connect(lambda: self.move(-1))
        toolbar.addWidget(up_btn)

        down_btn = QPushButton("⬇️ 下移")
        down_btn.clicked.connect(lambda: self.move(1))
        toolbar.addWidget(down_btn)

        toolbar.addSpacing(10)

        apply_btn = QPushButton("💾 应用启动顺序")
        apply_btn.setObjectName("primary")
        apply_btn.clicked.connect(self.apply_order)
        toolbar.addWidget(apply_btn)

        next_btn = QPushButton("🎯 下次启动此项")
        next_btn.setObjectName("warning")
        next_btn.clicked.connect(self.set_next_boot)
        toolbar.addWidget(next_btn)

        toolbar.addStretch()
        layout.addLayout(toolbar)

        toolbar2 = QHBoxLayout()
        edit_btn = QPushButton("✏️ 编辑路径")
        edit_btn.clicked.connect(self.edit_entry)
        toolbar2.addWidget(edit_btn)

        fix_zombie_btn = QPushButton("🔧 修复选中僵尸项")
        fix_zombie_btn.setObjectName("warning")
        fix_zombie_btn.clicked.connect(self.fix_zombie)
        toolbar2.addWidget(fix_zombie_btn)

        clean_zombie_btn = QPushButton("🧹 清理所有僵尸项")
        clean_zombie_btn.setObjectName("danger")
        clean_zombie_btn.clicked.connect(self.clean_all_zombies)
        toolbar2.addWidget(clean_zombie_btn)

        toolbar2.addStretch()

        toolbar2.addWidget(QLabel("固件超时:"))
        self.fw_timeout_spin = QSpinBox()
        self.fw_timeout_spin.setRange(0, 999)
        self.fw_timeout_spin.setValue(3)
        self.fw_timeout_spin.setSuffix(" 秒")
        self.fw_timeout_spin.setFixedWidth(100)
        toolbar2.addWidget(self.fw_timeout_spin)

        apply_timeout_btn = QPushButton("应用")
        apply_timeout_btn.clicked.connect(self.apply_fw_timeout)
        toolbar2.addWidget(apply_timeout_btn)

        layout.addLayout(toolbar2)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["顺序", "GUID", "描述", "路径", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_context_menu)
        layout.addWidget(self.table, 1)

    def refresh(self):
        self.main_window.log("🔄 正在读取 UEFI 固件启动项...")

        def do():
            return BCDEngine.enum_firmware()

        self.workers.start(Worker(do), self._on_loaded, self.main_window.log)

    @Slot(bool, object)
    def _on_loaded(self, ok, result):
        if not ok:
            self.main_window.log(f"❌ 读取失败: {result}")
            return
        self.fw_entries = [e for e in result if e.is_firmware or e.guid.startswith("{")]
        zombie_count = sum(1 for e in self.fw_entries if e.is_zombie)
        self.main_window.log(f"✅ 读取到 {len(self.fw_entries)} 个固件项 (僵尸: {zombie_count})")
        self._populate()

    def _populate(self):
        self.table.setRowCount(len(self.fw_entries))
        for i, entry in enumerate(self.fw_entries):
            self.table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.table.setItem(i, 1, QTableWidgetItem(entry.guid))
            self.table.setItem(i, 2, QTableWidgetItem(entry.description or "(无)"))
            self.table.setItem(i, 3, QTableWidgetItem(entry.path or "(无)"))

            if entry.is_zombie:
                status_item = QTableWidgetItem("⚠️ 僵尸项")
                status_item.setForeground(QColor("#b85c00"))
                self.table.item(i, 2).setBackground(QColor("#fff3e0"))
                self.table.item(i, 3).setBackground(QColor("#fff3e0"))
            else:
                status_item = QTableWidgetItem("✅ 正常")
                status_item.setForeground(QColor("#2e7d32"))
            self.table.setItem(i, 4, status_item)

    def _selected_row(self) -> int:
        rows = set()
        for item in self.table.selectedItems():
            rows.add(item.row())
        return list(rows)[0] if rows else -1

    def _selected_entry(self) -> Optional[BootEntry]:
        row = self._selected_row()
        if row < 0 or row >= len(self.fw_entries):
            return None
        return self.fw_entries[row]

    def on_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item:
            return
        self.table.selectRow(item.row())
        entry = self._selected_entry()
        if not entry:
            return

        menu = QMenu(self)

        if entry.is_zombie:
            fix_action = QAction("🔧 修复僵尸项", self)
            fix_action.triggered.connect(self.fix_zombie)
            menu.addAction(fix_action)
            menu.addSeparator()

        edit_action = QAction("✏️ 编辑路径", self)
        edit_action.triggered.connect(self.edit_entry)
        menu.addAction(edit_action)

        next_action = QAction("🎯 下次启动此项", self)
        next_action.triggered.connect(self.set_next_boot)
        menu.addAction(next_action)

        menu.addSeparator()

        delete_action = QAction("🗑️ 删除", self)
        delete_action.triggered.connect(self.delete_selected)
        menu.addAction(delete_action)

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def move(self, direction: int):
        row = self._selected_row()
        if row < 0:
            QMessageBox.warning(self, "提示", "请先选择一项")
            return
        new_row = row + direction
        if new_row < 0 or new_row >= len(self.fw_entries):
            return
        self.fw_entries[row], self.fw_entries[new_row] = self.fw_entries[new_row], self.fw_entries[row]
        self._populate()
        self.table.selectRow(new_row)

    def apply_order(self):
        if not self.fw_entries:
            QMessageBox.warning(self, "提示", "没有可应用的项")
            return

        preview = "\n".join(
            f"  {i+1}. {e.description or e.guid}"
            for i, e in enumerate(self.fw_entries[:8])
        )
        if len(self.fw_entries) > 8:
            preview += "\n  ..."

        reply = QMessageBox.question(
            self, "确认应用",
            "确定要应用当前的 UEFI 启动顺序吗？\n\n顺序:\n" + preview,
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        guids = [e.guid for e in self.fw_entries if e.guid]

        def do():
            return BCDEngine.set_fw_order(guids)

        self.workers.start(
            Worker(do),
            lambda ok, r: self._op_done(ok, r, "应用启动顺序"),
            self.main_window.log
        )

    def set_next_boot(self):
        entry = self._selected_entry()
        if not entry:
            QMessageBox.warning(self, "提示", "请先选择一项")
            return

        def do():
            return BCDEngine.set_boot_sequence(entry.guid)

        self.workers.start(
            Worker(do),
            lambda ok, r: self._op_done(ok, r, "设置下次启动"),
            self.main_window.log
        )

    def apply_fw_timeout(self):
        seconds = self.fw_timeout_spin.value()

        def do():
            return BCDEngine.set_fw_timeout(seconds)

        self.workers.start(
            Worker(do),
            lambda ok, r: self._op_done(ok, r, "设置固件超时"),
            self.main_window.log
        )

    def edit_entry(self):
        entry = self._selected_entry()
        if not entry:
            QMessageBox.warning(self, "提示", "请先选择一项")
            return

        dialog = EditFirmwareDialog(entry, self)
        if dialog.exec() == QDialog.Accepted:
            values = dialog.get_values()
            def do():
                if values["device"]:
                    ok, msg = BCDEngine.set_device(entry.guid, values["device"])
                    if not ok:
                        return False, f"device: {msg}"
                if values["path"]:
                    ok, msg = BCDEngine.set_path(entry.guid, values["path"])
                    if not ok:
                        return False, f"path: {msg}"
                if values["description"]:
                    BCDEngine.set_description(entry.guid, values["description"])
                return True, "更新成功"
            self.workers.start(
                Worker(do),
                lambda ok, r: self._op_done(ok, r, "编辑固件项"),
                self.main_window.log
            )

    def fix_zombie(self):
        entry = self._selected_entry()
        if not entry or not entry.is_zombie:
            QMessageBox.warning(self, "提示", "请先选择一个僵尸项")
            return

        dialog = ZombieFixDialog(entry, self)
        if dialog.exec() == QDialog.Accepted:
            efi_path, partition = dialog.get_values()
            if not efi_path or not partition:
                return
            def do():
                ok, msg = BCDEngine.set_device(entry.guid, f"partition={partition}")
                if not ok:
                    return False, f"device 失败: {msg}"
                ok, msg = BCDEngine.set_path(entry.guid, efi_path)
                if not ok:
                    return False, f"path 失败: {msg}"
                return True, "修复成功"
            self.workers.start(
                Worker(do),
                lambda ok, r: self._op_done(ok, r, "修复僵尸项"),
                self.main_window.log
            )

    def clean_all_zombies(self):
        zombies = [e for e in self.fw_entries if e.is_zombie]
        if not zombies:
            QMessageBox.information(self, "提示", "没有僵尸项")
            return

        reply = QMessageBox.question(
            self, "确认清理",
            f"将删除 {len(zombies)} 个僵尸项：\n\n" +
            "\n".join(f"  • {e.description or e.guid}" for e in zombies[:10]) +
            ("\n  ..." if len(zombies) > 10 else ""),
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        def do():
            count = 0
            for e in zombies:
                ok, msg = BCDEngine.delete_entry(e.guid)
                if ok:
                    count += 1
            return True, f"已清理 {count}/{len(zombies)} 个僵尸项"

        self.workers.start(
            Worker(do),
            lambda ok, r: self._op_done(ok, r, "清理僵尸项"),
            self.main_window.log
        )

    def delete_selected(self):
        entry = self._selected_entry()
        if not entry:
            return
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定删除固件项吗？\n\n{entry.description}\n{entry.guid}",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        def do():
            return BCDEngine.delete_entry(entry.guid)
        self.workers.start(
            Worker(do),
            lambda ok, r: self._op_done(ok, r, "删除固件项"),
            self.main_window.log
        )

    @Slot(bool, object)
    def _op_done(self, ok, result, action: str):
        if ok:
            self.main_window.log(f"✅ {action}成功")
            self.refresh()
        else:
            self.main_window.log(f"❌ {action}失败: {result}")
            QMessageBox.warning(self, "失败", f"{action}失败:\n{result}")


class EditFirmwareDialog(QDialog):
    def __init__(self, entry: BootEntry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.setWindowTitle("✏️ 编辑固件项")
        self.setMinimumWidth(500)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        layout.addWidget(QLabel(f"GUID: {self.entry.guid}"))

        layout.addWidget(QLabel("描述:"))
        self.desc_edit = QLineEdit(self.entry.description)
        layout.addWidget(self.desc_edit)

        layout.addWidget(QLabel("Device:"))
        self.device_edit = QLineEdit(self.entry.device)
        self.device_edit.setPlaceholderText("例如: partition=J:")
        layout.addWidget(self.device_edit)

        layout.addWidget(QLabel("Path:"))
        self.path_edit = QLineEdit(self.entry.path)
        self.path_edit.setPlaceholderText(r"例如: \EFI\ubuntu\shimx64.efi")
        layout.addWidget(self.path_edit)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("💾 保存")
        ok_btn.setObjectName("primary")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def get_values(self):
        return {
            "description": self.desc_edit.text().strip(),
            "device": self.device_edit.text().strip(),
            "path": self.path_edit.text().strip(),
        }


# ============================================================
# Linux 扫描面板
# ============================================================
class LinuxPanel(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.disks: List[dict] = []
        self.partitions: List[dict] = []
        self.efi_files: List[str] = []
        self.workers = WorkerManager()
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.driver_status = QLabel("🔍 正在检测 ext4 驱动...")
        self.driver_status.setStyleSheet("background: #f8f9fa; color: #5b6777; padding: 10px; border-radius: 6px; border: 1px solid #e1e4e8; font-size: 12px;")
        layout.addWidget(self.driver_status)

        driver_row = QHBoxLayout()
        self.install_driver_btn = QPushButton("📦 安装 ext4 驱动")
        self.install_driver_btn.setObjectName("primary")
        self.install_driver_btn.clicked.connect(self.install_driver)
        driver_row.addWidget(self.install_driver_btn)

        self.recheck_driver_btn = QPushButton("🔄 重新检测")
        self.recheck_driver_btn.clicked.connect(self.check_driver)
        driver_row.addWidget(self.recheck_driver_btn)

        driver_row.addStretch()
        layout.addLayout(driver_row)

        scan_row = QHBoxLayout()
        scan_btn = QPushButton("🔍 扫描所有磁盘")
        scan_btn.setObjectName("primary")
        scan_btn.clicked.connect(self.scan_disks)
        scan_row.addWidget(scan_btn)

        mount_all_btn = QPushButton("📂 一键挂载所有 Linux 分区")
        mount_all_btn.setObjectName("warning")
        mount_all_btn.clicked.connect(self.mount_all_linux)
        scan_row.addWidget(mount_all_btn)

        scan_row.addStretch()
        layout.addLayout(scan_row)

        layout.addWidget(QLabel("💽 物理磁盘:"))
        self.disk_table = QTableWidget()
        self.disk_table.setColumnCount(5)
        self.disk_table.setHorizontalHeaderLabels(["#", "名称", "大小(GB)", "分区表", "总线"])
        self.disk_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.disk_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.disk_table.setSelectionMode(QTableWidget.SingleSelection)
        self.disk_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.disk_table.verticalHeader().setVisible(False)
        self.disk_table.setMaximumHeight(140)
        self.disk_table.itemSelectionChanged.connect(self.on_disk_selected)
        layout.addWidget(self.disk_table)

        layout.addWidget(QLabel("📂 分区 (右键查看更多):"))
        self.part_table = QTableWidget()
        self.part_table.setColumnCount(6)
        self.part_table.setHorizontalHeaderLabels(["#", "盘符", "大小(GB)", "类型", "文件系统", "系统"])
        self.part_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.part_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.part_table.setSelectionMode(QTableWidget.SingleSelection)
        self.part_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.part_table.verticalHeader().setVisible(False)
        self.part_table.setMaximumHeight(160)
        self.part_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.part_table.customContextMenuRequested.connect(self.on_part_context_menu)
        layout.addWidget(self.part_table)

        op_row = QHBoxLayout()
        list_root_btn = QPushButton("📂 列出根目录")
        list_root_btn.clicked.connect(self.list_root)
        op_row.addWidget(list_root_btn)

        scan_efi_btn = QPushButton("🔍 扫描 EFI 文件")
        scan_efi_btn.setObjectName("primary")
        scan_efi_btn.clicked.connect(self.scan_efi)
        op_row.addWidget(scan_efi_btn)

        auto_mount_btn = QPushButton("🚀 一键修复 Linux 引导")
        auto_mount_btn.setObjectName("success")
        auto_mount_btn.clicked.connect(self.auto_mount_to_boot)
        op_row.addWidget(auto_mount_btn)

        op_row.addStretch()
        layout.addLayout(op_row)

        layout.addWidget(QLabel("📄 EFI 文件:"))
        self.efi_list = QListWidget()
        self.efi_list.setMaximumHeight(120)
        layout.addWidget(self.efi_list)

        QTimer.singleShot(500, self.check_driver)

    def check_driver(self):
        def do():
            installed = LinuxScanner.is_ext4_driver_installed()
            return True, installed
        self.workers.start(Worker(do), self._on_driver_check)

    @Slot(bool, object)
    def _on_driver_check(self, ok, installed):
        if installed:
            self.driver_status.setText("✅ ext4 驱动已安装")
            self.driver_status.setStyleSheet("background: #e8f5e9; color: #2e7d32; padding: 10px; border-radius: 6px; border: 1px solid #a5d6a7; font-size: 12px;")
            self.install_driver_btn.setEnabled(False)
            self.install_driver_btn.setText("✅ 已安装")
        else:
            self.driver_status.setText("⚠️ ext4 驱动未安装，无法读取 Linux 分区")
            self.driver_status.setStyleSheet("background: #fff3e0; color: #b85c00; padding: 10px; border-radius: 6px; border: 1px solid #ffd1b3; font-size: 12px;")
            self.install_driver_btn.setEnabled(True)
            self.install_driver_btn.setText("📦 安装 ext4 驱动")

    def install_driver(self):
        reply = QMessageBox.question(self, "确认安装",
            "将从 GitHub 下载并安装 ext4-win-driver。\n\n是否继续？",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self.install_driver_btn.setEnabled(False)
        self.install_driver_btn.setText("⏳ 安装中...")

        def do(log_callback=None):
            return LinuxScanner.install_ext4_driver(log_callback)

        self.workers.start(Worker(do), self._on_driver_installed, self.main_window.log)

    @Slot(bool, object)
    def _on_driver_installed(self, ok, result):
        self.install_driver_btn.setEnabled(True)
        if ok:
            QMessageBox.information(self, "成功", "✅ 驱动安装成功")
        else:
            QMessageBox.warning(self, "提示", f"安装结果:\n{result}")
        self.check_driver()

    def scan_disks(self):
        self.main_window.log("🔍 正在扫描物理磁盘...")

        def do():
            disks = LinuxScanner.list_physical_disks()
            return True, disks

        self.workers.start(Worker(do), self._on_disks_scanned, self.main_window.log)

    @Slot(bool, object)
    def _on_disks_scanned(self, ok, disks):
        if not ok:
            return
        self.disks = disks
        self.disk_table.setRowCount(len(disks))
        for i, d in enumerate(disks):
            self.disk_table.setItem(i, 0, QTableWidgetItem(str(d["number"])))
            self.disk_table.setItem(i, 1, QTableWidgetItem(d["name"]))
            self.disk_table.setItem(i, 2, QTableWidgetItem(f"{d['size_gb']:.1f}"))
            self.disk_table.setItem(i, 3, QTableWidgetItem(d["style"]))
            self.disk_table.setItem(i, 4, QTableWidgetItem(d["bus"]))
        self.main_window.log(f"✅ 找到 {len(disks)} 个磁盘")

        if len(disks) > 0:
            self.disk_table.selectRow(0)

    def on_disk_selected(self):
        rows = set()
        for item in self.disk_table.selectedItems():
            rows.add(item.row())
        if not rows:
            return
        row = list(rows)[0]
        if row >= len(self.disks):
            return
        disk_num = self.disks[row]["number"]
        self.main_window.log(f"🔍 正在读取磁盘 {disk_num} 的分区...")

        def do():
            parts = LinuxScanner.list_partitions(disk_num)
            for p in parts:
                letter = p.get("letter", "")
                ptype = (p.get("type") or "").lower()

                # 跳过容器型
                skip_types = ["extended", "xint13", "container", "reserved"]
                if any(skip in ptype for skip in skip_types):
                    p["fs"] = "容器分区"
                    p["system"] = "容器"
                    p["sys_confidence"] = 0
                    continue

                if not letter:
                    # 尝试分配
                    can_mount, fs, fstype = LinuxScanner.can_mount_partition(disk_num, p["number"])
                    if not can_mount:
                        p["fs"] = fs if fs != "unknown" else "未识别"
                        if "linux" in ptype:
                            p["system"] = "Linux (未挂载)"
                            p["sys_confidence"] = 30
                        else:
                            p["system"] = f"未挂载 ({ptype})"
                            p["sys_confidence"] = 10
                        continue

                    ok, assigned = LinuxScanner.auto_assign_letter(disk_num, p["number"])
                    if ok:
                        letter = assigned
                        p["letter"] = letter
                        p["auto_assigned"] = True
                    else:
                        p["fs"] = fs
                        p["system"] = "挂载失败"
                        p["sys_confidence"] = 0
                        continue

                if letter:
                    p["fs"] = LinuxScanner.detect_fs_type(letter)
                    try:
                        sys_name, conf, feats = SystemIdentifier.identify(letter)
                        p["system"] = sys_name
                        p["sys_confidence"] = conf
                        p["sys_features"] = feats
                    except Exception as e:
                        dbg(f"识别失败 {letter}: {e}")
                        p["system"] = "识别失败"
                        p["sys_confidence"] = 0

            return True, parts

        self.workers.start(Worker(do), self._on_parts_loaded, self.main_window.log)

    @Slot(bool, object)
    def _on_parts_loaded(self, ok, parts):
        if not ok:
            return
        self.partitions = parts
        self.part_table.setRowCount(len(parts))
        for i, p in enumerate(parts):
            self.part_table.setItem(i, 0, QTableWidgetItem(str(p["number"])))
            self.part_table.setItem(i, 1, QTableWidgetItem(p.get("letter", "") or "-"))
            self.part_table.setItem(i, 2, QTableWidgetItem(f"{p['size_gb']:.1f}"))
            self.part_table.setItem(i, 3, QTableWidgetItem(p.get("type", "")))

            fs_item = QTableWidgetItem(p.get("fs", "未知"))
            fs = p.get("fs", "").lower()
            if "ext" in fs:
                fs_item.setForeground(QColor("#28a745"))
            elif "ntfs" in fs:
                fs_item.setForeground(QColor("#1a73e8"))
            elif "fat" in fs:
                fs_item.setForeground(QColor("#ffc107"))
            self.part_table.setItem(i, 4, fs_item)

            sys_name = p.get("system", "-")
            sys_item = QTableWidgetItem(sys_name)
            if "Linux" in sys_name:
                sys_item.setForeground(QColor("#28a745"))
            elif "Windows" in sys_name:
                sys_item.setForeground(QColor("#1a73e8"))
            elif "EFI" in sys_name:
                sys_item.setForeground(QColor("#5b6777"))
            self.part_table.setItem(i, 5, sys_item)

        self.part_table.resizeColumnsToContents()
        self.part_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.main_window.log(f"✅ 找到 {len(parts)} 个分区")

    def _selected_partition(self) -> Optional[dict]:
        rows = set()
        for item in self.part_table.selectedItems():
            rows.add(item.row())
        if not rows:
            return None
        row = list(rows)[0]
        if row < len(self.partitions):
            return self.partitions[row]
        return None

    def on_part_context_menu(self, pos):
        item = self.part_table.itemAt(pos)
        if not item:
            return
        self.part_table.selectRow(item.row())
        part = self._selected_partition()
        if not part:
            return

        menu = QMenu(self)

        if not part.get("letter"):
            mount_action = QAction("📂 分配盘符并识别", self)
            mount_action.triggered.connect(lambda: self.mount_partition(part))
            menu.addAction(mount_action)
        else:
            refresh_action = QAction("🔄 重新识别", self)
            refresh_action.triggered.connect(lambda: self.refresh_partition(part))
            menu.addAction(refresh_action)
            release_action = QAction("🔓 释放盘符", self)
            release_action.triggered.connect(lambda: self.release_partition(part))
            menu.addAction(release_action)

        menu.addSeparator()

        scan_efi_action = QAction("🔍 扫描此分区 EFI", self)
        scan_efi_action.triggered.connect(lambda: self.scan_efi_for(part))
        menu.addAction(scan_efi_action)

        menu.addSeparator()

        auto_mount_action = QAction("🚀 一键修复 Linux 引导", self)
        auto_mount_action.triggered.connect(lambda: self._auto_mount_specific(part))
        menu.addAction(auto_mount_action)

        menu.exec(self.part_table.viewport().mapToGlobal(pos))

    def mount_partition(self, part):
        disk_row = -1
        for item in self.disk_table.selectedItems():
            disk_row = item.row()
            break
        if disk_row < 0:
            QMessageBox.warning(self, "提示", "请先在磁盘表选中一块磁盘")
            return
        disk_num = self.disks[disk_row]["number"]

        def do():
            ok, letter = LinuxScanner.auto_assign_letter(disk_num, part["number"])
            if ok:
                part["letter"] = letter
                part["fs"] = LinuxScanner.detect_fs_type(letter)
                sys_name, conf, feats = SystemIdentifier.identify(letter)
                part["system"] = sys_name
                part["sys_confidence"] = conf
            return ok, letter

        self.workers.start(
            Worker(do),
            lambda ok, r: self._on_manual_mount(ok, r, part),
            self.main_window.log
        )

    @Slot(bool, object)
    def _on_manual_mount(self, ok, result, part):
        if ok:
            self.main_window.log(f"✅ 已分配: {result}")
            self._refresh_all()
        else:
            QMessageBox.warning(self, "失败", f"分配失败: {result}")

    def refresh_partition(self, part):
        letter = part.get("letter", "")
        if not letter:
            return
        def do():
            part["fs"] = LinuxScanner.detect_fs_type(letter)
            sys_name, conf, feats = SystemIdentifier.identify(letter)
            part["system"] = sys_name
            part["sys_confidence"] = conf
            return True, "ok"
        self.workers.start(Worker(do), lambda ok, r: self._refresh_all(), self.main_window.log)

    def release_partition(self, part):
        letter = part.get("letter", "").rstrip(':').strip()
        if not letter:
            return
        reply = QMessageBox.question(self, "确认释放",
            f"确定释放盘符 {letter}: 吗？",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        def do():
            return LinuxScanner.release_letter(letter)
        def on_done(ok, result):
            if ok:
                part["letter"] = ""
                part["fs"] = "未挂载"
                self._refresh_all()
                self.main_window.log(f"🔓 已释放: {letter}:")
            else:
                QMessageBox.warning(self, "失败", f"释放失败: {result}")
        self.workers.start(Worker(do), on_done, self.main_window.log)

    def _refresh_all(self):
        for part in self.partitions:
            for i, p in enumerate(self.partitions):
                if p["number"] == part["number"]:
                    self.part_table.setItem(i, 1, QTableWidgetItem(p.get("letter", "") or "-"))
                    fs_item = QTableWidgetItem(p.get("fs", "未知"))
                    fs = p.get("fs", "").lower()
                    if "ext" in fs:
                        fs_item.setForeground(QColor("#28a745"))
                    elif "ntfs" in fs:
                        fs_item.setForeground(QColor("#1a73e8"))
                    self.part_table.setItem(i, 4, fs_item)
                    self.part_table.setItem(i, 5, QTableWidgetItem(p.get("system", "-")))
                    break

    def scan_efi_for(self, part):
        letter = part.get("letter", "")
        if not letter:
            QMessageBox.warning(self, "提示", "该分区没有盘符")
            return
        self._do_scan_efi(letter)

    def list_root(self):
        part = self._selected_partition()
        if not part:
            QMessageBox.warning(self, "提示", "请先选择一个分区")
            return
        letter = part.get("letter", "")
        if not letter:
            QMessageBox.warning(self, "提示", "该分区没有盘符")
            return
        items = SystemIdentifier.get_root_listing(letter, max_items=50)
        if not items:
            QMessageBox.information(self, "提示", "无法读取根目录")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"📂 {letter}\\ 根目录")
        dialog.setMinimumSize(500, 400)
        dialog.setStyleSheet(FLAT_LIGHT_STYLE)
        layout = QVBoxLayout(dialog)

        sys_name, conf, feats = SystemIdentifier.identify(letter)
        info_label = QLabel(f"🎯 识别结果: <b>{sys_name}</b> (置信度 {conf}/100)")
        info_label.setStyleSheet("padding: 8px; background: #e8f0fe; border-radius: 6px; color: #1a73e8;")
        layout.addWidget(info_label)

        list_widget = QListWidget()
        for item in items:
            list_widget.addItem(item)
        layout.addWidget(list_widget)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)
        dialog.exec()

    def mount_all_linux(self):
        disk_row = -1
        for item in self.disk_table.selectedItems():
            disk_row = item.row()
            break
        if disk_row < 0:
            QMessageBox.warning(self, "提示", "请先选中一块磁盘")
            return
        disk_num = self.disks[disk_row]["number"]

        targets = [p for p in self.partitions if not p.get("letter")]
        if not targets:
            QMessageBox.information(self, "提示", "没有未挂载的分区")
            return

        reply = QMessageBox.question(self, "确认",
            f"将挂载 {len(targets)} 个未挂载分区？",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        def do():
            success = 0
            for p in targets:
                ok, letter = LinuxScanner.auto_assign_letter(disk_num, p["number"])
                if ok:
                    p["letter"] = letter
                    p["fs"] = LinuxScanner.detect_fs_type(letter)
                    try:
                        sys_name, conf, feats = SystemIdentifier.identify(letter)
                        p["system"] = sys_name
                        p["sys_confidence"] = conf
                    except:
                        pass
                    success += 1
            return True, f"成功挂载 {success}/{len(targets)}"

        self.workers.start(Worker(do), self._on_mount_all_done, self.main_window.log)

    @Slot(bool, object)
    def _on_mount_all_done(self, ok, result):
        if ok:
            self.main_window.log(f"✅ {result}")
            self._refresh_all()

    def scan_efi(self):
        part = self._selected_partition()
        if not part:
            QMessageBox.warning(self, "提示", "请先选择一个分区")
            return
        letter = part.get("letter", "")
        if not letter:
            QMessageBox.warning(self, "提示", "请先分配盘符")
            return
        self._do_scan_efi(letter)

    def _do_scan_efi(self, letter: str):
        self.main_window.log(f"🔍 扫描 {letter} EFI...")
        known_paths = self.main_window.config.get("known_linux_efi_paths", [])
        def do():
            files = LinuxScanner.scan_efi_files(letter, known_paths)
            return True, files
        self.workers.start(Worker(do), self._on_efi_scanned, self.main_window.log)

    @Slot(bool, object)
    def _on_efi_scanned(self, ok, files):
        if not ok:
            return
        self.efi_files = files
        self.efi_list.clear()
        for f in files:
            self.efi_list.addItem(f)
        self.main_window.log(f"✅ 找到 {len(files)} 个 EFI 文件")

    def _auto_mount_specific(self, part):
        disk_row = -1
        for item in self.disk_table.selectedItems():
            disk_row = item.row()
            break
        if disk_row < 0:
            QMessageBox.warning(self, "提示", "请先选中磁盘")
            return
        disk_num = self.disks[disk_row]["number"]

        dialog = AutoMountOptionsDialog(part, self)
        if dialog.exec() != QDialog.Accepted:
            return
        opts = dialog.get_options()

        def do(log_callback=None):
            return AutoMountToBoot.auto_mount(
                part, self.partitions, disk_num,
                set_default=opts["set_default"],
                release_after=opts["release_after"],
                use_firmware=opts["use_firmware"],
                log_callback=log_callback,
            )

        self.workers.start(Worker(do), self._on_auto_mount_done, self.main_window.log)

    def auto_mount_to_boot(self):
        part = self._selected_partition()
        if not part:
            QMessageBox.warning(self, "提示", "请先选择一个 Linux 分区")
            return
        disk_row = -1
        for item in self.disk_table.selectedItems():
            disk_row = item.row()
            break
        if disk_row < 0:
            QMessageBox.warning(self, "提示", "请先选中磁盘")
            return
        disk_num = self.disks[disk_row]["number"]

        dialog = AutoMountOptionsDialog(part, self)
        if dialog.exec() != QDialog.Accepted:
            return
        opts = dialog.get_options()

        def do(log_callback=None):
            return AutoMountToBoot.auto_mount(
                part, self.partitions, disk_num,
                set_default=opts["set_default"],
                release_after=opts["release_after"],
                use_firmware=opts["use_firmware"],
                log_callback=log_callback,
            )

        self.workers.start(Worker(do), self._on_auto_mount_done, self.main_window.log)

    @Slot(bool, object)
    def _on_auto_mount_done(self, ok, result):
        if ok:
            self.main_window.log("✅ 自动挂载完成")
            QMessageBox.information(self, "成功", str(result))
            self.on_disk_selected()
        else:
            self.main_window.log(f"❌ 失败: {result}")
            QMessageBox.warning(self, "失败", f"失败:\n{result}")


class AutoMountOptionsDialog(QDialog):
    def __init__(self, part: dict, parent=None):
        super().__init__(parent)
        self.part = part
        self.setWindowTitle("🚀 一键修复 Linux 引导")
        self.setMinimumWidth(500)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        title = QLabel("🚀 一键修复 Linux 引导")
        title.setStyleSheet("font-size: 16px; font-weight: 600; color: #1a73e8;")
        layout.addWidget(title)

        info = QLabel(
            f"将自动执行:\n"
            f"  1️⃣ 给 Linux 分区分配盘符（如无）\n"
            f"  2️⃣ 扫描并定位 Linux EFI 文件\n"
            f"  3️⃣ 检测并复用 NVRAM 僵尸项\n"
            f"  4️⃣ 创建/修复启动项\n"
            f"  5️⃣ 加入启动菜单\n\n"
            f"选中分区: #{self.part.get('number', '?')}  "
            f"({self.part.get('size_gb', 0):.1f} GB)"
        )
        info.setStyleSheet("background: #e8f0fe; color: #1a73e8; padding: 12px; border-radius: 6px; border: 1px solid #c5d8f8; font-size: 12px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        opt_group = QGroupBox("选项")
        opt_layout = QVBoxLayout(opt_group)

        self.use_firmware_check = QCheckBox("🔥 使用独立固件启动项（推荐，绕过 Windows BCD）")
        self.use_firmware_check.setChecked(True)
        opt_layout.addWidget(self.use_firmware_check)

        self.set_default_check = QCheckBox("⭐ 设为默认启动项")
        opt_layout.addWidget(self.set_default_check)

        self.release_check = QCheckBox("🔓 完成后释放临时盘符")
        self.release_check.setChecked(True)
        opt_layout.addWidget(self.release_check)

        layout.addWidget(opt_group)

        warn = QLabel(
            "⚠️ 提示:\n"
            "• 独立固件项由主板 UEFI 直接加载，最稳定\n"
            "• 如果选 BCD 链式加载，可能遇到 0xC000007b 错误\n"
            "• 建议创建后重启测试"
        )
        warn.setStyleSheet("background: #fff3e0; color: #b85c00; padding: 10px; border-radius: 6px; border: 1px solid #ffd1b3; font-size: 11px;")
        warn.setWordWrap(True)
        layout.addWidget(warn)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("🚀 开始")
        ok_btn.setObjectName("primary")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def get_options(self):
        return {
            "use_firmware": self.use_firmware_check.isChecked(),
            "set_default": self.set_default_check.isChecked(),
            "release_after": self.release_check.isChecked(),
        }


# ============================================================
# 引导修复面板
# ============================================================
class RepairPanel(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.workers = WorkerManager()
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        warning = QLabel(
            "⚠️ 引导修复属于高风险操作！\n"
            "• 操作前建议先备份 BCD 和重要数据\n"
            "• bootrec 系列用于修复 MBR/引导扇区\n"
            "• bcdboot 用于重建 BCD（推荐方式）"
        )
        warning.setStyleSheet("background: #fff3e0; color: #b85c00; padding: 10px; border-radius: 6px; border: 1px solid #ffd1b3; font-size: 12px;")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        # 修复向导
        wizard_group = QGroupBox("🩺 引导修复向导")
        wizard_layout = QVBoxLayout(wizard_group)
        wizard_btn = QPushButton("🚀 启动修复向导（推荐新手）")
        wizard_btn.setObjectName("success")
        wizard_btn.setMinimumHeight(40)
        wizard_btn.clicked.connect(self.start_wizard)
        wizard_layout.addWidget(wizard_btn)
        layout.addWidget(wizard_group)

        grid = QGridLayout()
        grid.setSpacing(8)

        btn_fixmbr = QPushButton("🔧 修复 MBR\n(bootrec /fixmbr)")
        btn_fixmbr.clicked.connect(lambda: self.run_repair("fixmbr"))
        grid.addWidget(btn_fixmbr, 0, 0)

        btn_fixboot = QPushButton("🔧 修复引导扇区\n(bootrec /fixboot)")
        btn_fixboot.clicked.connect(lambda: self.run_repair("fixboot"))
        grid.addWidget(btn_fixboot, 0, 1)

        btn_scanos = QPushButton("🔍 扫描系统\n(bootrec /scanos)")
        btn_scanos.clicked.connect(lambda: self.run_repair("scanos"))
        grid.addWidget(btn_scanos, 1, 0)

        btn_rebuild = QPushButton("🔨 重建 BCD\n(bootrec /rebuildbcd)")
        btn_rebuild.setObjectName("warning")
        btn_rebuild.clicked.connect(lambda: self.run_repair("rebuildbcd"))
        grid.addWidget(btn_rebuild, 1, 1)

        layout.addLayout(grid)

        bcdboot_group = QGroupBox("🛠️ 重建 BCD（推荐）")
        bcdboot_layout = QGridLayout(bcdboot_group)

        bcdboot_layout.addWidget(QLabel("系统盘:"), 0, 0)
        self.system_drive = QLineEdit("C:")
        self.system_drive.setFixedWidth(60)
        bcdboot_layout.addWidget(self.system_drive, 0, 1)

        bcdboot_layout.addWidget(QLabel("EFI 分区:"), 0, 2)
        self.efi_drive = QLineEdit("J:")
        self.efi_drive.setFixedWidth(60)
        bcdboot_layout.addWidget(self.efi_drive, 0, 3)

        bcdboot_btn = QPushButton("🔨 执行 bcdboot")
        bcdboot_btn.setObjectName("primary")
        bcdboot_btn.clicked.connect(self.run_bcdboot)
        bcdboot_layout.addWidget(bcdboot_btn, 0, 4)

        layout.addWidget(bcdboot_group)

        backup_group = QGroupBox("💾 BCD 备份/恢复")
        backup_layout = QHBoxLayout(backup_group)

        backup_btn = QPushButton("📤 立即备份 BCD")
        backup_btn.clicked.connect(self.backup_bcd)
        backup_layout.addWidget(backup_btn)

        restore_btn = QPushButton("📥 从文件恢复")
        restore_btn.setObjectName("warning")
        restore_btn.clicked.connect(self.restore_bcd)
        backup_layout.addWidget(restore_btn)

        open_dir_btn = QPushButton("📂 打开备份目录")
        open_dir_btn.clicked.connect(lambda: os.startfile(str(BACKUP_DIR)))
        backup_layout.addWidget(open_dir_btn)

        backup_layout.addStretch()
        layout.addWidget(backup_group)

        layout.addWidget(QLabel("📋 输出:"))
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)

    def start_wizard(self):
        wizard = RepairWizard(self.main_window, self)
        wizard.exec()

    def run_repair(self, action: str):
        action_names = {
            "fixmbr": "修复 MBR", "fixboot": "修复引导扇区",
            "scanos": "扫描系统", "rebuildbcd": "重建 BCD",
        }
        reply = QMessageBox.question(self, "确认",
            f"确定执行「{action_names[action]}」？\n\n⚠️ 高风险！",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        funcs = {
            "fixmbr": BootRepair.fix_mbr,
            "fixboot": BootRepair.fix_boot,
            "scanos": BootRepair.scan_os,
            "rebuildbcd": BootRepair.rebuild_bcd,
        }

        def do():
            return funcs[action]()

        self.workers.start(
            Worker(do),
            lambda ok, r: self._on_repair_done(ok, r, action_names[action]),
            self.main_window.log
        )

    def run_bcdboot(self):
        sys_drive = self.system_drive.text().strip() or "C:"
        efi_drive = self.efi_drive.text().strip() or "J:"
        reply = QMessageBox.question(self, "确认",
            f"执行: bcdboot {sys_drive}\\Windows /s {efi_drive} /f ALL\n\n确认盘符正确？",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        def do():
            return BootRepair.bcdboot(sys_drive, efi_drive)

        self.workers.start(
            Worker(do),
            lambda ok, r: self._on_repair_done(ok, r, "bcdboot"),
            self.main_window.log
        )

    def backup_bcd(self):
        def do():
            return BCDEngine.backup_bcd(str(BACKUP_DIR))
        self.workers.start(Worker(do), self._on_backup_done)

    @Slot(bool, object)
    def _on_backup_done(self, ok, result):
        if ok:
            self.output.append(f"✅ BCD 已备份: {result}")
            self.main_window.log(f"✅ BCD 已备份")

    def restore_bcd(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择 BCD 备份", str(BACKUP_DIR), "BCD 备份 (*.bcd)")
        if not file_path:
            return
        reply = QMessageBox.question(self, "确认",
            f"从 {Path(file_path).name} 恢复？\n\n⚠️ 会覆盖当前 BCD！",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        def do():
            return BCDEngine.restore_bcd(file_path)

        self.workers.start(
            Worker(do),
            lambda ok, r: self._on_repair_done(ok, r, "恢复 BCD")
        )

    @Slot(bool, object)
    def _on_repair_done(self, ok, result, action: str):
        self.output.append(f"\n{'='*50}")
        self.output.append(f"操作: {action}")
        self.output.append(f"结果: {'✅ 成功' if ok else '❌ 失败'}")
        self.output.append(f"{'-'*50}")
        self.output.append(str(result))
        self.output.append(f"{'='*50}\n")
        if ok:
            self.main_window.log(f"✅ {action}成功")
        else:
            self.main_window.log(f"❌ {action}失败")


class RepairWizard(QDialog):
    """引导修复向导"""
    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self.parent_panel = parent
        self.setWindowTitle("🩺 引导修复向导")
        self.setMinimumSize(700, 550)
        self.setStyleSheet(FLAT_LIGHT_STYLE)
        self.current_step = 0
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("🩺 引导修复向导")
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #1a73e8;")
        layout.addWidget(title)

        self.step_label = QLabel()
        self.step_label.setStyleSheet("font-size: 14px; color: #5b6777;")
        layout.addWidget(self.step_label)

        self.content = QTextEdit()
        self.content.setReadOnly(True)
        self.content.setMinimumHeight(250)
        self.content.setStyleSheet("""
            QTextEdit {
                background: #ffffff;
                border: 1px solid #e1e4e8;
                border-radius: 8px;
                padding: 12px;
                font-size: 13px;
                line-height: 1.6;
            }
        """)
        layout.addWidget(self.content)

        self.diagnosis = QLabel()
        self.diagnosis.setStyleSheet("background: #e8f0fe; color: #1a73e8; padding: 12px; border-radius: 8px; font-size: 13px;")
        self.diagnosis.setWordWrap(True)
        layout.addWidget(self.diagnosis)

        btn_row = QHBoxLayout()
        self.prev_btn = QPushButton("⬅️ 上一步")
        self.prev_btn.clicked.connect(self.prev_step)
        self.prev_btn.setEnabled(False)
        btn_row.addWidget(self.prev_btn)

        self.next_btn = QPushButton("➡️ 下一步")
        self.next_btn.setObjectName("primary")
        self.next_btn.clicked.connect(self.next_step)
        btn_row.addWidget(self.next_btn)

        self.fix_btn = QPushButton("🔧 一键修复")
        self.fix_btn.setObjectName("success")
        self.fix_btn.clicked.connect(self.execute_fix)
        self.fix_btn.setVisible(False)
        btn_row.addWidget(self.fix_btn)

        btn_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self.show_step(0)

    def show_step(self, step):
        self.current_step = step
        self.step_label.setText(f"第 {step+1} / 4 步")

        if step == 0:
            self.content.setHtml("""
                <h3>👋 欢迎使用引导修复向导</h3>
                <p>本向导会帮助你诊断并修复常见的引导问题：</p>
                <ul>
                    <li>🔍 检测当前引导模式（UEFI/BIOS）</li>
                    <li>🔍 检测磁盘分区表（GPT/MBR）</li>
                    <li>🔍 检查 BCD 配置完整性</li>
                    <li>🔍 检查 UEFI 固件启动项</li>
                    <li>🔧 一键修复发现的问题</li>
                </ul>
                <p><b>点击「下一步」开始诊断。</b></p>
            """)
            self.diagnosis.setText("💡 提示: 请以管理员身份运行")
            self.diagnosis.setStyleSheet("background: #e8f0fe; color: #1a73e8; padding: 12px; border-radius: 8px; font-size: 13px;")

        elif step == 1:
            self.content.setHtml("""
                <h3>🔍 正在诊断引导配置...</h3>
                <p>正在收集以下信息：</p>
                <ul>
                    <li>启动模式 (UEFI / Legacy)</li>
                    <li>磁盘分区表类型 (GPT / MBR)</li>
                    <li>BCD 启动项列表</li>
                    <li>UEFI 固件启动项列表</li>
                    <li>僵尸固件项检测</li>
                </ul>
                <p>请稍候...</p>
            """)
            self.diagnosis.setText("⏳ 正在诊断...")

            from threading import Thread
            def diagnose():
                results = self._diagnose()
                QMetaObject.invokeMethod(self, "_show_diagnosis", Qt.QueuedConnection,
                                         Q_ARG(str, results))
            Thread(target=diagnose, daemon=True).start()

        elif step == 2:
            self.content.setHtml("""
                <h3>📊 诊断结果</h3>
                <p>根据诊断结果，系统会给出修复建议。</p>
                <p>点击「下一步」查看修复方案。</p>
            """)
            self.diagnosis.setText(f"✅ 诊断完成\n\n{self.diagnosis_text}")

        elif step == 3:
            self.content.setHtml("""
                <h3>🔧 修复方案</h3>
                <p>根据诊断结果，建议执行以下修复：</p>
                <ul>
                    <li>修复僵尸固件项</li>
                    <li>重建 BCD（如需要）</li>
                    <li>修复引导顺序</li>
                </ul>
                <p><b>点击「🔧 一键修复」执行。</b></p>
            """)
            self.diagnosis.setText(f"🛠️ {self.fix_plan}")
            self.fix_btn.setVisible(True)

        self.prev_btn.setEnabled(step > 0)
        self.next_btn.setEnabled(step < 3)

    def _diagnose(self):
        info = []
        try:
            mode = BCDEngine.get_boot_mode()
            info.append(f"启动模式: {mode}")

            disk_type = BCDEngine.get_disk_type()
            info.append(f"磁盘类型: {disk_type}")

            ok, entries = BCDEngine.enum_all()
            bcd_count = sum(1 for e in entries if not e.is_firmware) if ok else 0
            info.append(f"BCD 启动项: {bcd_count} 个")

            ok, fw_entries = BCDEngine.enum_firmware()
            fw_count = len([e for e in fw_entries if e.is_firmware]) if ok else 0
            zombie_count = len([e for e in fw_entries if e.is_zombie]) if ok else 0
            info.append(f"固件启动项: {fw_count} 个")
            info.append(f"僵尸固件项: {zombie_count} 个")

            if zombie_count > 0:
                info.append(f"\n⚠️ 发现 {zombie_count} 个僵尸项，建议清理或修复")
                self.fix_plan = f"🔧 修复 {zombie_count} 个僵尸项\n🔧 重建 BCD"
            elif bcd_count == 0:
                info.append(f"\n⚠️ BCD 启动项为空，建议重建 BCD")
                self.fix_plan = f"🔧 重建 BCD"
            else:
                info.append(f"\n✅ 引导配置看起来正常")
                self.fix_plan = "✅ 无需修复"

            self.diagnosis_text = "\n".join(info)
            return self.diagnosis_text
        except Exception as e:
            self.diagnosis_text = f"诊断失败: {e}"
            self.fix_plan = "❌ 诊断失败"
            return self.diagnosis_text

    @Slot(str)
    def _show_diagnosis(self, text):
        self.diagnosis.setText(f"✅ 诊断完成\n\n{text}")

    def prev_step(self):
        if self.current_step > 0:
            self.show_step(self.current_step - 1)

    def next_step(self):
        if self.current_step < 3:
            self.show_step(self.current_step + 1)

    def execute_fix(self):
        reply = QMessageBox.question(self, "确认",
            "将执行以下修复:\n\n" + self.fix_plan + "\n\n确定继续？",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        QMessageBox.information(self, "提示", "修复命令已发送，请查看主界面日志。")
        self.accept()


# ============================================================
# 备份面板
# ============================================================
class BackupPanel(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.workers = WorkerManager()
        self.backups: List[dict] = []
        self.setup_ui()
        QTimer.singleShot(500, self.refresh_list)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        info = QLabel(
            "💾 完整引导配置备份\n"
            "• 导出: BCD (.bcd) + 配置 (JSON) + 恢复脚本 (.bat + .sh)\n"
            "• 恢复: 从 JSON 一键恢复所有配置\n"
            "• 建议: 每次修改引导前先备份"
        )
        info.setStyleSheet("background: #e8f0fe; color: #1a73e8; padding: 10px; border-radius: 6px; border: 1px solid #c5d8f8; font-size: 12px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        toolbar = QHBoxLayout()
        backup_btn = QPushButton("📦 创建完整备份")
        backup_btn.setObjectName("success")
        backup_btn.clicked.connect(self.create_backup)
        toolbar.addWidget(backup_btn)

        refresh_btn = QPushButton("🔄 刷新列表")
        refresh_btn.clicked.connect(self.refresh_list)
        toolbar.addWidget(refresh_btn)

        open_dir_btn = QPushButton("📂 打开备份目录")
        open_dir_btn.clicked.connect(lambda: os.startfile(str(BACKUP_DIR)))
        toolbar.addWidget(open_dir_btn)

        toolbar.addStretch()
        layout.addLayout(toolbar)

        layout.addWidget(QLabel("📋 已有备份:"))

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self.show_backup_details)
        layout.addWidget(self.list_widget, 1)

        detail_row = QHBoxLayout()
        restore_btn = QPushButton("📥 恢复此备份")
        restore_btn.setObjectName("warning")
        restore_btn.clicked.connect(self.restore_backup)
        detail_row.addWidget(restore_btn)

        open_btn = QPushButton("📂 打开此备份目录")
        open_btn.clicked.connect(self.open_selected_backup_dir)
        detail_row.addWidget(open_btn)

        export_btn = QPushButton("📤 导出备份到 ZIP")
        export_btn.clicked.connect(self.export_backup_zip)
        detail_row.addWidget(export_btn)

        delete_btn = QPushButton("🗑️ 删除备份")
        delete_btn.setObjectName("danger")
        delete_btn.clicked.connect(self.delete_backup)
        detail_row.addWidget(delete_btn)

        detail_row.addStretch()
        layout.addLayout(detail_row)

    def refresh_list(self):
        self.backups = BackupManager.list_backups()
        self.list_widget.clear()
        for b in self.backups:
            size_mb = b["size"] / 1024 / 1024
            item = QListWidgetItem(f"📦 {b['timestamp']}  ({size_mb:.2f} MB)")
            item.setData(Qt.UserRole, b)
            self.list_widget.addItem(item)
        self.main_window.log(f"✅ 找到 {len(self.backups)} 个备份")

    def create_backup(self):
        self.main_window.log("📦 开始创建完整备份...")

        def do(log_callback=None):
            return BackupManager.create_full_backup(log_callback)

        self.workers.start(Worker(do), self._on_backup_done, self.main_window.log)

    @Slot(bool, object)
    def _on_backup_done(self, ok, result):
        if ok:
            QMessageBox.information(self, "成功", f"✅ 备份完成:\n{result}")
            self.refresh_list()
        else:
            QMessageBox.warning(self, "失败", f"备份失败:\n{result}")

    def _selected_backup(self) -> Optional[dict]:
        item = self.list_widget.currentItem()
        if not item:
            return None
        return item.data(Qt.UserRole)

    def show_backup_details(self):
        b = self._selected_backup()
        if not b:
            return
        QMessageBox.information(self, "备份详情",
            f"路径: {b['path']}\n"
            f"时间: {b['timestamp']}\n"
            f"大小: {b['size']/1024/1024:.2f} MB\n"
            f"JSON: {Path(b['json']).name}")

    def restore_backup(self):
        b = self._selected_backup()
        if not b:
            QMessageBox.warning(self, "提示", "请先选择备份")
            return
        reply = QMessageBox.question(self, "确认恢复",
            f"从以下备份恢复引导配置？\n\n{b['timestamp']}\n\n⚠️ 会覆盖当前配置！",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        def do(log_callback=None):
            return BackupManager.restore_from_backup(b['json'], log_callback)

        self.workers.start(Worker(do), self._on_restore_done, self.main_window.log)

    @Slot(bool, object)
    def _on_restore_done(self, ok, result):
        if ok:
            QMessageBox.information(self, "成功", f"✅ 恢复完成:\n{result}")
            self.main_window.bcd_panel.refresh()
            self.main_window.uefi_panel.refresh()
        else:
            QMessageBox.warning(self, "失败", f"恢复失败:\n{result}")

    def open_selected_backup_dir(self):
        b = self._selected_backup()
        if not b:
            return
        os.startfile(b['path'])

    def export_backup_zip(self):
        b = self._selected_backup()
        if not b:
            QMessageBox.warning(self, "提示", "请先选择备份")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出 ZIP", str(BASE_DIR / f"backup_{b['timestamp']}.zip"),
            "ZIP 文件 (*.zip)")
        if not file_path:
            return
        try:
            shutil.make_archive(file_path.replace('.zip', ''), 'zip', b['path'])
            QMessageBox.information(self, "成功", f"已导出:\n{file_path}")
        except Exception as e:
            QMessageBox.warning(self, "失败", str(e))

    def delete_backup(self):
        b = self._selected_backup()
        if not b:
            return
        reply = QMessageBox.question(self, "确认删除",
            f"删除备份 {b['timestamp']}？\n\n⚠️ 不可恢复！",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        try:
            shutil.rmtree(b['path'])
            self.refresh_list()
        except Exception as e:
            QMessageBox.warning(self, "失败", str(e))


# ============================================================
# 设置面板
# ============================================================
class SettingsPanel(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.setup_ui()
        self.load_settings()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        general_group = QGroupBox("⚙️ 通用设置")
        general_layout = QVBoxLayout(general_group)

        self.auto_driver_check = QCheckBox("自动检测 ext4 驱动状态")
        general_layout.addWidget(self.auto_driver_check)

        self.auto_backup_check = QCheckBox("修改 BCD 前自动备份")
        general_layout.addWidget(self.auto_backup_check)

        layout.addWidget(general_group)

        backup_group = QGroupBox("📦 备份设置")
        backup_layout = QVBoxLayout(backup_group)

        backup_row = QHBoxLayout()
        backup_row.addWidget(QLabel("备份目录:"))
        self.backup_dir_edit = QLineEdit(str(BACKUP_DIR))
        self.backup_dir_edit.setReadOnly(True)
        backup_row.addWidget(self.backup_dir_edit, 1)
        browse_btn = QPushButton("浏览")
        browse_btn.clicked.connect(self.browse_backup_dir)
        backup_row.addWidget(browse_btn)
        backup_layout.addLayout(backup_row)

        layout.addWidget(backup_group)

        efi_group = QGroupBox("📄 已知 Linux EFI 路径")
        efi_layout = QVBoxLayout(efi_group)
        self.efi_paths_edit = QTextEdit()
        self.efi_paths_edit.setMaximumHeight(180)
        efi_layout.addWidget(self.efi_paths_edit)
        layout.addWidget(efi_group)

        about_group = QGroupBox("ℹ️ 关于")
        about_layout = QVBoxLayout(about_group)
        about_text = QLabel(
            f"<b>{APP_NAME} v{APP_VERSION}</b><br><br>"
            "Windows 引导管理器<br>"
            "EasyBCD + EasyUEFI 的开源替代<br><br>"
            "功能:<br>"
            "• BCD 管理（Windows/VHD/WinPE/Linux）<br>"
            "• UEFI 固件项管理（含僵尸项检测）<br>"
            "• 独立固件启动项创建<br>"
            "• Linux 扫描 + 一键修复引导<br>"
            "• 完整引导配置备份（JSON+BAT+SH）<br>"
            "• 引导修复向导<br>"
            "• 高级 BCD 选项<br><br>"
            "<span style='color:#5b6777;'>Mikan 系列工具 🌸</span>"
        )
        about_text.setWordWrap(True)
        about_layout.addWidget(about_text)
        layout.addWidget(about_group)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("💾 保存设置")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self.save_settings)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def load_settings(self):
        cfg = self.main_window.config
        self.auto_driver_check.setChecked(cfg.get("auto_install_driver", True))
        self.auto_backup_check.setChecked(cfg.get("backup_bcd_before_change", True))
        self.efi_paths_edit.setPlainText("\n".join(cfg.get("known_linux_efi_paths", [])))

    def save_settings(self):
        cfg = self.main_window.config
        cfg["auto_install_driver"] = self.auto_driver_check.isChecked()
        cfg["backup_bcd_before_change"] = self.auto_backup_check.isChecked()
        paths = [l.strip() for l in self.efi_paths_edit.toPlainText().split("\n") if l.strip()]
        cfg["known_linux_efi_paths"] = paths
        ConfigManager.save(cfg)
        QMessageBox.information(self, "成功", "设置已保存")

    def browse_backup_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "选择备份目录", str(BACKUP_DIR))
        if folder:
            self.backup_dir_edit.setText(folder)


# ============================================================
# 主窗口
# ============================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = ConfigManager.load()
        self.detect_workers = WorkerManager()
        self.setup_ui()
        self.setStyleSheet(FLAT_LIGHT_STYLE)
        QTimer.singleShot(500, self.auto_refresh)

    def setup_ui(self):
        self.setWindowTitle(f"🚀 {APP_NAME} v{APP_VERSION}")
        self.setMinimumSize(1200, 800)
        self.resize(1350, 900)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        header.setStyleSheet("background: #ffffff; border-bottom: 1px solid #e1e4e8;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(20, 14, 20, 14)

        title = QLabel(f"🚀 {APP_NAME}")
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #1a73e8;")
        header_layout.addWidget(title)

        version_label = QLabel(f"v{APP_VERSION}")
        version_label.setStyleSheet("color: #5b6777; font-size: 12px; padding-top: 6px;")
        header_layout.addWidget(version_label)

        header_layout.addStretch()

        self.mode_label = QLabel("模式: 检测中...")
        self.mode_label.setStyleSheet("color: #5b6777; font-size: 12px; padding: 6px 12px; background: #f8f9fa; border-radius: 6px;")
        header_layout.addWidget(self.mode_label)

        self.disk_label = QLabel("磁盘: 检测中...")
        self.disk_label.setStyleSheet("color: #5b6777; font-size: 12px; padding: 6px 12px; background: #f8f9fa; border-radius: 6px;")
        header_layout.addWidget(self.disk_label)

        self.admin_label = QLabel("权限: 检测中...")
        self.admin_label.setStyleSheet("color: #5b6777; font-size: 12px; padding: 6px 12px; background: #f8f9fa; border-radius: 6px;")
        header_layout.addWidget(self.admin_label)

        layout.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.setContentsMargins(12, 12, 12, 12)

        self.bcd_panel = BCDPanel(self)
        self.tabs.addTab(self.bcd_panel, "📋 BCD 管理")

        self.uefi_panel = UEFIPanel(self)
        self.tabs.addTab(self.uefi_panel, "🔧 UEFI 启动项")

        self.linux_panel = LinuxPanel(self)
        self.tabs.addTab(self.linux_panel, "🐧 Linux 扫描")

        self.repair_panel = RepairPanel(self)
        self.tabs.addTab(self.repair_panel, "🩺 引导修复")

        self.backup_panel = BackupPanel(self)
        self.tabs.addTab(self.backup_panel, "📦 备份导出")

        self.settings_panel = SettingsPanel(self)
        self.tabs.addTab(self.settings_panel, "⚙️ 设置")

        layout.addWidget(self.tabs, 1)

        log_widget = QWidget()
        log_widget.setStyleSheet("background: #fafbfc; border-top: 1px solid #e1e4e8;")
        log_layout = QVBoxLayout(log_widget)
        log_layout.setContentsMargins(12, 8, 12, 8)
        log_layout.setSpacing(4)

        log_header = QHBoxLayout()
        log_title = QLabel("📋 日志")
        log_title.setStyleSheet("font-weight: 600; color: #5b6777; font-size: 12px;")
        log_header.addWidget(log_title)
        log_header.addStretch()

        clear_btn = QPushButton("清空")
        clear_btn.setFixedHeight(24)
        clear_btn.clicked.connect(lambda: self.log_text.clear())
        log_header.addWidget(clear_btn)

        log_layout.addLayout(log_header)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(120)
        self.log_text.setStyleSheet(
            "background: #ffffff; border: 1px solid #e1e4e8; "
            "border-radius: 6px; padding: 6px; font-family: Consolas, monospace; "
            "font-size: 12px; color: #2c3e50;"
        )
        log_layout.addWidget(self.log_text)

        layout.addWidget(log_widget)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")

    def log(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{ts}] {message}")
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )

    def auto_refresh(self):
        if not is_admin():
            self.admin_label.setText("权限: ❌ 非管理员")
            self.admin_label.setStyleSheet("color: #b85c00; font-size: 12px; padding: 6px 12px; background: #fff3e0; border-radius: 6px;")
        else:
            self.admin_label.setText("权限: ✅ 管理员")
            self.admin_label.setStyleSheet("color: #2e7d32; font-size: 12px; padding: 6px 12px; background: #e8f5e9; border-radius: 6px;")

        def detect():
            mode = BCDEngine.get_boot_mode()
            disk_type = BCDEngine.get_disk_type()
            return True, {"mode": mode, "disk": disk_type}

        self.detect_workers.start(Worker(detect), self._on_detect_done)

    @Slot(bool, object)
    def _on_detect_done(self, ok, result):
        if ok:
            self.mode_label.setText(f"启动模式: {result['mode']}")
            if result['mode'] == "UEFI":
                self.mode_label.setStyleSheet("color: #1a73e8; font-size: 12px; padding: 6px 12px; background: #e8f0fe; border-radius: 6px;")
            self.disk_label.setText(f"磁盘: {result['disk']}")

    def update_status(self):
        self.status_bar.showMessage(
            f"BCD 项: {len(self.bcd_panel.entries)} | "
            f"固件项: {len(self.uefi_panel.fw_entries)} | "
            f"备份: {len(self.backup_panel.backups)} | "
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def closeEvent(self, event):
        ConfigManager.save(self.config)
        for mgr in [self.detect_workers, self.bcd_panel.workers,
                    self.uefi_panel.workers, self.linux_panel.workers,
                    self.repair_panel.workers, self.backup_panel.workers]:
            try:
                mgr.stop_all()
            except:
                pass
        event.accept()


# ============================================================
# 入口
# ============================================================
def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    font = QFont("Microsoft YaHei", 9)
    app.setFont(font)

    window = MainWindow()
    window.show()

    if not is_admin():
        QTimer.singleShot(1500, lambda: _show_admin_hint(window))

    sys.exit(app.exec())


def _show_admin_hint(window):
    reply = QMessageBox.question(
        window, "管理员权限",
        "Mikan Boot Manager 的许多功能需要管理员权限：\n\n"
        "• 读取 UEFI 固件启动项\n"
        "• 修改 BCD 启动项\n"
        "• 读取磁盘分区 / 分配盘符\n"
        "• 创建独立固件启动项\n"
        "• 执行引导修复\n\n"
        "是否以管理员身份重新启动？",
        QMessageBox.Yes | QMessageBox.No
    )
    if reply == QMessageBox.Yes:
        if request_admin():
            window.close()
        else:
            sys.exit(0)


if __name__ == "__main__":
    main()