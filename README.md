# UE5_MH_Render_by_MCP

UE 5.3 的 MetaHuman CSV 批量渲染工具。通过 Unreal Python 和 Movie Render Queue 自动导入 CSV、绑定 Live Link、复用角色与镜头、输出 PNG。每个 CSV 数据行对应一张图片。

当前实现是本机命令行与文件队列，可供 MCP 工具调用；没有安装或注册额外的 MCP 服务器，也不需要开放网络端口。

快速使用三种入口见 [HowToUse.md](HowToUse.md)。所有 `.cmd` 入口都在仓库根目录，图片渲染代码在 `image_render/`，视频合成代码在 `video_render/`。

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
py "C:/Users/openface/Documents/ChatGPT/渲染/UE5_MH_Render_by_MCP/image_render/start_service.py"
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

## 合成视频与配音

`render_video.cmd` 按顺序执行：**渲染 PNG → 用 FFmpeg 合成 MP4 → 完整解码校验 → 删除本视频对应的 PNG**。输出视频为 H.264，有配音时音频为 AAC。每张图片对应视频的一帧，不插帧。`--fps` 是视频播放帧率，与 UE 内部求值帧率无关。

| 参数 | 用法 |
| --- | --- |
| `--fps` | 默认 **`25`**，可以省略；也支持 `30`、`29.97`、`30000/1001` 等正数或分数。应与原始动作和音频使用的帧率一致。 |
| `--wav` | 可选音频路径；省略则输出无声视频。不会自动猜测或加载同目录的音频。 |

从 CSV 渲染图片并自动合成视频，参数与 `render.cmd submit` 相同。下面示例先渲染 25 行，使用默认 25 fps 生成 1 秒视频，成功后删除这 25 张 PNG；去掉 `--count 25` 处理整个 CSV：

```powershell
.\render_video.cmd submit --avatar Stephane --csv "C:\Users\openface\Downloads\infer_test_full_20261018\sample_01\stage1\infer_test_stage1_V00_S2048_I00000690_P1305A_full.csv" --output "C:\Users\openface\Desktop\MH_Renders" --count 25 --wav "C:\Users\openface\Downloads\infer_test_full_20261018\sample_01\stage1\stage1_generated_V00_S2048_I00000690_P1305A_full.wav"
```

此入口会调用同一套 render 渲染流程，自动等待图片任务完成，无需 `--wait`（保留该参数也可以）。每个 CSV 的任务目录中保存 `video.mp4`、`video.json`、`image_cleanup.json` 和原有任务/帧映射记录。记录包含帧率、图片数、音频起点、解码校验结果、清理数量和释放的空间。

批量 CSV 逐个处理：上一个视频校验、清理成功后才提交下一个 CSV，降低临时图片的磁盘占用。任何一步失败即停止后续提交；`--timeout` 是每个 CSV 等待图片渲染的上限。传入一个 `--wav` 时，每个 CSV 都使用这份音频。

直接用已经渲染好的图片合成，不需要打开 Unreal。`encode` 同样会在视频校验成功后删除对应 PNG。其后填写包含 `000000.png` 等最终图片的任务目录，不能填写 `.capture` 临时目录。比如合成之前的完整 Stephane 任务：

```powershell
.\render_video.cmd encode "C:\Users\openface\Desktop\MH_Renders\infer_test_stage1_V00_S2048_I00000690_P1305A_full\redlantern\20261103_213410_76ed66a974" --wav "C:\Users\openface\Downloads\infer_test_full_20261018\sample_01\stage1\stage1_generated_V00_S2048_I00000690_P1305A_full.wav"
```

默认保存到该图片目录下的 `video.mp4`。`encode --output "C:\Users\openface\Desktop\preview.mp4"` 可指定其他 MP4 文件；已有视频不会被覆盖。两个子命令都支持 `--dry-run`，仅检查输入和显示配置，不创建任务、视频或删除图片。

视频长度始终为 `图片数 / fps`。音频过长时截取，过短时补静音，保持所有图片帧。若图片从 CSV 第 N 行开始，音频也从 `N / fps` 秒开始，因此 `--wav` 应提供从 CSV 第 0 行对应时间开始的完整音频。如果音频已经提前裁切，需使用从 `000000.png` 开始编号的独立图片目录。音频与图片的原始时间起点应一致。

