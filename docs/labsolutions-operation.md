# 岛津 LabSolutions UV-Vis 自动控制交接说明

## 0. 来源与结论

本说明基于岛津《LabSolutions UV-Vis 自动控制功能说明书》、2026-07-01 至 2026-07-02 厂商沟通记录，以及本项目当前实现整理。详细来源、PDF 哈希和页码依据见 [岛津 UV-Vis 连接方式来源记录](vendor-communication-and-manual-notes.md)。

微信目录中的外部文件 `shimadzu_labsolutions_uvvis_operation.md` 与本文件不相同：外部文件是英文长版操作说明，本文件是结合本仓库脚本和现场验收流程整理后的中文交接说明。

## 1. 集成方式

岛津目前提供的是 LabSolutions UV-Vis 上层文本交换，而不是仪器底层 API。自动化程序与 LabSolutions 交换命令和反馈文件，LabSolutions 继续负责连接仪器、执行方法和保存数据。

```text
上层程序写命令文件
        |
        v
LabSolutions 执行一条命令 -> 岛津 UV-Vis
        |
        +-> 写反馈文件
        +-> 按软件内预设导出 CSV/TXT/Excel
```

## 2. 现场需要先配置

1. 在 LabSolutions UV-Vis 中建立并保存测量参数文件，例如：

   ```text
   D:\UVVis-Automation\methods\growth_scan_300_900.vspm
   ```

2. 在 LabSolutions 中设置测量完成后自动导出，建议固定到：

   ```text
   D:\UVVis-Automation\export
   ```

3. 推荐导出字段包括样品名、样品 ID、测量时间、波长、吸光度/透过率和峰表。
4. 在 `Tools -> Customize -> Automatic Control` 设置命令接收目录。官方手册第 2.1 节给出的默认目录是：

   ```text
   C:\UVVisControl
   ```

   本项目运行配置使用 `D:\UVVis-Automation\control`，因此必须在 LabSolutions 中显式修改，
   并在退出自动控制后重新进入，使设置生效。不能因为自动控制窗口显示“正在待机”就假定
   两边目录一致。

5. 打开 `Instrument -> Automatic Control` 并保持窗口运行。

官方手册要求用户在外部控制开始前启动测定程序并切换到自动控制模式，文本交换协议本身
没有“进入自动控制”的命令。本项目的 `LabSolutionsRuntimeManager` 在同一 Windows 登录会话
中启动 `UVNavi.exe /APP:Spectrum`，按窗口菜单文字定位
`Instrument -> Automatic Control`，并通过 Win32/UI Automation 触发。不要使用截图坐标。
运行时管理器只有在窗口显示等待状态且 `Command=0` 返回 `Return=0` 后才能报告 `READY`；
任一条件失败时不得继续发送连接、校正或测量命令。

加载新方法后，LabSolutions 可能弹出“已更改参数文件。是否执行基线校正？”对话框。该提示不作为
学生确认门禁，也不允许人工点击。运行时管理器按进程、对话框类别、提示文本以及“是/否”控件 ID
严格识别，并通过 Win32 消息自动选择“否”。真正的空白基线校正只在学生确认空白已放好后由
`Command=21, CorrectionType=1` 执行。该流程不使用 Computer Use 或截图坐标。

可独立执行无动作验收：

```powershell
python -m shimadzu_uvvis.cli `
  --config D:\UVVis-Automation\control-pc.toml ensure-ready
