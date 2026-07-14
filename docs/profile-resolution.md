# 扫描请求与 LabSolutions profile 解析

AI tutor 中的实验步骤会不断变化，例如：

```text
扫描 400 nm 到 700 nm，步长 1 nm
扫描 200 nm 到 800 nm，步长 2 nm
从 700 nm 向 400 nm 扫描，步长 1 nm
```

项目不会为每种范围硬编码分支。`shimadzu_uvvis.profiles` 提供统一的
`ScanProfileRegistry`，负责把结构化扫描请求精确匹配到已经登记并人工验证的
LabSolutions Spectrum 方法文件 `.vspm`。

## MCP 请求契约

AI tutor 或上层语言模型负责把自然语言转换为结构化参数。MCP 工具只接受并校验结构化数据：

```json
{
  "start_nm": 400.0,
  "stop_nm": 700.0,
  "step_nm": 1.0,
  "direction": null,
  "profile_name": null
}
```

- `start_nm`、`stop_nm`：扫描范围的两个边界。MCP 层会归一化为低、高波长。
- `step_nm`：数据间隔，必须大于零，并能整除扫描范围。
- `direction`：可选，取 `ascending` 或 `descending`。教学步骤没有明确方向时传 `null`。
- `profile_name`：可选。已知必须使用某一方法时指定，同时仍会校验其范围和步长。

`400 nm` 在结构化请求中就是数值 `400.0`，不带单位字符串。单位固定为 nm。

## profile 登记

每个 profile 都指向一个真实的 LabSolutions 方法：

```toml
[scan_profiles.visible_400_700_1nm]
method_file = "C:\\UVVis-Data\\Parameter\\visible_400_700_1nm.vspm"
start_nm = 400.0
stop_nm = 700.0
step_nm = 1.0
```

如果实际方法从高波长向低波长扫描，则按方法真实设置登记：

```toml
[scan_profiles.visible_700_400_1nm]
method_file = "C:\\UVVis-Data\\Parameter\\visible_700_400_1nm.vspm"
start_nm = 700.0
stop_nm = 400.0
step_nm = 1.0
```

## 匹配规则

解析器采用精确、安全的匹配规则：

1. 验证波长、步长、数据网格和可选方向。
2. 在全部登记 profile 中匹配相同范围和步长。
3. 请求指定方向时，再匹配 `.vspm` 的真实扫描方向。
4. 唯一匹配时返回 profile 及方法文件路径。
5. 没有匹配时拒绝测量，要求先在 LabSolutions 中创建并登记方法。
6. 多个方法都匹配时拒绝自动选择，要求提供方向或 `profile_name`。

解析器不会编辑或生成 `.vspm`。自动控制手册没有提供通过 `SPC_CMD.txt` 动态设置
Spectrum 起止波长和步长的命令，`Command=111` 只能执行已经加载的方法。

## Python 公共接口

MCP 适配器可直接调用：

```python
from shimadzu_uvvis import resolve_scan_profile

resolved = resolve_scan_profile(
    settings.scan_profiles,
    start_nm=400.0,
    stop_nm=700.0,
    step_nm=1.0,
)

method_file = resolved.profile.method_file
payload = resolved.as_dict()
```

成功结果包含规范化请求、点数、profile 名称、`.vspm` 路径和扫描方向。对于
`400~700 nm、1 nm`，点数为 `301`，因为起点和终点都包含在内。

该模块只完成“请求到方法”的解析。后续执行层仍按以下顺序生成文本交换命令：

```text
Command=100  加载 resolved.profile.method_file
Command=110  设置样品信息和数据文件
Command=111  执行 Spectrum 测量
```
