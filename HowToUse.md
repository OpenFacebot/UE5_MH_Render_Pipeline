# HowToUse

先打开 MetaThomas 项目，等待 Unreal 编辑器就绪；在 PowerShell 中进入仓库根目录，并将下方 `C:\data\...` 替换为实际 CSV/WAV 完整路径。

```powershell
cd "C:\Users\openface\Documents\ChatGPT\渲染\UE5_MH_Render_by_MCP"
```

## render

按指定 avatar 渲染 CSV，每个数据行生成一张 PNG 并保留图片；`Bernice` 使用 Split 场景，`Stephane` 使用 RedLantern 场景。

```powershell
.\render.cmd submit --avatar Bernice `
  --csv "C:\data\motion.csv" `
  --output "C:\Users\openface\Desktop\MH_Renders" --wait
```

## render_video

先渲染图片，再按默认 25 fps 合成单人视频，可用 `--wav` 配音，视频校验成功后自动删除对应 PNG。

```powershell
.\render_video.cmd submit --avatar Stephane `
  --csv "C:\data\motion.csv" `
  --wav "C:\data\voice.wav" `
  --output "C:\Users\openface\Desktop\MH_Renders" --fps 25
```

## render_video_dual

分别渲染两人，以默认 25 fps 合成左右并排的视频：avatar1/wav1 在左侧画面及左声道，avatar2/wav2 在右侧，校验成功后自动删除两组 PNG。

```powershell
.\render_video_dual.cmd `
  --csv1 "C:\data\person1.csv" --avatar1 Bernice --wav1 "C:\data\person1.wav" `
  --csv2 "C:\data\person2.csv" --avatar2 Stephane --wav2 "C:\data\person2.wav" `
  --output "C:\Users\openface\Desktop\MH_Renders_Dual" --fps 25
```

先试跑可在任意渲染命令末尾加 `--count 25`；加 `--dry-run` 则只检查输入。视频的 `--fps 25` 可省略，WAV 参数也可省略。双人任务要求双方所选行数一致。

已有图片合成、进度查询等完整用法见 [README.md](README.md)。