图片缺失或任务尚未完成会拒绝合成；音频无法读取会在提交 UE 渲染前报错。视频写出后会完整解码，校验帧数、帧时间和音频；合成或校验失败保留 PNG，可修正参数后用 `encode` 重试。清理仅按本视频的帧清单逐个删除 PNG，不递归删除文件夹，CSV、WAV 和任务记录保留；编码期间被修改的图片会阻止清理。若文件占用导致清理中途失败，会保留视频与剩余 PNG，并记录已删除数量。清理成功后的 `render.cmd status JOB_ID --watch` 会显示视频帧数和 PNG 清理数量。

关闭命令行或等待超时后，UE 图片任务可能继续运行，但不会自动继续视频合成，需要完成后执行 `encode`。`render.cmd` 用于只渲染图片，继续保留 PNG。

本机已检测到 FFmpeg 7.1，无需额外安装。程序从 `PATH` 查找 `ffmpeg.exe`，也支持环境变量 `FFMPEG_EXE` 指定完整路径。视频相关检查实际编码并解码了测试视频，验证帧数、分数帧率、音频起点、裁剪与补静音；另覆盖成功后精确清理、失败保留图片、批量顺序及默认 25 fps，共 32 项检查通过。早先 30 fps 的真实样例在 `runs/video_verification/`；默认 25 fps 的渲染、配音、校验和清理验证在 `runs/video_cleanup_verification/`。

