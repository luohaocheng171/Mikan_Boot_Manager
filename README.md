[README.md](https://github.com/user-attachments/files/32429489/README.md)
# Mikan Boot Manager

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/)
[![PySide6](https://img.shields.io/badge/GUI-PySide6-green.svg)](https://www.qt.io/qt-for-python)

> Windows 引导管理器 — EasyBCD + EasyUEFI 的完整开源替代方案

Mikan Boot Manager 是一款基于 Python 和 PySide6 开发的 Windows 引导管理器桌面应用程序，提供图形化界面来管理 BCD 启动项、UEFI 固件启动项、Linux 引导配置以及执行引导修复操作。旨在为 Windows 用户提供一个免费、开源、功能全面的引导管理工具。

---

## 功能特性

### BCD 管理
- 查看所有 BCD 启动项（Windows / VHD / VHDX / WinPE / ISO / Linux）
- 添加 Windows、Linux、VHD/VHDX、WinPE 启动项
- 编辑启动项描述、设置默认启动项
- 调整启动菜单超时时间
- 高级 BCD 选项配置（DEP、detecthal、测试签名等）
- 启动顺序拖拽调整

### UEFI 固件项管理
- 查看所有 UEFI 固件启动项
- 僵尸项检测与修复（自动识别无 path/device 的无效固件项）
- 独立固件启动项创建（绕过 Windows BCD 链式加载，由主板 UEFI 直接加载 GRUB）
- 启动顺序调整（上移/下移/应用）
- 设置下次启动项
- 批量清理僵尸项

### Linux 扫描与引导修复
- 自动检测并安装 ext4 驱动（ext4-win-driver）
- 扫描所有物理磁盘及分区
- 自动识别 Linux 分区（Ubuntu、Fedora、Debian、Arch、Manjaro 等）
- 一键挂载 Linux 分区并分配盘符
- 扫描 EFI 分区中的引导文件
- 一键修复 Linux 引导（自动挂载 + 扫描 EFI + 创建/修复启动项）

### 引导修复工具
- 修复 MBR（bootrec /fixmbr）
- 修复引导扇区（bootrec /fixboot）
- 扫描系统（bootrec /scanos）
- 重建 BCD（bootrec /rebuildbcd）
- bcdboot 重建引导（推荐方式）
- 引导修复向导（分步诊断 + 一键修复）
- BCD 备份与恢复

### 备份与导出
- 完整引导配置备份（JSON + BAT + SH + BCD 文件）
- 从备份一键恢复所有配置
- 导出备份为 ZIP 压缩包
- 备份目录管理

### 其他特性
- 管理员权限自动检测与提权
- PowerShell 命令封装执行
- 扁平化白底蓝调 UI 风格
- JSON 持久化配置
- 异步 Worker 线程（不阻塞 UI）
- 操作日志实时显示

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.8+ |
| GUI 框架 | PySide6 (Qt for Python) |
| 系统交互 | subprocess (bcdedit / powershell) |
| 配置存储 | JSON 文件 |
| 驱动支持 | ext4-win-driver (antimatter-studios) |
| UI 风格 | 自定义 QSS 扁平化主题 |

---

## 安装与运行

### 环境要求

- Windows 10/11（支持 UEFI 和 Legacy BIOS）
- Python 3.8 或更高版本
- 管理员权限（部分功能需要）

### 安装依赖

```bash
pip install PySide6
```

### 运行程序

```bash
python Mikan_Boot_Manager.py
```

首次运行时，程序会提示是否以管理员身份运行。建议点击"是"以获得完整功能权限。

---

## 项目结构

```
Mikan_Boot_Manager.py    # 主程序入口（单文件应用）
├── boot_config/          # 配置目录
│   ├── config.json       # 用户配置
│   ├── backups/          # 备份文件
│   ├── logs/             # 运行日志
│   └── drivers/          # 驱动文件
```

> 注：本项目采用单文件架构，所有模块、面板、对话框和工具类均集成在 `Mikan_Boot_Manager.py` 中，便于分发和部署。

---

## 模块说明

| 模块 | 说明 |
|------|------|
| `ConfigManager` | 配置管理（JSON 持久化） |
| `BCDEngine` | BCD 引擎核心（bcdedit 封装） |
| `LinuxScanner` | Linux 分区扫描与挂载 |
| `SystemIdentifier` | 系统识别（分区文件系统检测） |
| `AutoMountToBoot` | 自动挂载并创建引导项 |
| `BootRepair` | 引导修复工具集 |
| `BackupManager` | 完整备份/恢复管理 |
| `Worker` / `WorkerManager` | 异步线程管理器 |
| `BCDPanel` | BCD 管理面板 |
| `UEFIPanel` | UEFI 固件项面板 |
| `LinuxPanel` | Linux 扫描面板 |
| `RepairPanel` | 引导修复面板 |
| `BackupPanel` | 备份导出面板 |
| `SettingsPanel` | 设置面板 |
| `MainWindow` | 主窗口 |

---

## 注意事项

1. **管理员权限**：许多功能（读取 UEFI 固件项、修改 BCD、磁盘分区操作等）需要管理员权限。首次运行时程序会提示提权。
2. **杀毒软件**：部分功能涉及系统底层操作，可能被杀毒软件误报。请将程序加入白名单。
3. **备份建议**：在执行引导修复或大规模 BCD 修改前，建议先使用内置备份功能保存当前配置。
4. **ext4 驱动**：Linux 分区扫描功能依赖 ext4-win-driver。程序提供一键安装功能，也可手动从 [antimatter-studios/ext4-win-driver](https://github.com/antimatter-studios/ext4-win-driver) 下载。
5. **UEFI 模式**：推荐使用 UEFI 模式启动，可获得更好的引导稳定性和功能支持。

---

## 开发

### 代码风格

- 遵循 PEP 8 编码规范
- 使用类型注解（typing）
- 异步操作通过 QThread + WorkerManager 实现
- UI 样式统一使用 QSS 管理

### 添加新功能

1. 在对应面板类中实现 UI 逻辑
2. 在 `BCDEngine` 或专用工具类中封装系统命令
3. 通过 `Worker` 异步执行耗时操作
4. 在主窗口 `MainWindow.setup_ui()` 中注册新标签页

---

## 许可证

本项目采用 **GNU General Public License v3.0** 许可。你可以自由地复制、修改和再分发本软件，但必须保留版权声明和许可声明。衍生作品也必须以相同的许可证开源。

详见 [LICENSE](LICENSE) 文件。

---

## 致谢

- [PySide6](https://www.qt.io/qt-for-python) - Qt for Python 绑定
- [ext4-win-driver](https://github.com/antimatter-studios/ext4-win-driver) - Windows ext4 文件系统驱动
- [EasyBCD](https://www.easybcd.com/) - 灵感来源
- [DeepSeek](https://www.deepseek.com/) - AI 辅助开发

---

## 联系方式

如有问题或建议，欢迎提交 Issue 或 Pull Request。

---

*Made with ❤️ by Mikan Team · AI assisted by DeepSeek*
