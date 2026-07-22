# LabSolutions 波长范围与多波长控制

## 结论

可以保留旧 UV-Vis 控制器的 `start / stop / step` 使用方式，但 LabSolutions 的实现原理不同。

旧控制器直接向单色器发送：

```text
swl <wavelength_nm>
```

然后在每个波长读取能量。因此它可以在运行时自由生成任意波长序列。

LabSolutions UV-Vis 自动控制手册中的 Spectrum 命令 `100/110/111` 没有起始波长、终止波长或数据间隔参数。Photometric 命令 `300/310/311` 也没有波长列表参数。波长设置保存在 LabSolutions 方法文件中：

- 连续光谱范围：Spectrum 方法 `.vspm`
- 一个或多个离散波长：Photometric 方法 `.vphm`

因此本项目不会修改或猜测专有方法文件，而是把已人工验证的方法登记为扫描配置，再允许旧式参数精确选择配置。

## 旧控制器与 LabSolutions 的对应关系

| 旧控制器 | LabSolutions 实现 |
| --- | --- |
| `--start` | `.vspm` 中保存的 Spectrum 起始波长 |
| `--stop` | `.vspm` 中保存的 Spectrum 终止波长 |
| `--step` | `.vspm` 中保存的数据间隔，不是文本命令参数 |
| 逐点 `swl` | LabSolutions 执行一次完整 Spectrum 测量 |
| 多个目标波长 | 从完整 Spectrum 中选择目标点，或使用 `.vphm` Photometric 方法 |

扫描方向也属于配置。`300 -> 900 nm` 与 `900 -> 300 nm` 应登记为不同配置，因为在动力学实验中采集时间顺序不同。

## 在 LabSolutions 中建立连续扫描方法

1. 打开 Spectrum 测定程序。
2. 设置测量模式，例如 Absorbance。
3. 设置起始波长和终止波长。
4. 设置数据间隔、扫描速度、带宽和附件。
5. 人工使用空白和已知样品验证方法。
6. 保存为 ASCII 路径下的 `.vspm` 文件，例如：

   ```text
   D:\UVVis-Automation\methods\scan_300_900_1nm.vspm
   ```

7. 在 `control-pc.toml` 中登记完全相同的元数据：

   ```toml
   [scan_profiles.full_300_900_1nm]
   method_file = "D:\\UVVis-Automation\\methods\\scan_300_900_1nm.vspm"
   start_nm = 300.0
   stop_nm = 900.0
   step_nm = 1.0
   ```

这里的数值是方法声明和安全校验信息。本项目无法从专有 `.vspm` 文件中读取这些设置，因此首次现场验收必须人工确认配置与 LabSolutions 界面一致。

如需登记已核对的扫描速度，可加入 `scan_speed_nm_per_min`。相邻波长点标称时间、重复完整光谱和 Time Course 的区别见 [时间步长控制说明](time-step-control.md)。

## 使用配置名称

先只查看计划：

```powershell
.\scripts\uvvis.ps1 spectrum `
  --profile full_300_900_1nm `
  --sample-name validation_sample
```

计划中的 `wavelength_control` 应显示：

```json
{
  "source": "registered_labsolutions_method",
  "profile_verified": true,
  "profile": "full_300_900_1nm",
  "start_nm": 300.0,
  "stop_nm": 900.0,
  "step_nm": 1.0,
  "acquisition": "continuous_spectrum"
}
```

确认后才加入 `--execute`。

## 兼容旧 start/stop/step 参数

也可以使用与旧控制器相同的入口：

```powershell
.\scripts\uvvis.ps1 spectrum `
  --start 300 `
  --stop 900 `
  --step 1 `
  --sample-name validation_sample
```

程序只会选择起点、终点、方向和间隔完全匹配的已登记配置。如果没有匹配项，会列出可用配置并停止，不会退回默认方法。

以下命令会被拒绝，除非另行登记对应 `.vspm`：

```powershell
.\scripts\uvvis.ps1 spectrum `
  --start 310 `
  --stop 900 `
  --step 1 `
  --sample-name validation_sample
```

## 多个目标波长

在完整 Spectrum 中关注多个波长时，可以加入：

```powershell
.\scripts\uvvis.ps1 spectrum `
  --start 300 `
  --stop 900 `
  --step 1 `
  --wavelengths 450 520 650 `
  --sample-name validation_sample
```

程序会确认每个目标点：

- 位于配置范围内
- 落在配置的数据间隔网格上
- 没有重复值

这些目标波长会写入计划和最终 run manifest。当前版本仍采集完整的 `300-900 nm` Spectrum；`--wavelengths` 不会把仪器改成只测三个点，也不会在未知 CSV 格式下自动抽取数值。

例如，在 `step_nm = 1.0` 的配置中，`450.5 nm` 会被拒绝。在 `step_nm = 0.5` 的配置中，该点才有效。

## 真正的离散多波长测量

如果实验只需要 `450/520/650 nm` 三个离散点，应在 LabSolutions Photometric 程序中建立 `.vphm` 方法，并在方法内部设置波长列表。

自动控制手册给出的命令族是：

```text
Command=300  测定准备，加载 .vphm 并设置 .vphd 数据文件
Command=310  设置样品信息
Command=311  执行 Photometric 测量
Command=320  保存数据文件
Command=321  关闭数据文件
```

自动控制手册第 5.13.4 和 5.13.5 节只定义保存和关闭 `.vphd`，没有 CSV 导出命令。本项目在
`320/321` 均返回 `0` 后，直接读取 OLE 容器中的 `Sample Table/Column Data/Axxx.x` 流，生成
标准长表 CSV。只有 `.vphd` 结构无法识别时才等待 LabSolutions 外部导出。

当前仓库尚未开放这个执行流程，原因是仍需在真实控制电脑上确认：

- `.vphm` 中多个波长的结果表结构
- 其他 LabSolutions 版本的 `.vphd` 列流结构是否保持兼容
- 单样品和多样品表的保存行为
- 多联池、抽吸和排出附件的实际动作

在这些项目完成现场验收前，不应把 Spectrum 的 `--wavelengths` 描述成离散 Photometric 控制。

## 建议的配置集合

为常用实验保存少量经过验证的方法，而不是尝试生成任意方法：

```toml
[scan_profiles.uv_200_400_05nm]
method_file = "D:\\UVVis-Automation\\methods\\uv_200_400_05nm.vspm"
start_nm = 200.0
stop_nm = 400.0
step_nm = 0.5

[scan_profiles.visible_400_800_1nm]
method_file = "D:\\UVVis-Automation\\methods\\visible_400_800_1nm.vspm"
start_nm = 400.0
stop_nm = 800.0
step_nm = 1.0

[scan_profiles.growth_900_300_1nm]
method_file = "D:\\UVVis-Automation\\methods\\growth_900_300_1nm.vspm"
start_nm = 900.0
stop_nm = 300.0
step_nm = 1.0
```

每个方法都应单独完成空白、已知样品、数据文件和自动导出验收。

## 与岛津工程师确认

- Spectrum 方法中的“数据间隔”是否等同于导出表的波长间隔
- 扫描方向、扫描速度和响应时间对生长动力学时间分辨率的影响
- 是否存在官方支持的外部方法参数化方式；没有书面确认前不要编辑 `.vspm`
- Photometric 多波长方法的最大点数、顺序和导出格式
- 其他 LabSolutions 版本或信号类型的 `.vphd` 内部列命名
