# Shimadzu UV-Vis Automation

通过岛津 **LabSolutions UV-Vis 自动控制文本交换功能**规划四种 UV-Vis 模式，并执行可审计的 Spectrum 测量和重复完整光谱采集。

本项目不直接访问仪器 USB、串口或底层驱动。LabSolutions 负责连接仪器、执行已保存的方法和保存原始数据；本项目负责命令调度、安全校验、结果解析、结果关联和审计。Spectrum 可以使用 LabSolutions 预设导出，Photometric 则直接解析已保存的 `.vphd`。

```text
Python -> SPC_CMD.txt -> LabSolutions UV-Vis -> Shimadzu UV-Vis
Python <- SPC_RES.txt <- LabSolutions UV-Vis
Python <- CSV/TXT/XLSX automatic export <- LabSolutions UV-Vis
Python <- .vphd OLE data <- LabSolutions UV-Vis
```

> 当前代码、单元测试和文件交换模拟器已经通过验收。真实岛津仪器上的方法参数、附件行为、扫描耗时和自动导出仍需按本文的分级流程在控制电脑上验证。

## 能力与边界

| 功能 | 当前状态 | 说明 |
| --- | --- | --- |
| 单次完整 Spectrum | 已实现 | 加载已验证的 `.vspm`，设置样品信息，执行 `Command=111` |
| 重复完整 Spectrum | 已实现 | 使用单调时钟按 `Command=111` start-to-start 时间间隔调度 |
| Spectrum 参数化方法生成 | 已实现并现场验证 | 从只读模板 Save As 并重新打开读回；支持本机列出的 `0.01-5 nm` 数据间隔 |
| 测量模式预路由 | 已实现 | 在启动 LabSolutions 前按请求能力选择模式；`400-700/10 nm` 转为 Photometric 的 31 个精确离散点 |
| `start/stop/step` 兼容入口 | 已实现 | 旧入口仍只匹配已登记 profile；新 MCP 生成器处理可表示的 Spectrum 请求 |
| Photometric 离散多波长 | 已实现并现场验证 | 单方法最多 10 点；更长列表自动分段、逐个读回并在测量后合并 |
| 四模式 MCP 规划 | 已实现 | Spectrum、Photometric、Quantitation、Time Course 的请求校验、模板选择和命令计划 |
| 多样品 Spectrum/Photometric MCP 执行 | 已实现 | 持久状态机依次校正基线、测量指定的下一个样品并归档数据 |
| 四模式基础方法模板 | 已创建并校验 | D 盘真实 LabSolutions 方法文件，配置登记 SHA-256 完整性校验 |
| Photometric 执行 | 已实现，正在实机验收 | `300/310/311/320/321` 分段执行；直接解析 `.vphd`，生成合并 CSV、JSON、PNG 和最大吸收波长 |
| Quantitation/Time Course 执行 | 尚未开放 | 需要结果结构和保存流程的现场验收 |
| USB、串口或底层仪器 API | 不提供 | 当前岛津集成边界是 LabSolutions 上层文本交换 |

核心安全能力：

- 默认只显示命令计划，加入 `--execute` 或 `-Execute` 才测量
- 原子命令写入和完整工作流跨进程互斥
- 超时后建立恢复标记，禁止状态不明时自动重发
- 每条命令、每次测量和每个序列均生成 JSON 审计记录
- Spectrum 直接解析 `.vspd` 的 X/Y 数据流并严格验证范围、间隔和点数，无法识别时才等待 CSV；Photometric 关闭 `.vphd` 后直接解析并生成标准结果
- 每次序列采集使用唯一 SampleID、`.vspd` 路径和导出匹配模式
- 前一次扫描或导出超时后，在下一次 `Command=111` 之前停止
- 多样品批次持久记录当前状态，严格拒绝乱序 SampleID 和重复数据路径
- 批次命令结果不确定时进入 `RECOVERY_REQUIRED`，不自动重试物理动作

## 波长与时间的含义

### 连续光谱内部

起始波长、终止波长、数据间隔、扫描速度和响应设置位于 LabSolutions `.vspm` 方法中。`Command=111` 不提供这些运行时参数，也没有旧底层控制器中逐波长 `settle_time` 的等价参数。

本项目只登记经过人工核对的方法元数据：