FFmpeg 参数参考：[image2 图片序列与帧率](https://ffmpeg.org/ffmpeg-formats.html#image2-1)、[atrim 音频裁剪](https://ffmpeg.org/ffmpeg-filters.html#atrim)、[apad 补静音](https://ffmpeg.org/ffmpeg-filters.html#apad)。

## 双人视频

`render_video_dual.cmd` 接收两份 CSV、两个 avatar 和两份可选 WAV，复用 render 的队列分别渲染两人的图片，然后合成一个左右并排的视频。默认 **25 fps**。视频完整解码校验通过后，才删除两组对应的 PNG，保留 CSV、WAV、任务及帧映射记录。

| 输入 | 画面位置 | 音频位置 |
| --- | --- | --- |
| `--csv1`、`--avatar1`、`--wav1` | 左边 | 左声道（FL） |
| `--csv2`、`--avatar2`、`--wav2` | 右边 | 右声道（FR） |

默认单人 512×512，两人拼接后为 **1024×512**。每份 WAV 先转单声道，再分别放入立体声的左右声道；不把两人的声音混到同一声道。只提供一份 WAV 时另一侧补静音，两份都不提供时输出无声视频。配音按共同的 `--start-row / fps` 起点截取，过长裁剪、过短补静音。

例如先渲染双方各 25 行，生成 1 秒双人视频。下面命令可在本仓库目录的 PowerShell 中直接运行；去掉 `--count 25` 即处理完整 CSV：

```powershell
.\render_video_dual.cmd --csv1 "C:\Users\openface\Downloads\infer_test_full_20261018\sample_01\stage1\infer_test_stage1_V00_S2048_I00000690_P1305A_full.csv" `
  --avatar1 Bernice `
  --csv2 "C:\Users\openface\Downloads\infer_test_full_20261018\sample_01\partner\partner_gt_arkit.csv" `
  --avatar2 Stephane `
  --wav1 "C:\Users\openface\Downloads\infer_test_full_20261018\sample_01\stage1\stage1_generated_V00_S2048_I00000690_P1305A_full.wav" `
  --wav2 "C:\Users\openface\Downloads\infer_test_full_20261018\sample_01\partner\partner_original.wav" `
  --output "C:\Users\openface\Desktop\MH_Renders_Dual" --count 25
```

可以显式写 `render_video_dual.cmd submit ...`，与省略 `submit` 等效。支持 `--fps`、共同的 `--start-row` / `--count`、每个角色的等待上限 `--timeout` 及只检查配置的 `--dry-run`。会自动等待两人的渲染，不需要另加 `--wait`。

双方按 CSV 行号一一对应，所选行数必须一致；不一致时在渲染前报错，可以用 `--count` 明确选择共同的范围。每次只接受一对 CSV 文件，不会自动配对文件夹。两份样例 CSV 均为 5800 行，完整任务渲染 11600 张临时图片，最终视频为 5800 帧、232 秒（25 fps）。

输出目录为：

```text
输出根目录/Bernice__Stephane/双人任务ID/
  video_dual.mp4
  video_dual.json
  dual_job.json
  video_dual.side1.json
  video_dual.side2.json
  left/CSV文件名/avatar1/图片任务ID/   # 帧映射、图片任务及清理记录
  right/CSV文件名/avatar2/图片任务ID/
```

两人的 PNG 都会保留到双人视频合成、解码校验成功之后，不生成额外的单人视频或拼接 PNG。任一角色渲染失败、视频合成失败或校验失败时保留已有图片。若已经有两组最终 PNG，可以用 `encode` 重试拼接；输入目录需要不同且行范围、图片高度相同：

```powershell
.\render_video_dual.cmd encode --images1 "左边的图片任务目录" --images2 "右边的图片任务目录" --wav1 "左边音频.wav" --wav2 "右边音频.wav" --output "C:\Users\openface\Desktop\dual.mp4"
```

`encode` 也会在校验成功后清理两组 PNG。双人测试使用逐帧变化的颜色和两种不同频率的音频，验证所有帧的左右画面、左右声道分离、单侧静音、分数帧率、裁剪与补静音，以及失败时保留双方图片。相关 FFmpeg 滤镜说明：[hstack](https://ffmpeg.org/ffmpeg-filters.html#hstack)、[join](https://ffmpeg.org/ffmpeg-filters.html#join)。

包含原有功能在内的 41 项检查已通过。真实样例各 25 行也已完成双人渲染，输出 1024×512、25 fps、1 秒立体声 MP4，并清理全部 50 张 PNG，释放约 14.54 MiB。视频、校验记录和预览位于 `runs/dual_verification/Bernice__Stephane/20261103_234347_a6efba129b/`。

## 行与图片的对应方式

CSV 要包含 `Timecode`、`BlendshapeCount`（大小写兼容）和 61 个 ARKit/头部/眼部通道；`idx` 为可选元数据。按列名读取，允许通道顺序不同，拒绝缺列、列数异常及 NaN/Inf。原 CSV 不会被修改，系数不滤波、不缩放、不裁剪。

中间 CSV 将第 N 行安排到 N 秒，剔除不属于动画的 `idx` 列；原时间码保存在映射清单中。导入后的 61 条曲线设为 Constant 插值，并关闭时间抗锯齿与运动模糊，防止相邻行混合。

实测 UE 5.3 的 Live Link 在序列求值后下一次游戏线程更新才应用到角色。直接以 1 fps 渲染会使表情滞后一行。因此内部使用 2 fps 求值，每行保持两个求值帧，只渲染第二个帧，跳过帧仍更新游戏线程。MRQ 输出的帧号 `2*N+1` 在完成后重命名为 CSV 行号 `N`，最终仍是每行一张图。中性／张嘴／闭眼的三行测试已验证这一对应关系。

对外的数据行范围为左闭右开 `[start_row, end_row)`；5800 行的完整范围是 `[0, 5800)`，最后一张是 `005799.png`。`.capture/` 是任务完成前的原始图片暂存位置；失败或取消时保留供排查。

**MRQ 预览中的帧数与 Sub Sample：** 完整 5800 行任务的内部帧区间为 `[1, 11600)`，因此预览会显示 `11599` 个内部帧位置，包括隔帧输出时跳过的帧；实际只保存奇数帧 `1, 3, …, 11599`，合计 5800 张。`Sub Sample: 1 of 8` 表示当前图片的第 1 个子采样，总共 8 个；本配置为空间采样 8、时间采样 1，同一时刻的 8 个空间采样合成一张 PNG，不会输出 8 张，也不是 8 fps。需要看图片完成度时，以命令行的 `图片 已写出/目标数量` 为准。

相关说明见 Epic 官方的 [MRQ 输出设置（output_frame_step）](https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api/class/MoviePipelineOutputSetting?application_version=5.3) 和 [抗锯齿设置（spatial_sample_count / temporal_sample_count）](https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api/class/MoviePipelineAntiAliasingSetting?application_version=5.3)。

这是逐行图片数据集的采样方式，不代表原动作只有 1 fps。要合成为原速度的视频，应按原始数据的真实帧率另行编码。

## 配置与运行状态

根目录的 `render_config.json` 中，`avatars` 管理角色名、别名和 `preset` 场景映射；`presets` 管理各场景的模板、角色标签与相机标签。配置中还包含分辨率和空间采样数。调高空间采样可改善边缘与噪点，也会增加渲染时间。若项目里尚无模板，在 Unreal 中执行 `image_render/setup_templates.py`；它只创建缺少的模板，已有模板不会被覆盖。`source_map` 可以指向已保存的关卡或当前编辑器已加载的未保存关卡。

需要保持 Unreal 编辑器运行，并避免在任务过程中手动开启其他 PIE/渲染任务。取消保留已经写出的图片。UE 5.3 在 MRQ 首镜头尚未初始化时立即取消可能触发引擎断言，因此运行中任务的取消请求会等待首张图片写出再执行；排队中的任务直接取消。关闭 Unreal 后，正在运行的任务不会自动认定为成功，也不会自动重试；服务再次启动会将其标记为 `interrupted`，重新提交剩余的行会生成新任务目录。

本机采样对比测试中，UE 窗口最小化时曾出现远景构图异常；恢复窗口后相同参数恢复正常面部特写。当前运行时请保持 UE 窗口正常显示。状态 JSON 写入已增加 Windows 短暂文件占用重试，避免查看进度时偶发读写冲突将成功渲染误标为失败。

当前后端使用 MRQ；旧版 Sequencer 导出在自动测试中产生黑图，因此未作为正式后端。MRQ 的采样和后期路径可能与原手动导出有差异，不能把两者视为逐像素一致。预览图片保留在 `runs/` 下供对照。

实际验证结果在 `runs/verified/` 和 `runs/verification.json`：真实样例的前 3 行、末 3 行在两套场景下共 12 张；另有两套场景的中性／张嘴／闭眼对齐测试。编辑器重启后自动加载以及延后执行的运行中取消也已验证。2026-11-03 的两项完整渲染任务已完成，Split 与 RedLantern 各输出 5800 张，文件名为 `000000.png` 至 `005799.png`。早期未修正帧偏移的开发图片已移到 `.runtime/earlier-renders/`，不应作为最终结果使用。

角色调度改动通过了 14 项自动检查，并分别以 `--avatar Bernice`、`--avatar Stephane` 实际提交前 3 行：每次只创建一个任务，角色蓝图与场景匹配，各输出 3 张 PNG。记录与图片在 `runs/avatar_dispatch/`，验证清单为 `runs/avatar_dispatch/verification.json`。单文件完整预检为 1 个任务、5800 张；5 个 CSV 的文件夹预检为 5 个任务、29000 张。

2026-11-03 的 Stephane 空间采样对比保存在 `runs/spatial_benchmark_20261103_closeup/compare.html`：同一 CSV 前 3 行，采样 1、2、4、8，各重复 3 轮，共 36 张有效图片。`benchmark.json` 包含计时和参数，`quality_metrics.json` 记录相对 8 次采样的像素差异（8 不是画质真值）。辅助脚本现位于 `tools/`：在根目录运行 `python -m tools.benchmark_spatial --help` 查看对比测试参数，使用 `python -m tools.analyze_spatial <结果目录>` 生成对照页（分析脚本需要 NumPy、Pillow）。当时的 15 项自动检查包括状态文件短暂占用的回归检查。

## 仓库目录

```text
UE5_MH_Render_by_MCP/
  render.cmd                   # 渲染 PNG
  render_video.cmd             # 渲染并合成单人视频
  render_video_dual.cmd        # 渲染并合成双人视频
  render_config.json          # 项目、avatar、场景和渲染配置
  README.md                   # 完整说明
  HowToUse.md                 # 三个入口的简明用法
  image_render/
    __init__.py
    render.py                 # 图片渲染命令与队列提交
    render_common.py          # CSV 校验、暂存及帧映射
    ue_batch.py               # Unreal / MRQ 后端
    start_service.py          # Unreal 内启动监听服务
    setup_templates.py        # 创建关卡模板
  video_render/
    __init__.py
    render_video.py           # 单人视频、校验与 PNG 清理
    render_video_dual.py      # 双人画面和左右声道合成
  tests/                      # CSV、调度、视频及清理检查
  tools/                      # 采样对比和报告生成脚本
  .runtime/                   # 本机任务队列及状态，不提交到 Git
  runs/                       # 本机结果及验证记录，不提交到 Git
```

根目录的 `.cmd` 使用 UE 5.3 自带 Python 调用子目录中的模块，并自动设置导入路径；从其他工作目录通过完整路径调用也可以。配置、任务队列和历史结果路径仍以仓库根目录为基准。MetaThomas 的 `Content/Python/init_unreal.py` 已指向新的 `image_render/start_service.py`。

CSV、调度及视频检查可通过 UE 自带的 Python 运行（视频检查使用本机 FFmpeg）：

```powershell
& "C:\Program Files\Epic Games\UE_5.3\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" -m unittest discover -s tests -t . -v
```
