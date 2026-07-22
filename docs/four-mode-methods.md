# 四种测量模式与方法模板

仓库统一支持四种 LabSolutions UV-Vis 测量请求的校验和规划：

| 模式 | 方法文件 | 原始数据 | 自动控制命令 |
| --- | --- | --- | --- |
| Spectrum | `.vspm` | `.vspd` | `100/110/111` |
| Photometric | `.vphm` | `.vphd` | `300/310/311/320/321` |
| Quantitation | `.vqum` | `.vqud` | `200/210/211/220/221` |
| Time Course | `.vtmm` 或 `.vtcm` | `.vtmd` | `400/410/411` |

本机安装的 LabSolutions UV-Vis 1.13 程序组件和 2026-07-16 现场“另存为”对话框均确认
Time Course 方法为 `.vtmm`、数据为 `.vtmd`；自动控制 PDF 示例使用 `.vtcm`。配置解析器
保留两种方法扩展名的版本兼容，但本控制电脑固定使用 `.vtmm`，不手工改扩展名。

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

以下文件已在对应 LabSolutions 程序中创建并保存：

```text
D:\UVVis-Automation\templates\spectrum_absorbance.vspm
D:\UVVis-Automation\templates\photometric_absorbance.vphm
D:\UVVis-Automation\templates\quantitation_absorbance.vqum
D:\UVVis-Automation\templates\time_course_absorbance.vtmm
```

这些文件不能由仓库伪造。模板必须保存真实仪器型号、附件、信号类型、狭缝、响应、扫描速度、
数据处理和导出设置。信号类型不同，例如吸光度、透过率或能量，应登记不同模板，避免只改波长
却沿用错误的纵轴和处理条件。

### 本机验证清单

验证日期：2026-07-16。仪器注册型号：UV-2700 系列；本机名称：UV2700i。

| 模板 | 大小 | SHA-256 |
| --- | ---: | --- |
| `spectrum_absorbance.vspm` | 4096 B | `4494F77E2CC8BA17AF732D081E3AC6DFCE51E7BA573E4D16CF4C6EC521FF3A5B` |
| `photometric_absorbance.vphm` | 4096 B | `20F28BAD2F196E45B405EB14141DF7B36692BB0C62B3373689F5891AD8121071` |
| `quantitation_absorbance.vqum` | 4096 B | `AAA70638906AD43C52D6DF9C7B95A1A08D0D51EF13767D1E7DC634A05DACEAF0` |
| `time_course_absorbance.vtmm` | 4096 B | `F990592927B267528631BC368719391A04460C1C27087AB6DC01D2ADB8C7AFD5` |

对应母版参数：

| 模式 | 已保存的占位参数 |
| --- | --- |
| Spectrum | `800 -> 400 nm`、`0.5 nm`、中速、吸光度 |
| Photometric | `450/520/650 nm`、点测量、吸光度 |
| Quantitation | `520 nm`、线性标准曲线（含截距）、`mg/L`；标准浓度不固定 |
| Time Course | `520 nm`、积分 `0.1 s`、数据间隔 `1 s`、测定时间 `600 s`、吸光度 |

这些参数是生成方法副本的起点，不是所有实验的固定条件。D 盘实际配置登记了上述哈希；MCP
规划时若文件内容与登记值不一致，会返回 `template_sha256_matches=false` 并阻止就绪状态。

## 统一 MCP 请求

推荐让 AI tutor 省略 `mode`，由 `plan_uvvis_measurement` 在任何 LabSolutions 或仪器动作之前自动
选择模式：

| 请求形态 | 自动选择 |
| --- | --- |
| 范围和 Spectrum 支持的数据间隔 | Spectrum |
| 范围和 Spectrum 不支持的数据间隔 | Photometric 精确离散波长列表 |
| 一个或多个离散波长 | Photometric |
| 固定波长、时间间隔和总时长 | Time Course |
| 固定波长且 `measurement_purpose=quantitation` | Quantitation |

例如 `400-700 nm, step 10 nm` 会先路由成 Photometric 的 31 个点。本机 Photometric 编辑器
现场确认单个方法最多登记 10 个波长，因此生成器自动拆成 `10+10+10+1` 四个 `.vphm`，逐个
Save As、重新打开并读回。拆分属于方法文件限制，逻辑测量结果仍是一个 31 点数据集。

显式模式请求仍然支持：

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

`plan_uvvis_measurement` 已实现能力优先的模式路由、四模式请求、模板选择、目标文件命名、路径
就绪检查和命令计划。
它是只读工具，不写 `*_CMD.txt`，不编辑方法，不连接仪器，也不开始测量。

Spectrum 吸光度动态方法生成器已经实现。它通过已验证的 Win32 控件 ID 编辑 LabSolutions 参数、
另存为、重新打开读回，并恢复 Automatic Control；不会直接修改未知 OLE 二进制格式。已确认本机
Spectrum 数据间隔仅支持 `0.01/0.05/0.1/0.2/0.5/1/2/5 nm`。显式 Spectrum 的 10 nm 请求会
被拒绝；自动模式会在启动 LabSolutions 前将其路由为 Photometric 精确离散点。

Photometric 动态生成器已实现并完成现场方法生成验收。Quantitation 和 Time Course 仍只做规划
和模板选择，直到各自参数编辑器、保存和读回流程完成现场验证。

### Photometric 现场生成记录

2026-07-21 在已连接的 UV-2700i 和 LabSolutions UV-Vis 1.13 上验证：第 11 个登录波长会显示
“登录波长为10个”。请求 `400-700 nm, step 10 nm, absorbance` 随后成功生成并读回四个方法：

```text
400,410,...,490 nm   10 点
500,510,...,590 nm   10 点
600,610,...,690 nm   10 点
700 nm                1 点
```

生成结束后 Automatic Control Waiting 和 `Command=0/Return=0` 均通过；过程中未发送
`Command=21` 或 `Command=311`。

### Spectrum 现场生成记录

2026-07-21 在已连接的 UV-2700i 和 LabSolutions UV-Vis 1.13 上完成以下无测量验收：

```text
请求: 400-700 nm, 5 nm, absorbance
LabSolutions 读回: start=700 nm, end=400 nm, interval=5.0 nm, 吸收值
目标: D:\UVVis-Automation\methods\generated\spectrum_400_700_5nm_absorbance.vspm
目标 SHA-256: D01180DAB9A18B6C4DB2D51FD50DFF2B931EB3B2A944612EA5401F0991F32CC4
模板 SHA-256: 4494F77E2CC8BA17AF732D081E3AC6DFCE51E7BA573E4D16CF4C6EC521FF3A5B
结果: Save As、重新打开读回、恢复 Automatic Control Waiting、Command=0/Return=0 均通过
```

该验收没有发送 `Command=21` 或 `Command=111`，所以只证明方法生成和运行时恢复，不证明基线、
实际扫描、原始数据或自动导出链路。
