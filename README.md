# UE5_MH_Render_by_MCP

UE 5.3 的 MetaHuman CSV 批量渲染工具。通过 Unreal Python 和 Movie Render Queue 自动导入 CSV、绑定 Live Link、复用角色与镜头、输出 PNG。每个 CSV 数据行对应一张图片。

当前实现是本机命令行与文件队列，可供 MCP 工具调用；没有安装或注册额外的 MCP 服务器，也不需要开放网络端口。

## 已配置的项目

- 项目：`C:/Users/openface/Desktop/MetaThomas/MetaThomas.uproject`
- Split：Bernice，镜头位于 `(0, 120, 150)`；模板来自编辑器中原先尚未保存的 `MHC_LightingPreset_Split_RT1` 副本，该关卡现已从保留的快照恢复并保存。
- RedLantern：Stephane，镜头位于约 `(0, 120, 170)`；模板来自 `MHC_LightingPreset_RedLantern_RT`。
- 模板保存到 `/Game/MHBatchRender/Templates/`，每次任务在 `/Game/MHBatchRender/Jobs/` 生成独立副本。
- 默认输出：512×512 PNG，每个 CSV 数据行一张图；内部 2 fps 求值、隔帧输出，50 帧引擎预热、32 帧渲染预热、8 个空间采样。

两套模板保留各自的角色、灯光、镜头和后期配置。当前配置不把它们替换为同一个人物。模板是创建时的快照；以后修改原关卡不会自动更新这些快照。

## 使用

打开 MetaThomas 项目，等待 Unreal 编辑器就绪。已在该项目的 `Content/Python/init_unreal.py` 添加自动入口；首次部署到另一台机器时，可在 Unreal 底部的 **Cmd** 输入框执行：

```text
py "C:/Users/openface/Documents/ChatGPT/渲染/UE5_MH_Render_by_MCP/start_service.py"
```

每次提交必须用 `--avatar` 选择一个角色，由角色名决定使用哪个场景；不会默认渲染两套场景。不区分大小写，也接受蓝图名称：

| `--avatar` | 也接受 | 场景 |
| --- | --- | --- |
| `Bernice` | `BP_Bernice` | `MHC_LightingPreset_Split_RT1` 的 Split 模板 |
| `Stephane` | `BP_Stephane` | `MHC_LightingPreset_RedLantern_RT` 的 RedLantern 模板 |

在本仓库目录的 PowerShell 中先用 Bernice 试渲染前 3 行，共 3 张：

```powershell
.\render.cmd submit --avatar Bernice --csv "C:\Users\openface\Downloads\infer_test_full_20261018\sample_01\stage1\infer_test_stage1_V00_S2048_I00000690_P1305A_full.csv" --output "C:\Users\openface\Desktop\MH_Renders" --count 3 --wait
```

去掉 `--count 3` 即渲染全部 5800 行，所选角色共 5800 张。把 `Bernice` 改成 `Stephane` 即使用 Stephane 和 RedLantern 场景。若需要两个角色的结果，分别提交两次。

文件夹批量处理，以及从指定数据行开始：

```powershell
.\render.cmd submit --avatar Bernice --csv "C:\Users\openface\Downloads\infer_test_20261020" --output "C:\Users\openface\Desktop\MH_Renders" --dry-run
.\render.cmd submit --avatar Bernice --csv "C:\Users\openface\Downloads\infer_test_20261020" --output "C:\Users\openface\Desktop\MH_Renders" --wait
.\render.cmd submit --avatar Stephane --csv "C:\Users\openface\Downloads\infer_test_full_20261018\sample_01\stage1\infer_test_stage1_V00_S2048_I00000690_P1305A_full.csv" --output "C:\Users\openface\Desktop\MH_Renders" --start-row 5797 --count 3 --wait
.\render.cmd status
.\render.cmd status JOB_ID
.\render.cmd status JOB_ID --watch
.\render.cmd cancel JOB_ID
```

`--csv` 支持文件或递归文件夹，可重复传入。`--pattern "*_scaling.csv"` 可筛选文件夹中的文件。`--dry-run` 仅验证输入并计算图片总数。数据行编号从 0 开始，不包含表头。未指定 `--wait` 时，提交后立即返回，Unreal 在后台依次处理；关闭命令行不会取消任务。

`--wait` 或 `status JOB_ID --watch` 每 10 秒显示一次实际已写出的图片数量，例如 `[Bernice] rendering | 图片 100/5800 (1.72%)`。运行中同时计数 `.capture/` 中的图片和已整理的最终图片，按 CSV 行去重；完成后还会校验文件名、数量及尺寸。旧任务也支持这一进度查看方式。旧的 `--presets` 参数已移除，遗漏角色名或传入未知角色会报错，不会自动创建两个任务。

每次任务使用单独的输出目录，已有结果不会被覆盖：

```text
输出根目录/CSV文件名/Bernice或Stephane/任务ID/
  000000.png
  000001.png
  ...
  frame_mapping.json
  job.json
```

`frame_mapping.json` 记录 PNG 文件名、原始 CSV 数据行号、文本行号、`idx` 和原始时间码。`job.json` 记录输入 SHA-256、角色与相机、生成的 Unreal 资产、状态及失败原因。完成回调会验证 PNG 文件名集合、数量和尺寸。

已有任务继续使用原来的 `split` / `redlantern` 目录，不会移动或重命名已有结果。

## 行与图片的对应方式

