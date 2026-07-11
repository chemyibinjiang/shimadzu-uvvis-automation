# 岛津 LabSolutions UV-Vis 自动控制交接说明

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
   C:\UVVis-Data\Parameter\growth_scan_300_900.vspm
   ```

2. 在 LabSolutions 中设置测量完成后自动导出，建议固定到：

   ```text
   C:\UVVis-Data\Export
   ```

3. 推荐导出字段包括样品名、样品 ID、测量时间、波长、吸光度/透过率和峰表。
4. 在 `Tools -> Customize -> Automatic Control` 设置命令接收目录。手册默认目录为：

   ```text
   C:\UVVisControl
   ```

5. 打开 `Instrument -> Automatic Control` 并保持窗口运行。

路径和文件名建议先只使用英文字母、数字、下划线和短横线，避免 Unicode 兼容问题。

## 3. 文件名映射

| 模式 | 命令文件 | 反馈文件 |
| --- | --- | --- |
| Spectrum | `SPC_CMD.txt` | `SPC_RES.txt` |
| Quantitation | `QUA_CMD.txt` | `QUA_RES.txt` |
| Photometric | `PHO_CMD.txt` | `PHO_RES.txt` |
| Time Course | `TMC_CMD.txt` | `TMC_RES.txt` |

每个命令文件只能包含一条命令。必须等上一条命令的反馈完成后才能发送下一条。

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
ParameterFileName=C:\UVVis-Data\Parameter\growth_scan_300_900.vspm
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

### 设置样品信息

```text
Command=110
DataFileName=C:\UVVis-Data\Data\run_20260711_001.vspd
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

推荐让样品 ID、LabSolutions 数据文件名和导出文件名使用同一个 run ID，例如：

```text
run_20260711_001
```

这样上层编排系统可以可靠地关联命令、反馈、原始数据和分析结果。

## 7. 与岛津工程师确认的事项

- 当前 LabSolutions UV-Vis 版本是否支持自动控制功能，以及许可证是否已启用
- 当前仪器型号、固件和支持的命令子集
- 命令与反馈文件的实际编码
- `MeasurementMode=1/2` 在当前附件配置中的含义
- 参数文件扩展名和数据文件命名规则
- 自动导出菜单位置、格式、字段、覆盖策略和完成时点
- 多联池、自动进样器、清洗装置的初始化与安全位置
- 断线、测量中止、附件错误和保存失败后的恢复步骤
- 是否允许长期无人值守，以及实验室要求的联锁和人工确认点

## 8. 首次现场验收

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

连续扫描范围和多个目标波长的配置方法见 [LabSolutions 波长范围与多波长控制](wavelength-control.md)。