```toml
[scan_profiles.default]
method_file = "D:\\UVVis-Automation\\methods\\growth_scan_300_900.vspm"
start_nm = 300.0
stop_nm = 900.0
step_nm = 1.0

# 只有与 LabSolutions 方法界面核对后才填写。
# scan_speed_nm_per_min = 600.0
```

登记扫描速度后，程序会报告标称时间：

```text
相邻点标称时间 = step_nm * 60 / scan_speed_nm_per_min
标称扫描时间   = abs(stop_nm - start_nm) * 60 / scan_speed_nm_per_min
```

这些估算不包含仪器响应、附件动作、命令处理、保存和自动导出耗时。

### 重复完整光谱

`--interval-seconds` 表示相邻两次 `Command=111` 的目标开始时间差，不是“上一次完成后再等待多少秒”。程序采用单调时钟，记录计划偏移、实际开始偏移和迟到量。

如果前一次测量、保存或导出超过计划时间和允许容差，序列会停止，不发送下一条测量命令。

### 固定波长时间曲线

如果实验需要固定一个或多个波长的高时间分辨率曲线，应在 LabSolutions Time Course 中建立 `.vtmm` 方法。本机程序组件使用 `.vtmm/.vtmd`，自动控制手册示例使用 `.vtcm`；以本机“另存为”的实际格式为准。`Command=400/410/411` 负责加载方法、设置样品和开始测量，采样波长、时间间隔和总时长仍由方法定义。

更完整的说明见 [扫描请求与 profile 解析](docs/profile-resolution.md)、[时间步长控制](docs/time-step-control.md) 和 [波长控制](docs/wavelength-control.md)。

## 控制电脑要求

- Windows PowerShell 5.1 或更高版本
- Python 3.11 或更高版本
- 已安装并授权自动控制功能的 LabSolutions UV-Vis
- 已由操作人员手动验证的 `.vspm` Spectrum 方法
- 已配置的数据目录、自动导出目录和日志目录；命令目录由运行时管理器校验

核心运行时没有第三方 Python 依赖，控制电脑不需要联网安装 Python 包。

## 快速开始

### 1. 获取并初始化

```powershell
git clone https://github.com/chemyibinjiang/shimadzu-uvvis-automation.git
cd shimadzu-uvvis-automation

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\setup-control-pc.ps1 `
  -MethodFile D:\UVVis-Automation\methods\growth_scan_300_900.vspm `
  -ScanStartNm 300 `
  -ScanStopNm 900 `
  -ScanStepNm 1
```

初始化脚本会：

- 创建本地 `.venv`，但不下载第三方依赖
- 创建命令、数据、导出和审计目录
- 生成被 Git 忽略的 `control-pc.toml`
- 保留已有配置，除非显式加入 `-Force`

如果已经在 LabSolutions 中核对扫描速度，可在首次初始化时额外传入 `-ScanSpeedNmPerMinute <数值>`。不要根据仪器型号猜测该值。

### 2. 运行软件模拟验收

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\test-simulator.ps1
```

通过标准：

```text
SIMULATOR ACCEPTANCE PASSED
```

报告位于 `runtime\simulator-acceptance\<时间>\reports`。模拟方法、原始数据和导出文件均带有 `SIMULATED` 标记，不能作为实验数据。

### 3. 配置 LabSolutions

1. 在 LabSolutions UV-Vis 中保存并人工验证 Spectrum 参数文件 `.vspm`。
2. 设置测量后自动导出 CSV、TXT 或 Excel 到 `D:\UVVis-Automation\export`。
3. 让导出文件名包含或以 `SampleID` 开头。
4. 在 `D:\UVVis-Automation\control-pc.toml` 启用 `[runtime].enabled = true`。
   MCP 会在启动批次时自动启动 Spectrum、设置 `Tools -> Customize -> Automatic Control`
   的命令目录、进入 `Instrument -> Automatic Control`，并用 Hello 验证。
5. 运行时管理器使用 Windows 控件 API，不需要 Computer Use；如果目录、Waiting 或 Hello
   任一项失败，MCP 不会发送基线或测量命令。

官方手册默认命令目录为 `C:\UVVisControl`，本项目运行目录为：

```text
D:\UVVis-Automation\control
```

低级人工验收仍可在 LabSolutions 中检查：

```text
Tools -> Customize -> Automatic Control
Instrument -> Automatic Control
```

但生产批次由运行时管理器执行上述步骤，不要求操作人员每次点击。

推荐目录：

```text
D:\UVVis-Automation\control
D:\UVVis-Automation\methods
D:\UVVis-Automation\methods\generated
D:\UVVis-Automation\templates
D:\UVVis-Automation\data
D:\UVVis-Automation\export
D:\UVVis-Automation\logs
```

首次验收建议只使用 ASCII 路径、样品名和 SampleID。

### 4. 分级验证真实通道

先检查文件系统、配置和权限，不发送 LabSolutions 命令：

```powershell
.\scripts\test-live.ps1
```

也可以让运行时管理器完成无动作 Ready 验收：

```powershell
python -m shimadzu_uvvis.cli `
  --config D:\UVVis-Automation\control-pc.toml ensure-ready
```