```

手册说明命令与反馈文件使用 UTF-8，但同时说明 LabSolutions UV-Vis 不支持 Unicode 文本输入。现场首次验收应按保守规则执行：路径、样品名、SampleID、数据文件名和导出文件名只使用英文字母、数字、下划线、点号和短横线，数值只使用半角数字。

## 3. 文件名映射

| 模式 | 命令文件 | 反馈文件 |
| --- | --- | --- |
| Spectrum | `SPC_CMD.txt` | `SPC_RES.txt` |
| Quantitation | `QUA_CMD.txt` | `QUA_RES.txt` |
| Photometric | `PHO_CMD.txt` | `PHO_RES.txt` |
| Time Course | `TMC_CMD.txt` | `TMC_RES.txt` |

每个命令文件只能包含一条命令。必须等上一条命令的反馈完成后才能发送下一条。

LabSolutions 读取命令文件后会删除命令文件，再执行命令并写反馈文件。本项目因此采用临时文件写入后原子替换为 `SPC_CMD.txt` 的方式，避免 LabSolutions 读到半写入文件。若 `SPC_CMD.txt` 长时间未消失，通常表示 LabSolutions 没有在监听当前命令目录；若命令文件已消失但没有反馈，通常表示 LabSolutions 已读取命令但执行或反馈写回异常。

## 4. Spectrum 最小流程

### Hello

```text
Command=0
```

用于确认 LabSolutions 正处于自动控制状态。

### 连接仪器

```text
Command=1
```

### 加载方法

```text
Command=100
ParameterFileName=D:\UVVis-Automation\methods\growth_scan_300_900.vspm
```

### 校正

按方法自动校正：

```text
Command=21
CorrectionType=1
```

指定范围基线校正：

```text
Command=21
CorrectionType=2
StartWL=300.0
EndWL=900.0
```

单波长调零：

```text
Command=21
CorrectionType=3
WL=500.0
```

对于吸光度溶液测量，优先在样品位和参比位放置合适的空白溶液后使用
`CorrectionType=1`。不要在上层程序中把导出吸光度再手工减暗电流或空气能量。完整 SOP 见
[吸光度空白与基线校正](absorbance-correction.md)。

### 设置样品信息

```text
Command=110
DataFileName=D:\UVVis-Automation\data\run_20260711_001.vspd
SampleName=Au_growth_01
SampleID=run_20260711_001
```

### 执行 Spectrum 测量

```text
Command=111
MeasurementMode=2
Discharge=OFF
```

`MeasurementMode=1` 表示多联池时测量全部已配置样品池，`MeasurementMode=2` 表示只测当前样品池。岛津手册还说明，使用抽吸附件时如果省略 `Discharge`，该参数可能按启用处理。因此本项目首次测试始终显式使用 `MeasurementMode=2` 和 `Discharge=OFF`。

### 断开仪器

```text
Command=2
```

正常反馈格式：

```text
Command=111
Return=0
Error=""
```

`Return` 为负数时应停止当前流程，记录完整反馈，并由操作人员判断是否重试。不要对测量和附件移动命令进行无限自动重试。

## 5. 常用命令号

| 命令 | 作用 |
| ---: | --- |
| 0 | Hello/检查自动控制 |
| 1 | 连接仪器 |
| 2 | 断开仪器 |
| 11 | 初始化附件 |
| 12 | 移动样品池位置 |
| 13 | 清洗 |
| 21 | 调零或基线校正 |
| 100 | 设置 Spectrum 参数文件 |
| 110 | 设置 Spectrum 样品信息 |
| 111 | 执行 Spectrum 测量 |
| 190 | 退出 Spectrum 程序 |

完整参数和错误码应以对应版本的《LabSolutions UV-Vis 自动控制功能说明书》为准。

## 6. CSV/TXT/Excel 输出

文本交换协议负责触发测量；结果格式和自动导出位置需要提前在 LabSolutions 中设置。本项目在 `Command=111` 成功后监控导出目录，只有文件大小和修改时间持续稳定一段时间才返回，避免解析仍在写入的文件。

Spectrum 原始文件稳定后，执行器优先读取 `.vspd` 中的 X/Y 双精度数据流；结构无法识别时才等待
自动导出 CSV。结果的波长范围、数据间隔和点数必须与已加载 `.vspm` 完全一致，然后在样品目录生成
`result.csv`、`result.json` 和 `result.png`，并同步发布到仓库 `outputs/<batch>/<sample>`。原始顺序
可以是升序或降序，标准结果统一为升序；缺点、重复点、越界点和网格外波长均拒绝发布。

自动控制手册中 Spectrum 测定的主数据文件是 `.vspd`。CSV、TXT 或 Excel 不是通过一条自动控制 `EXPORT CSV` 命令生成，而是由 LabSolutions 的自动输出设置在测定完成后生成。因此现场必须提前确认自动输出菜单位置、格式、字段、命名规则、覆盖策略和完成时点。

推荐让样品 ID、LabSolutions 数据文件名和导出文件名使用同一个 run ID，例如：

```text
run_20260711_001
```

这样上层编排系统可以可靠地关联命令、反馈、原始数据和分析结果。

## 7. 波长与时间步长

LabSolutions Spectrum 的 `Command=111` 不接收起始波长、终止波长、数据间隔、扫描速度或每点等待时间。这些参数必须先保存在 `.vspm` 方法中；上层程序只能加载经过验证的方法。

生长实验需要重复完整光谱时，使用 `scripts\run-growth-series.ps1` 设置采集次数和相邻 `Command=111` 的 start-to-start 时间间隔。固定一个或多个波长的高时间分辨率曲线则应使用 LabSolutions Time Course 方法；本机 1.13 已确认方法扩展名为 `.vtmm`，其中采样间隔和总时长仍由方法定义。

详细配置、公式、命令示例和超时处置见 [LabSolutions UV-Vis 时间步长控制](time-step-control.md)。

## 8. 与岛津工程师确认的事项

- 当前 LabSolutions UV-Vis 版本是否支持自动控制功能，以及许可证是否已启用
- 当前仪器型号、固件和支持的命令子集
- 命令与反馈文件的实际编码
- `MeasurementMode=1/2` 在当前附件配置中的含义
- 参数文件扩展名和数据文件命名规则
- 自动导出菜单位置、格式、字段、覆盖策略和完成时点
- 多联池、自动进样器、清洗装置的初始化与安全位置
- 断线、测量中止、附件错误和保存失败后的恢复步骤
- 是否允许长期无人值守，以及实验室要求的联锁和人工确认点

## 9. 首次现场验收

1. 运行 `scripts\test-simulator.ps1` 完成软件侧端到端测试。
2. 运行 `scripts\test-live.ps1`，只检查路径和权限。
3. LabSolutions 进入自动控制模式后运行 `scripts\test-live.ps1 -Ping`。
4. 加载已由操作人员手动验证的方法文件。
5. 使用 `scripts\run-test-measurement.ps1` 查看首次测量计划。
6. 放置正确样品后加入 `-Execute`，只测当前样品池且不排出。
7. 人工对照 LabSolutions 界面、`.vspd` 原始数据、导出文件和 run manifest。
8. 检查 run ID、单位、小数点、编码、列顺序和文件完成判定。
9. 制造一次可控错误，确认上层程序会停止并保留反馈。
10. 验收完成后再接入自动进样、反应器或生长实验编排。

连续扫描范围和多个目标波长的配置方法见 [LabSolutions 波长范围与多波长控制](wavelength-control.md)。重复完整光谱、扫描速度和 Time Course 边界见 [LabSolutions UV-Vis 时间步长控制](time-step-control.md)。
