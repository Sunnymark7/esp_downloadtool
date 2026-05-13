# ESP32 一键下载工具

用于把 ZIP 固件包自动识别后下载到 ESP32。默认是用户模式，只保留选择固件、选择/自动识别串口、开始下载几个核心操作。

## 目录结构

```text
assets/                 图标资源
packaging/              PyInstaller 打包配置和 Windows 版本信息
release/                发布说明和发布压缩包
src/                    应用源码
requirements.txt        Python 依赖
README.md               项目说明
```

## 源码运行

```powershell
python src\esp32_flasher_gui.py
```

## 用户模式

- 选择固件 ZIP。
- 工具自动识别 `bootloader.bin`、分区表和主程序。
- 没有配置文件时，默认从 `0x0` 开始烧录；单文件固件默认地址也是 `0x0`。
- 串口默认自动识别。
- 芯片默认使用 `--chip auto`，由 esptool 自动识别板子。
- 波特率默认使用快速自动：先直接用 `921600` 下载，失败再降到 `460800`、`230400`、`115200`。
- 不再单独执行 `chip-id` 预探测，减少连接步骤，提高下载速度。

## 开发者模式

可手动调整：

- 芯片型号
- 波特率
- Flash mode / freq / size
- 烧录后复位方式
- 是否整片擦除 Flash
- 烧录文件和地址

开发者模式内容在窗口内部滚动显示

## 固件包结构

推荐：

```text
firmware.zip
├─ bootloader.bin
├─ partition-table.bin
└─ app.bin
```

如果 ZIP 内包含 ESP-IDF 的 `flasher_args.json`，工具会优先使用其中的真实烧录地址。

下载时界面会显示综合进度条和当前状态。进度会覆盖准备、串口识别、板子连接、擦除、写入、校验、复位和完成阶段；完成或失败后会弹窗提醒。

## 打包

先安装依赖：

```powershell
python -m pip install -r requirements.txt
```

生成图标：

```powershell
python scripts\create_icon.py
```

打包辅助烧录程序：

```powershell
python -m PyInstaller --noconfirm --clean packaging\esptool_runner.spec
```

打包主程序：

```powershell
python -m PyInstaller --noconfirm --clean packaging\ESP32Flasher.spec
```

发布时需要把 `dist\ESP32Flasher.exe` 和 `dist\esptool_runner.exe` 放在同一目录。也可以直接使用 `release\ESP32Flasher_release.zip`。

EXE 属性信息中的公司名已设置为“元思科技”。
