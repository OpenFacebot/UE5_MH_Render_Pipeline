"""Create comparison figures: python -m tools.analyze_spatial <benchmark directory>."""
import argparse
import html as html_utils
import itertools
import json
from pathlib import Path
from urllib.parse import quote

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    report = json.loads((folder/'benchmark.json').read_text(encoding='utf-8'))
    if len(report['runs']) != 12 or report.get('excluded_from_comparison'):
        raise ValueError('Need all 12 valid measured jobs')
    samples = [1,2,4,8]
    runs = {(r['samples'],r['repetition']): r for r in report['runs']}
    frames = {(s,rep,row): Image.open(Path(runs[s,rep]['output'])/f'{row:06d}.png').convert('RGB')
              for s in samples for rep in [1,2,3] for row in [0,1,2]}
    arrays = {key: np.asarray(img,dtype=np.float32) for key,img in frames.items()}
    quality = {'note':'8 samples is a comparison reference, not ground truth. MAE is mean absolute RGB code-value difference on 0..255 PNG values; it is not an objective quality score.',
               'difference_vs_8':[], 'same_setting_repeat_difference':[]}
    for s in samples:
        diffs = [np.abs(arrays[s,rep,row]-arrays[8,rep,row]) for rep in [1,2,3] for row in [0,1,2]]
        quality['difference_vs_8'].append({'samples':s,
            'mean_full_image_mae':float(np.mean([d.mean() for d in diffs])),
            'mean_face_roi_mae':float(np.mean([d[150:480,100:410].mean() for d in diffs])),
            'pair_count':len(diffs)})
        repeat = [float(np.abs(arrays[s,a,row]-arrays[s,b,row]).mean())
                  for a,b in itertools.combinations([1,2,3],2) for row in [0,1,2]]
        quality['same_setting_repeat_difference'].append({'samples':s,'mean_mae':float(np.mean(repeat)),
                                                         'max_mae':max(repeat),'pair_count':len(repeat)})
    (folder/'quality_metrics.json').write_text(json.dumps(quality,indent=2),encoding='utf-8')
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf',22)
    small = ImageFont.truetype('C:/Windows/Fonts/arial.ttf',17)
    bg = '#111820'
    for row in [0,1,2]:
        canvas = Image.new('RGB',(4*512+5*12,512+80),bg)
        draw = ImageDraw.Draw(canvas)
        for col,s in enumerate(samples):
            x = 12+col*(512+12)
            draw.text((x+8,12),f'Spatial samples: {s}  |  CSV row {row}',font=font,fill='white')
            canvas.paste(frames[s,1,row],(x,58))
        canvas.save(folder/f'comparison_row_{row}.png')
    regions = [('Hair edge', (115,65,243,193)),
               ('Eye / face edge', (105,210,233,338)),
               ('Mouth / beard', (185,345,313,473))]
    canvas = Image.new('RGB',(4*384+5*12,70+len(regions)*(384+40)),bg)
    draw = ImageDraw.Draw(canvas)
    for col,s in enumerate(samples):
        x = 12+col*(384+12)
        draw.text((x+6,10),f'Spatial samples: {s}',font=font,fill='white')
        for i,(name,box) in enumerate(regions):
            y = 55+i*(384+40)
            draw.text((x+6,y),name+' | 3x nearest',font=small,fill='#b8c5cf')
            crop = frames[s,1,0].crop(box).resize((384,384),Image.Resampling.NEAREST)
            canvas.paste(crop,(x,y+30))
    canvas.save(folder/'detail_comparison_row_0.png')
    canvas.crop((0,0,canvas.width,485)).save(folder/'hair_comparison_row_0.png')
    index = [{'samples':s,'round':rep,'row':row,
              'src':quote((Path(runs[s,rep]['output'])/f'{row:06d}.png').relative_to(folder).as_posix())}
             for s in samples for rep in [1,2,3] for row in [0,1,2]]
    timing_rows = ''.join('<tr><td>'+str(r['samples'])+'</td><td>'+
                          f"{r['end_to_end_seconds']['median']:.3f}"+'</td><td>'+
                          f"{r['mrq_pipeline_seconds']['median']:.3f}"+'</td><td>'+
                          f"{r['mrq_pipeline_seconds']['min']:.3f}–{r['mrq_pipeline_seconds']['max']:.3f}"+
                          '</td></tr>' for r in report['summary'])
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stephane 空间采样对比</title><style>
body{margin:0;background:#111820;color:#e6edf3;font:16px system-ui,sans-serif}main{max-width:1160px;margin:auto;padding:24px}
h1{font-size:25px}p{line-height:1.7;color:#b8c5cf}label{display:inline-block;margin:8px 18px 8px 0}select{padding:6px;background:#263543;color:white;border:1px solid #506273;border-radius:5px}
table{border-collapse:collapse;margin:20px 0;width:100%;max-width:780px}td,th{text-align:left;padding:10px;border-bottom:1px solid #354454}
.scroll{overflow:auto;border:1px solid #354454;background:black}#stage{position:relative;width:512px;height:512px}#stage img{display:block;width:512px;height:512px;max-width:none;image-rendering:pixelated}
#overlay{position:absolute;left:0;top:0;width:50%;height:100%;overflow:hidden;border-right:2px solid white;box-sizing:border-box}#slider{width:100%;max-width:800px}
a{color:#8ac7ff}.badges{display:flex;justify-content:space-between;max-width:800px;margin:12px 0}.preview{max-width:100%;height:auto}</style>
<main><h1>Stephane：空间采样 1 / 2 / 4 / 8</h1><p><strong>__ASSESSMENT__</strong></p>
<p>同一 CSV 的前 3 行；RedLantern，512×512，时间采样 1，关闭运动模糊。每档交错重复 3 轮，独立预热不计入统计。原始 PNG 未修改。滑块左右为同一行的两种采样结果，放大使用最近邻，不添加细节。</p>
<label>左侧采样 <select id="left"><option>1</option><option>2</option><option selected>4</option><option>8</option></select></label>
<label>右侧采样 <select id="right"><option>1</option><option>2</option><option>4</option><option selected>8</option></select></label>
<label>CSV 数据行（从 0 开始） <select id="row"><option>0</option><option>1</option><option>2</option></select></label>
<label>重复轮次 <select id="round"><option>1</option><option>2</option><option>3</option></select></label>
<label>显示倍率 <select id="zoom"><option value="1">1×</option><option value="2">2×</option><option value="3">3×</option></select></label>
<div class="badges"><a id="leftLink" target="_blank">左侧原图</a><a id="rightLink" target="_blank">右侧原图</a></div>
<div class="scroll"><div id="stage"><img id="rightImage"><div id="overlay"><img id="leftImage"></div></div></div>
<label>拖动分界线 <input id="slider" type="range" min="0" max="100" value="50"></label>
<h2>耗时：每次任务输出 3 张图</h2><table><tr><th>空间采样</th><th>端到端中位数 / 秒</th><th>MRQ 中位数 / 秒</th><th>MRQ 最小–最大 / 秒</th></tr>__TIMING__</table>
<p>端到端包含导入、准备、渲染与完成校验；MRQ 时间来自引擎日志，包含场景预热及输出，不是纯 GPU 时间。3 张图的任务受固定开销影响较大，不能直接外推为 5800 张的加速比。</p>
<h2>第 0 行的局部对比</h2><a href="detail_comparison_row_0.png"><img class="preview" src="detail_comparison_row_0.png"></a>
<p>每行从左到右为 1、2、4、8 次采样；依次为头发、眼部及脸部边缘、嘴部及胡须。3× 最近邻放大。</p>
<p><a href="benchmark.json">完整计时及参数</a> · <a href="quality_metrics.json">像素差异记录</a> · <a href="comparison_row_0.png">第 0 行全图对比</a> · <a href="comparison_row_1.png">第 1 行全图对比</a> · <a href="comparison_row_2.png">第 2 行全图对比</a></p>
<p>8 次采样仅作为差异比较参考，不是真值。另一次编辑器最小化时的异常远景测试已排除，本页仅包含确认过构图的面部特写。</p></main>
<script>const frames=__DATA__;const $=id=>document.getElementById(id);function update(){
for(const side of ['left','right']){const f=frames.find(x=>x.samples===+$(side).value&&x.round===+$('round').value&&x.row===+$('row').value);$(side+'Image').src=f.src;$(side+'Link').href=f.src;$(side+'Link').textContent=side==='left'?'左侧：'+f.samples+' 次采样原图':'右侧：'+f.samples+' 次采样原图';}
const size=512*+$('zoom').value;$('stage').style.width=size+'px';$('stage').style.height=size+'px';for(const id of ['leftImage','rightImage']){$(id).style.width=size+'px';$(id).style.height=size+'px';} $('overlay').style.width=$('slider').value+'%';}
for(const id of ['left','right','row','round','zoom'])$(id).addEventListener('change',update);$('slider').addEventListener('input',update);update();</script></html>'''
    html = html.replace('__TIMING__',timing_rows).replace('__DATA__',json.dumps(index))
    html = html.replace('__ASSESSMENT__',html_utils.escape(report.get('assessment','')))
    (folder/'compare.html').write_text(html,encoding='utf-8')
    print(json.dumps(quality,indent=2))
    print('Comparison: '+str(folder/'compare.html'))


if __name__ == '__main__':
    main()