CSV 要包含 `Timecode`、`BlendshapeCount`（大小写兼容）和 61 个 ARKit/头部/眼部通道；`idx` 为可选元数据。按列名读取，允许通道顺序不同，拒绝缺列、列数异常及 NaN/Inf。原 CSV 不会被修改，系数不滤波、不缩放、不裁剪。

中间 CSV 将第 N 行安排到 N 秒，剔除不属于动画的 `idx` 列；原时间码保存在映射清单中。导入后的 61 条曲线设为 Constant 插值，并关闭时间抗锯齿与运动模糊，防止相邻行混合。

实测 UE 5.3 的 Live Link 在序列求值后下一次游戏线程更新才应用到角色。直接以 1 fps 渲染会使表情滞后一行。因此内部使用 2 fps 求值，每行保持两个求值帧，只渲染第二个帧，跳过帧仍更新游戏线程。MRQ 输出的帧号 `2*N+1` 在完成后重命名为 CSV 行号 `N`，最终仍是每行一张图。中性／张嘴／闭眼的三行测试已验证这一对应关系。

对外的数据行范围为左闭右开 `[start_row, end_row)`；5800 行的完整范围是 `[0, 5800)`，最后一张是 `005799.png`。`.capture/` 是任务完成前的原始图片暂存位置；失败或取消时保留供排查。

**MRQ 预览中的帧数与 Sub Sample：** 完整 5800 行任务的内部帧区间为 `[1, 11600)`，因此预览会显示 `11599` 个内部帧位置，包括隔帧输出时跳过的帧；实际只保存奇数帧 `1, 3, …, 11599`，合计 5800 张。`Sub Sample: 1 of 8` 表示当前图片的第 1 个子采样，总共 8 个；本配置为空间采样 8、时间采样 1，同一时刻的 8 个空间采样合成一张 PNG，不会输出 8 张，也不是 8 fps。需要看图片完成度时，以命令行的 `图片 已写出/目标数量` 为准。

相关说明见 Epic 官方的 [MRQ 输出设置（output_frame_step）](https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api/class/MoviePipelineOutputSetting?application_version=5.3) 和 [抗锯齿设置（spatial_sample_count / temporal_sample_count）](https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api/class/MoviePipelineAntiAliasingSetting?application_version=5.3)。

这是逐行图片数据集的采样方式，不代表原动作只有 1 fps。要合成为原速度的视频，应按原始数据的真实帧率另行编码。

## 配置与运行状态

`render_config.json` 的 `avatars` 管理角色名、别名和 `preset` 场景映射；`presets` 管理各场景的模板、角色标签与相机标签。配置中还包含分辨率和空间采样数。调高空间采样可改善边缘与噪点，也会增加渲染时间。若项目里尚无模板，在 Unreal 中执行 `setup_templates.py`；它只创建缺少的模板，已有模板不会被覆盖。`source_map` 可以指向已保存的关卡或当前编辑器已加载的未保存关卡。

需要保持 Unreal 编辑器运行，并避免在任务过程中手动开启其他 PIE/渲染任务。取消保留已经写出的图片。UE 5.3 在 MRQ 首镜头尚未初始化时立即取消可能触发引擎断言，因此运行中任务的取消请求会等待首张图片写出再执行；排队中的任务直接取消。关闭 Unreal 后，正在运行的任务不会自动认定为成功，也不会自动重试；服务再次启动会将其标记为 `interrupted`，重新提交剩余的行会生成新任务目录。

当前后端使用 MRQ；旧版 Sequencer 导出在自动测试中产生黑图，因此未作为正式后端。MRQ 的采样和后期路径可能与原手动导出有差异，不能把两者视为逐像素一致。预览图片保留在 `runs/` 下供对照。

实际验证结果在 `runs/verified/` 和 `runs/verification.json`：真实样例的前 3 行、末 3 行在两套场景下共 12 张；另有两套场景的中性／张嘴／闭眼对齐测试。编辑器重启后自动加载以及延后执行的运行中取消也已验证。2026-11-03 的两项完整渲染任务已完成，Split 与 RedLantern 各输出 5800 张，文件名为 `000000.png` 至 `005799.png`。早期未修正帧偏移的开发图片已移到 `.runtime/earlier-renders/`，不应作为最终结果使用。

角色调度改动通过了 14 项自动检查，并分别以 `--avatar Bernice`、`--avatar Stephane` 实际提交前 3 行：每次只创建一个任务，角色蓝图与场景匹配，各输出 3 张 PNG。记录与图片在 `runs/avatar_dispatch/`，验证清单为 `runs/avatar_dispatch/verification.json`。单文件完整预检为 1 个任务、5800 张；5 个 CSV 的文件夹预检为 5 个任务、29000 张。

## 文件

- `render.cmd` / `render.py`：外部命令行入口，使用随 UE 5.3 安装的 Python，无需 pip 安装依赖。
- `render_common.py`：CSV 校验、暂存、映射与文件协议。
- `start_service.py` / `ue_batch.py`：Unreal 内的任务监听和 MRQ 执行。
- `setup_templates.py`：创建独立的关卡模板快照。
- `.runtime/`：本机命令、状态和暂存数据，不提交到 Git。
- `runs/`：本机输出和验证结果，不提交到 Git。

CSV 边界检查可通过 UE 自带的 Python 运行：

```powershell
& "C:\Program Files\Epic Games\UE_5.3\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" -m unittest test_render_common -v
```