它只发送 `Command=0`，不会连接仪器、移动附件、校正或测量。

低级手动流程下，LabSolutions 已进入自动控制等待状态后也可以只发送 `Command=0`：

```powershell
.\scripts\test-live.ps1 -Ping
```

`Ping` 不会连接仪器、移动附件、校正或测量。

### 5. 首次单光谱

先查看完整命令计划：

```powershell
.\scripts\run-test-measurement.ps1 `
  -SampleName validation_blank `
  -Profile default
```

确认当前样品池、样品、方法、数据路径和自动导出设置后才执行：

```powershell
.\scripts\run-test-measurement.ps1 `
  -SampleName validation_blank `
  -Profile default `
  -Execute
```

仪器尚未连接时加入 `-Connect`。只有空白已经正确放置并且确认需要校正时才加入 `-AutoCorrection`。

也可以使用旧控制器风格的范围入口：

```powershell
.\scripts\uvvis.ps1 spectrum `
  --start 300 `
  --stop 900 `
  --step 1 `
  --wavelengths 450 520 650 `
  --sample-name validation_sample
```

该命令只会匹配已登记的方法。`--wavelengths` 用于验证目标点位于完整 Spectrum 的范围和数据网格中。

### 6. 生长过程重复光谱

先查看序列计划：

```powershell
.\scripts\run-growth-series.ps1 `
  -SampleName Au_growth `
  -SeriesId Au_growth_20260711_001 `
  -Profile default `
  -Count 10 `
  -IntervalSeconds 90
```

确认每次 SampleID、数据文件、计划时间和安全参数后执行：

```powershell
.\scripts\run-growth-series.ps1 `
  -SampleName Au_growth `
  -SeriesId Au_growth_20260711_001 `
  -Profile default `
  -Count 10 `
  -IntervalSeconds 90 `
  -Execute
```

序列生成 `Au_growth_20260711_001_0001` 到 `..._0010`，每次采集均有独立原始数据、导出匹配和 run manifest。

时间间隔必须明显大于现场测得的完整扫描、保存和导出周期。首次真实序列只运行三次，并保持操作人员在场。

## 安全默认值

首次测量和生长序列包装脚本默认采用：

| 设置 | 默认值 | 含义 |
| --- | --- | --- |
| `MeasurementMode` | `2` | 多联池时只测当前样品池 |
| `Discharge` | `OFF` | 不请求抽吸附件在测量后排出 |
| `correction` | `none` | 不自动执行基线校正或调零 |
| `disconnect` | `false` | 测量后不主动断开仪器 |
| 执行模式 | plan only | 必须显式加入执行开关 |

岛津手册说明，`MeasurementMode=1` 会测量全部已配置样品池；使用抽吸附件时，如果省略 `Discharge`，软件可能按启用处理。因此本项目始终显式发送这些值。

通用 `send` 命令可能移动附件或改变仪器状态，应以当前版本自动控制手册、实验室 SOP 和现场工程师确认结果为准。

## 数据、导出与审计

默认日志目录：

```text
D:\UVVis-Automation\logs
```

| 输出 | 内容 |
| --- | --- |
| `<日期>\..._cmd<N>_<request-id>.json` | 单条命令、参数、反馈、耗时和异常 |
| `runs\<SampleID>.json` | 单次测量、数据路径、时间和导出 SHA-256 |
| `series\<SeriesID>.json` | 序列计划、实际开始偏移、迟到量和各次结果 |
| `D:\UVVis-Automation\data\<SampleID>.vspd` | LabSolutions 原始 Spectrum 数据 |
| CSV/TXT/XLSX | LabSolutions 按预设格式自动导出的结果 |
| `outputs/<batch>/<sample>/result.csv` | AI tutor 使用的标准波长/吸光度点表 |
| `outputs/<batch>/<sample>/result.json` | 点表、最大吸收波长和来源文件 |
| `outputs/<batch>/<sample>/result.png` | 合并后的光谱图 |

Spectrum 批次完成 `Command=111` 后，程序优先直接读取 `.vspd` 的 X/Y 双精度数据流；只有文件结构
无法识别时才等待 LabSolutions 自动导出 CSV。两种来源都必须与方法的波长范围、数据间隔和点数
完全一致。LabSolutions 即使按高波长到低波长保存，标准结果也统一按波长升序输出。缺点、重复点、
越界点或网格外波长会使批次进入 `RECOVERY_REQUIRED`，不会发布不完整谱图。

序列模式要求每次导出匹配模式唯一。推荐配置：

```toml
[export]
directory = "D:\\UVVis-Automation\\export"
pattern = "{sample_id}*.csv"
timeout_seconds = 120.0
stable_seconds = 2.0
```

## 超时与恢复

超时不代表命令一定没有执行。不要立即重发 `Command=111`。

先查看恢复状态：

```powershell
.\scripts\uvvis.ps1 recover
```

确认 LabSolutions 已结束当前动作，并核对命令文件、反馈文件、仪器界面和已生成数据后，先查看清除计划，再执行清除：

```powershell
.\scripts\uvvis.ps1 recover --clear
.\scripts\uvvis.ps1 recover --clear --execute
```

只有没有匹配反馈、但操作人员已经从仪器和 LabSolutions 确认最终状态时，才考虑 `--force`。恢复动作也会写入审计目录。

## 常用命令

```powershell
# 文件系统和配置诊断
.\scripts\uvvis.ps1 doctor --write-check

