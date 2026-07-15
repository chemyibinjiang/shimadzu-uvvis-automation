# 四种测量模式与方法模板

仓库统一支持四种 LabSolutions UV-Vis 测量请求的校验和规划：

| 模式 | 方法文件 | 原始数据 | 自动控制命令 |
| --- | --- | --- | --- |
| Spectrum | `.vspm` | `.vspd` | `100/110/111` |
| Photometric | `.vphm` | `.vphd` | `300/310/311/320/321` |
| Quantitation | `.vqum` | `.vqud` | `200/210/211/220/221` |
| Time Course | `.vtmm` 或 `.vtcm` | `.vtmd` | `400/410/411` |

本机安装的 LabSolutions UV-Vis 1.13 程序组件登记 Time Course 方法为 `.vtmm`、数据为
`.vtmd`；自动控制 PDF 示例使用 `.vtcm`。配置解析器暂时接受两种方法扩展名，现场应以本机
“另存为”对话框实际生成的格式为准，不应手工改扩展名。

## 模板不是文本命令

LabSolutions 的自动控制文本协议可以加载方法并开始测量，但没有公开修改以下参数的命令：

- Spectrum 的起止波长、数据间隔和扫描速度；
- Photometric 的离散波长列表；
- Quantitation 的测定波长、标准点和校准模型；
- Time Course 的测定波长、采样间隔和总时长。

因此，MCP 不能把 `400~700 nm, 1 nm` 直接写进 `SPC_CMD.txt`。当前采用两级文件：

```text
D:\UVVis-Automation\templates\*.方法文件
    由 LabSolutions 创建并人工验证，只读保留

D:\UVVis-Automation\methods\generated\*.方法文件
    按某次结构化请求另存并修改后的独立方法
```

规划器永远不会覆盖模板。目标方法尚不存在时返回
`status=method_generation_required`，并且 `execution_readiness.ready=false`。

## 四个基础模板

需要在对应 LabSolutions 程序中创建并保存：

```text
D:\UVVis-Automation\templates\spectrum_absorbance.vspm
D:\UVVis-Automation\templates\photometric_absorbance.vphm
D:\UVVis-Automation\templates\quantitation_absorbance.vqum
D:\UVVis-Automation\templates\time_course_absorbance.vtmm
```

这些文件不能由仓库伪造。模板必须保存真实仪器型号、附件、信号类型、狭缝、响应、扫描速度、
数据处理和导出设置。信号类型不同，例如吸光度、透过率或能量，应登记不同模板，避免只改波长
却沿用错误的纵轴和处理条件。

## 统一 MCP 请求

Spectrum：

```json
{"mode":"spectrum","start_nm":400,"stop_nm":700,"step_nm":1}
```

Photometric：

```json
{"mode":"photometric","wavelengths_nm":[450,520,650]}
```

Quantitation：

```json
{"mode":"quantitation","wavelength_nm":520}
```

Time Course：

```json
{"mode":"time_course","wavelength_nm":520,"interval_seconds":1,"duration_seconds":600}
```

Quantitation 的标准浓度、样品类型和校准模型仍属于方法及运行批次信息。当前统一规划器只校验
测定波长并选择模板，不会猜测标准曲线。

## 当前边界

`plan_uvvis_measurement` 已实现四模式请求、模板选择、目标文件命名、路径就绪检查和命令计划。
它是只读工具，不写 `*_CMD.txt`，不编辑方法，不连接仪器，也不开始测量。

真实的动态方法生成器尚未实现。下一阶段必须先确定 LabSolutions 是否提供受支持的方法复制/编辑
接口；如果只能通过 GUI 修改，就需要受控的操作员步骤或经过现场验证的 UI 自动化，不能直接按
未知二进制格式改文件字节。