# Hello 通道测试
.\scripts\uvvis.ps1 ping

# 单次 Spectrum 计划
.\scripts\uvvis.ps1 spectrum --profile default --sample-name sample_01

# 重复 Spectrum 计划
.\scripts\uvvis.ps1 series `
  --profile default `
  --sample-name growth `
  --series-id growth_01 `
  --count 10 `
  --interval-seconds 90

# 通用命令计划，不会立即发送
.\scripts\uvvis.ps1 send 12 CellPosition=3

# 确认后才发送通用命令
.\scripts\uvvis.ps1 send 12 CellPosition=3 --execute
```

## 文档

- [MCP 客户端 TOML 配置示例](mcp-client.example.toml)
- [AI tutor 的 UV-Vis MCP 服务](docs/mcp-server.md)
- [单样品位设备的多样品顺序测量](docs/sample-batches.md)
- [吸光度空白与基线校正](docs/absorbance-correction.md)
- [四种测量模式与方法模板](docs/four-mode-methods.md)
- [LabSolutions 自动控制交接说明](docs/labsolutions-operation.md)
- [设备连接来源记录](docs/vendor-communication-and-manual-notes.md)
- [控制电脑现场验收手册](docs/control-pc-acceptance.md)
- [扫描请求与 LabSolutions profile 解析](docs/profile-resolution.md)
- [波长范围与多波长控制](docs/wavelength-control.md)
- [时间步长、重复光谱与 Time Course](docs/time-step-control.md)
- [版本记录](CHANGELOG.md)

## 开发与打包

运行单元测试：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

运行完整模拟验收：

```powershell
.\scripts\test-simulator.ps1
```

从已提交且工作区干净的版本生成控制电脑 ZIP：

```powershell
.\scripts\build-control-pc-bundle.ps1
```

打包脚本使用 `git archive`，不会包含 `.git`、`.venv`、`runtime`、本机 `control-pc.toml` 或实验数据。

## 项目边界

本项目是 LabSolutions 上层文本交换客户端，不是岛津底层驱动，也不替代仪器联锁、实验室 SOP、校准流程或现场工程师确认。

以下能力必须单独完成真实控制电脑和仪器验收后才能用于实验：

- LabSolutions DB/CS 文件标识和权限
- Photometric 离散多波长方法
- Time Course `.vtmm/.vtmd`（手册版本可能为 `.vtcm`）流程
- 多联池批量测量
- 自动进样、抽吸、排出和清洗附件
- 与反应器或生长系统联动
- 长时间无人值守运行

许可证见 [LICENSE](LICENSE)。
