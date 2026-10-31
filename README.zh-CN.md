[EN](README.md) | [中文](README.zh-CN.md)

# comfyui-lesca-nodes

Lesca 的自定义 ComfyUI 节点。

目前提供 **Load Video (Lesca)**、**Save Video (Lesca)**、**Video Info
(Lesca)**、**Video to Images (Lesca)**、**Video Combine (Lesca)**、
**Video Add Noise (Lesca)** 和 **Context Clips (Lesca)** 七个节点。

## 节点功能一览

| 节点 | 功能 | 关键能力 |
|------|------|---------|
| [Load Video (Lesca)](#load-video-lesca) | 按显式文件路径加载视频，输出与内置 `LoadVideo` 一致 | 任意路径（相对 ComfyUI 根目录或绝对路径）；无需运行节点即可实时预览；文件缺失时输出确定的白色占位视频 |
| [Save Video (Lesca)](#save-video-lesca) | 把内置 `Create Video` + `Save Video` 合并为一个节点 | 用户指定保存路径（无 `_000001_` 计数器后缀）；帧 + 音频裁剪；裁剪后的 `images` / `audio` 透传为输出 |
| [Video Info (Lesca)](#video-info-lesca) | 以独立输出报告视频各项属性，供工作流分支使用 | `fps` / `bit_depth` / `color_space` / `width` / `height` / `frame_count`，外加逐行输出的 `video_info` 字符串；输出节点，可单独运行 |
| [Video to Images (Lesca)](#video-to-images-lesca) | 把视频解码成一批图片帧 | force-rate → trim → frame-cap → resize 管线；窗口化解码（不全量解码）；音频区间随导出帧同步裁剪 |
| [Video Combine (Lesca)](#video-combine-lesca) | 把目录中所有同前缀的视频文件合并成一个 | 任意上游输出触发；流拷贝合并、不兼容时重编码兜底；自动排除已合并产物；按源文件指纹做执行缓存 |
| [Video Add Noise (Lesca)](#video-add-noise-lesca) | 截取头部/尾部一段帧，添加互相独立的逐帧噪声 | 分层强度调度（`frame_num_list` / `strenth_list`）；可选 seed 复现逐帧噪声；短视频钳制 |
| [Context Clips (Lesca)](#context-clips-lesca) | 以纯元数据算术为目标视频规划片段调度 | 由 `frame_count` + `fps` 算出 `target_frame_count` / `total_clips` / `last_clip_frames`；`target_fps = 0` 保持源帧率；输出节点，可单独运行 |

## Load Video (Lesca)

ComfyUI 自带的视频加载节点（Config UI）只能从一个固定目录里挑选视频，无法指定
显式路径。本节点提供 `Load Video (Lesca)`：用一个直接输入的文件路径加载视频，
输出与 ComfyUI 自带的 `LoadVideo` 节点一致。

### 输入

| 字段 | 类型 | 说明 |
|------|------|------|
| `filename` | `String` | 要加载的视频文件**路径**，例如 `input/video/clip.mp4`。相对路径基于 ComfyUI **根目录**解析（比 `input` 高一级、存放 Config UI 源文件的那层目录），因此 `input/clip.mp4` 表示 `<root>/input/clip.mp4`，`output/clip.mp4` 表示 `<root>/output/clip.mp4`。绝对路径原样使用。逃出根目录的相对路径会被拒绝。 |

### 输出

| 字段 | 类型 | 说明 |
|------|------|------|
| `video` | `Video` | 加载得到的视频——与 ComfyUI 自带 `LoadVideo` 相同的输出类型。 |

### 实时预览

`filename` 字段下方有一个 **Update** 按钮（本节点自带一个小型自定义前端
`web/extension.js`）。点击它——或者直接编辑文件名——视频就会显示为节点上的
预览，**无需运行节点**。上游节点传入 `filename` 参数时，该按钮无效。

由于本节点接受任意路径（包括绝对路径），而 ComfyUI 内置的 `/view` 端点无法
服务这些路径，预览改由自定义后端端点 `/lesca/load_video/preview` 流式输出：
它复用与节点 `execute` 完全相同的路径解析逻辑，并支持 HTTP Range（拖动 seek）。
预览加载失败（文件不存在或不是视频）时，节点上会直接显示错误信息。当
`filename` 连接到字符串产出节点时预览同样可用——前端读取的是*源*节点的 widget
值，而不是本节点上那个（可能过期的）值。

### 文件缺失时

当 `filename` 指向一个**不存在**的路径时，节点**不会**让整个运行报错。
`execute` 会输出一个确定的 16×16、24 帧、纯白色占位视频，工作流继续运行，
下游每个节点都能拿到一个有效的 `Video`。空路径、逃出 ComfyUI 根目录的相对
路径、以及无法识别为视频的文件仍会照常报错——只有"文件确实不存在"才触发
回退。

`filename` 字段还可以**连接**到任何其它字符串产出节点（例如内置 `Text` 节点、
`PrimitiveString`）。

## Save Video (Lesca)

ComfyUI 内置流程需要两个节点才能把一批图片变成保存好的视频文件：`Create
Video`（图片 → 视频，可带音频）和 `Save Video`（把视频写入 output 目录，文件名
带 `_000001_` 形式的计数器后缀）。`Save Video (Lesca)` 把两者合并为一个节点，
并新增两项两者都不具备的能力：

* **用户指定保存路径** — `filename` 是要保存的视频文件*路径*，例如
  `output/video/clip.mp4`：相对路径基于 ComfyUI **根目录**解析，
  `output/video/clip.mp4` 表示 `<root>/output/video/clip.mp4`；绝对路径原样
  使用。文件就写在给定路径下——**没有 `_000001_` 计数器后缀**。文件名没有
  扩展名时会自动补上，父目录不存在时会自动创建。
* **帧 + 音频裁剪** — `trim_frames` 在编码前丢掉前 N 帧**以及**前 `N / fps`
  秒的音频（与 ComfyUI `TrimAudioDuration` 节点相同的算法）。默认 `0` 表示不
  裁剪。

其余输入照搬两个源节点：`fps`、`bit_depth`、`color_space` 来自 `Create
Video`，`format`（嵌套 `codec` → `encoding` → `crf`）来自 `Save Video`。
`Create Video` 自己的 `codec` 输入被有意省略——输出编码由 `format` 决定。

除保存得到的 `filename` 路径和编码后的 `video` 外，节点还会透传**裁剪后**的
`images` 和 `audio`（实际写进视频的数据），类型分别与对应输入相同，裁剪后的
中间结果可以再接给其它节点。

### 输入

| 字段 | 类型 | 说明 |
|------|------|------|
| `images` | `Image` | 用于创建视频的图片批次（与 `Create Video` 相同）。 |
| `audio` | `Audio` | 要附加的可选音频（与 `Create Video` 相同）。 |
| `filename` | `String` | 要保存的视频文件**路径**，例如 `output/video/clip.mp4`（默认值）。相对路径基于 ComfyUI **根目录**解析；绝对路径原样使用。就保存在该路径下（无计数器后缀）。没有扩展名时自动补上容器扩展名；父目录不存在时自动创建。逃出根目录的相对路径会被拒绝。 |
| `trim_frames` | `Int` | 丢掉前 N 帧以及前 `N / fps` 秒音频（`round(N / fps * sample_rate)` 个采样——与 `TrimAudioDuration` 相同）。默认 `0`。大于等于总帧数时会报错。 |
| `fps` | `Float` | 输出帧率。默认 `30`，范围 `1`–`120`。 |
| `bit_depth` | `Combo` | `auto` / `8` / `10`（默认 `auto`）。auto 对 sRGB 用 8-bit、对 HDR 用 10-bit。 |
| `color_space` | `Combo` | `sRGB` / `HDR` / `HDR PQ`（默认 `sRGB`）。HDR 选择 BT.2020/HLG，HDR PQ 选择 BT.2020/PQ。 |
| `format` | `DynamicCombo` | `auto` / `mp4` / `mkv` / `webm`，各自嵌套 `Codec`（`auto`/`h264`/`av1`；webm 仅 `auto`/`av1`）→ `Encoding`（`auto`/`re-encode`）→ `CRF`。auto 对 H.264 用 MP4、对 AV1 用 WebM。 |

与内置 `Save Video` 相同，保存的文件会内嵌 prompt 和 `extra_pnginfo`
元数据（可用 ComfyUI 的 `--disable-metadata` 关闭）。

### 输出

| 字段 | 类型 | 说明 |
|------|------|------|
| `filename` | `String` | 保存的视频文件绝对路径，例如 `/…/output/video/clip.mp4`。 |
| `video` | `Video` | 保存得到的视频——与 ComfyUI 自带 `Save Video` 相同的输出类型。 |
| `images` | `Image` | 裁剪后的图片——丢掉了前 `trim_frames` 帧。类型与 `images` 输入相同。 |
| `audio` | `Audio` | 裁剪后的音频——丢掉了前 `trim_frames / fps` 秒。类型与 `audio` 输入相同（未提供音频时为 `None`）。 |

## Video Info (Lesca)

以独立输出报告视频的各个属性，方便工作流据此分支（例如选择目标分辨率或重编码
参数）。`video` 输入接受 **Load Video (Lesca)** 的 `video` 输出——或任何其它
视频节点的输出，包括 Save Video (Lesca)。除逐项输出外，还有一个 `video_info`
字符串输出，把所有字段各占一行地列出来（`fps`、`bit_depth`、`color_space`、
`width`、`height`、`frame_count`），用于日志或显示。

该节点为输出节点：即使没有任何下游节点连接，Queue 也会执行它，因此可以单独
使用来查看视频属性。节点自带一个只读文本框，运行后在其中显示 `video_info`
字符串（未运行时显示占位提示）。

### 输入

| 字段 | 类型 | 说明 |
|------|------|------|
| `video` | `Video` | 要检查的视频。 |

### 输出

| 字段 | 类型 | 说明 |
|------|------|------|
| `video_info` | `String` | 其余所有输出字段拼成的一个字符串，每行一个：fps、bit_depth、color_space、width、height、frame_count。 |
| `fps` | `Float` | 视频帧率，例如 `24.0`。 |
| `bit_depth` | `String` | 位深，字符串形式，例如 `"8"` 或 `"10"`。 |
| `color_space` | `String` | `sRGB`、`HDR`、`HDR PQ`，或视频未声明时的 `auto`。 |
| `width` | `Int` | 宽度（像素）。 |
| `height` | `Int` | 高度（像素）。 |
| `frame_count` | `Int` | 视频总帧数。 |

数值取自 ComfyUI 的标准视频访问器（`get_frame_rate` / `get_bit_depth` /
`get_color_space` / `get_dimensions` / `get_frame_count`），因此文件型视频直接
读取容器元数据，不解码帧、不整体载入内存；张量创建的视频（例如来自 Save Video
(Lesca)）则报告其内存属性。

## Video to Images (Lesca)

把视频解码成一批图片帧，可选项包括帧率重采样、头尾裁剪、帧数上限和缩放。
`video` 输入接受 **Load Video (Lesca)** 的 `video` 输出（惰性的
`VideoFromFile`）或任何其它视频节点的输出。

### 输入

| 字段 | 类型 | 说明 |
|------|------|------|
| `video` | `Video` | 要解码的视频。 |
| `frame_cap` | `Int` | 最多导出的帧数。默认 `0` = 全部导出。 |
| `trim_mode` | `Combo` | `head`：丢掉最前面的帧；`tail`：丢掉最末尾的帧。默认 `head`。 |
| `trim_frames` | `Int` | 要丢掉的帧数，在帧率换算之后应用。默认 `0` = 不裁剪。 |
| `force_rate` | `Int` | 强制的输出帧率。默认 `0` = 保持源帧率。`force_rate = 24` 时，30fps 的内容会被重采样成 24fps 的帧序列。 |
| `custom_width` | `Int` | 目标宽度（像素）。默认 `0` = 保持源宽度。 |
| `custom_height` | `Int` | 目标高度（像素）。默认 `0` = 保持源高度。 |

### 输出

| 字段 | 类型 | 说明 |
|------|------|------|
| `images` | `Image` | 解码后的帧（按需缩放）。 |
| `audio` | `Audio` | 导出帧覆盖的时间段对应的音频，即 `[q[0]/S, (q[-1]+1)/S)`，`S` 为源帧率。head 裁剪丢掉被裁帧下的音频前缀；帧数上限同时丢掉未导出帧对应的音频尾部。按源采样率切片，不按输出帧率。无音频轨的视频输出 `None`。 |
| `fps` | `Float` | 导出帧序列的帧率：设置了 `force_rate` 时为 `force_rate`，否则为源帧率。 |
| `video` | `Video` | 重建的视频，与 `images` / `audio` 输出共享同一组张量。导出帧数不在此输出——把该 `video` 接入 **Video Info (Lesca)** 的 `frame_count` 即可获取。 |

### 处理顺序与缩放规则

管线按 **force-rate → trim → frame-cap → resize** 的顺序执行：

* **force-rate** — 内容先被重采样到目标帧率（输出第 `i` 帧取源第
  `round(i · S / T)` 帧，钳制到最后一帧），因此对 30fps 内容设置
  `force_rate = 24` 会导出 24fps 的帧序列。
* **trim** — `trim_mode = head` 丢掉前 `trim_frames` 帧，`tail` 丢掉后
  `trim_frames` 帧。`trim_frames` 必须小于帧率换算后的帧数，否则节点报错。
* **frame-cap** — 在帧率换算与裁剪之后最多保留 `frame_cap` 帧（`0` = 不限）。
* **resize** — `custom_width` / `custom_height`：两者都为 `0` 时保持源分辨率；
  恰好一个为 `0` 时另一个等比例缩放；两者都非 `0` 时精确缩放到 `(W, H)`。

### 窗口化解码

对普通文件输入的 `video`，节点只读取容器元数据（不解码），然后**只解码导出帧
覆盖的时间窗口** `[q[0]/S, (q[-1]+1)/S)`——导出一小时长视频的前 1 秒不再触发
全量解码和随之而来的内存暴涨。无法使用该路径的输入（张量型
`VideoFromComponents` 视频、已带裁剪窗口的输入、元数据中缺少帧率或帧数的输入、
或 pts 怪癖导致窗口解码结果不足）回退到全量解码，并对已加载的张量应用相同的
索引映射；两条路径输出逐像素一致。

## Video Combine (Lesca)

把指定目录中指定前缀的视频文件合并成一个文件，由任意上游输出触发——例如把
Save Video (Lesca) 的 `filename` 输出接到本节点，每保存一个新片段就会自动触发
对其所在目录的合并。

### 输入

| 字段 | 类型 | 说明 |
|------|------|------|
| `any` | `Any`（可选） | 触发输入：接受**任意**上游类型的输出即可启动本节点的运行（例如 `Save Video (Lesca)` 的 `filename`，新片段保存完成即触发合并）。可选——不连线也能单独运行。 |
| `folder` | `String` | 要扫描的目录，例如 `output/video`（默认值）。相对路径基于 ComfyUI **根目录**解析；绝对路径原样使用。可连接到字符串产出节点。 |
| `prefix` | `String` | 要合并的片段文件名前缀，例如 `dance`。可连接到字符串产出节点。 |

### 输出

| 字段 | 类型 | 说明 |
|------|------|------|
| `filename` | `String` | 合并后视频的绝对路径，例如 `/…/output/video/dance_merged-2026_10_18_16_33_44.mp4`。 |

### 行为

* 节点遍历 `folder`，保留所有以 `prefix` 开头的**视频**文件（`.txt` 等非视频
  文件由 ComfyUI 的视频内容类型过滤器跳过），按**文件名升序**排列，合并成一个
  `<prefix>_merged-YYYY_MM_DD_HH_MM_SS.mp4`，保存在同一目录。
* 之前生成的 `<prefix>_merged-…mp4` 会被**排除在扫描之外**，因此对同一目录再次
  运行节点时合并的是原始片段，绝不会合并已经合并过的文件。
* 合并策略：ffmpeg-python 的 concat demuxer 加 `-c copy`，直接拼接数据流、不
  解码（无损且快，适用于同一管线产出的片段）。当流拷贝因输入不兼容（编码、
  分辨率、帧率不一致）而失败时，节点自动回退到 concat filter 重编码为一个
  H.264 流（丢弃音频）。两条命令行均由 ffmpeg-python 包构建——它仍要求系统
  PATH 上有 ffmpeg 二进制。
* 该节点是输出节点：即使没有任何下游节点连接 `filename` 输出，Queue 也会执行
  它。
* 执行缓存以匹配片段的 `(basename, mtime)` 集合指纹为键：新增或修改片段会产出
  新的合并文件，目录无变化时复用缓存输出，不会每次运行都写一个新合并文件。
* 错误处理：目录不存在、不是目录、前缀为空、没有匹配前缀的视频文件、相对路径
  逃出 ComfyUI 根目录，都会抛出带说明的错误。

## Video Add Noise (Lesca)

从视频中截取头部或尾部的一段帧，并为这些帧添加互相独立的逐帧噪声，强度按层
调度。`video` 输入接受 **Load Video (Lesca)** 的 `video` 输出（惰性的
`VideoFromFile`）或任何其它视频节点的输出。

该节点为转换型（非输出）节点：只输出加噪后的 `images`，供下游节点使用。

### 输入

| 字段 | 类型 | 说明 |
|------|------|------|
| `video` | `Video` | 要截取帧的视频，例如直接来自 Load Video (Lesca)。 |
| `mode` | `Combo` | `head`：截取最前面的 N 帧（默认）；`tail`：截取最末尾的 N 帧。 |
| `frame_num_list` | `String` | 每层的帧数，例如 `18 2 1 1`（空白或逗号分隔）——4 层，共 22 帧。 |
| `strenth_list` | `String` | 每层的噪声强度，例如 `0.4 0.3 0.1 0.04`——与 `frame_num_list` 每层一一对应。 |
| `seed` | `Int` | 可选，默认 `0`。`0` = 完全随机（每次运行都不同）；`seed > 0` 时第 `i` 帧用 `seed + i` 播种，结果可复现，且各帧噪声仍互不相同。 |

### 输出

| 字段 | 类型 | 说明 |
|------|------|------|
| `images` | `Image` | 加噪后的帧，按截取顺序排列。 |

### 行为

* 截帧：`total = sum(frame_num_list)` 帧从头部或尾部截取。文件型输入走懒式
  `as_trimmed` 窗口（与内置 `VideoFrameSample` 的 head/tail 策略相同的参考），
  只解码所需帧；张量型输入或已带裁剪窗口的输入直接切片，避免重编码。
* 逐帧噪声：每一帧独立加噪，与内置 `ImageAddNoise` 相同（`image + strength *
  randn`，裁剪到 [0, 1]）——但噪声每帧重新随机，因此同强度的两帧仍然不同。帧的
  alpha 通道（透明，`C == 4`）原样透传。
* 分层强度：第 `i` 帧取累积 `frame_num_list` 帧数首次超过 `i` 的那一层的强度——
  默认 `18 2 1 1` / `0.4 0.3 0.1 0.04` 是一段淡出噪声斜坡。
* 层数必须对应：`frame_num_list` 与 `strenth_list` 的层数必须相同，否则节点抛
  异常（工作流无法运行）。
* 短视频钳制：视频帧数少于 `total` 时，截取数量取全部可用帧
  （`take = min(total, N)`），多余的帧由靠前的层吸收。

## Context Clips (Lesca)

以纯元数据算术为目标视频规划片段调度：只读取视频的 `frame_count` 和 `fps`
（与 Video Info (Lesca) 相同的访问器，不解码任何帧）。`video` 输入接受
**Load Video (Lesca)** 或任何其它视频节点的输出。该节点是**输出节点**
（`is_output_node=True`）：即使没有连接任何下游节点，Queue 也会执行它。

### 输入

| 字段 | 类型 | 说明 |
|------|------|------|
| `video` | `Video` | 要规划片段的视频。 |
| `target_fps` | `Int` | 默认 `0` = 保持源帧率；否则把视频重采样到该帧率。 |
| `clip_frames` | `Int` | 默认 `124`。每个片段的帧数；必须大于 `context_frames`（步长）。 |
| `context_frames` | `Int` | 默认 `22`。片段之间保留的上下文帧数；步长 = `clip_frames - context_frames`。 |
| `init_index` | `Int` | 默认 `0`。本节点运行前已消耗的片段数；从总数中减去。 |

### 输出

| 字段 | 类型 | 说明 |
|------|------|------|
| `target_frame_count` | `Int` | 目标视频的总帧数：`(frame_count / fps) * target_fps`；`target_fps` 为 `0` 时等于 `frame_count`。 |
| `total_clips` | `Int` | `floor((target_frame_count - context_frames) / stride) + 1 - init_index`。 |
| `last_clip_frames` | `Int` | `(remainder // 17) * 17 + context_frames`，其中 `remainder = (target_frame_count - clip_frames) % stride`。 |

### 行为

* 三个数值照搬 `specs.md`（目标 7）的参考公式，包括 Python float 的 `%` 与
  `//` 语义。
* `target_fps = 0` 表示"保持源帧率"（原式直接代入 0 会得到 0 帧）：改用源
  `fps` 代入，于是 `target_frame_count` 等于 `frame_count`。
* 内部按 float 计算，报告的整数值取 float 结果的 `int(round(...))`。
* 运行后，节点把三个字段逐行显示在节点自带的只读文本框中（前端附加的多行
  文本框，未运行时显示占位提示）。

## 安装

把这个文件夹放进 ComfyUI 的 custom_nodes 目录：

```
ComfyUI/custom_nodes/comfyui-lesca-nodes/
```

然后重启 ComfyUI。节点通过 ComfyUI 的 V3 `comfy_entrypoint` 机制注册（与
`comfy_extras/nodes_video.py` 使用的机制相同）。

## 文件

* `__init__.py` — 包入口；暴露 `comfy_entrypoint`（同时注册七个节点与 Load
  Video 预览路由的组合扩展），以及 `WEB_DIRECTORY = "web"`。
* `lesca_load_video.py` — Load Video 节点、路径解析逻辑、
  `/lesca/load_video/preview` 流式端点，以及文件缺失时的白色占位回退。
* `lesca_save_video.py` — Save Video 节点（Create Video + Save Video 合并，带
  帧/音频裁剪与用户指定保存路径）。
* `lesca_video_info.py` — Video Info 节点（video_info 字符串 / fps / bit
  depth / color space / width / height / frame count）。
* `lesca_video_to_images.py` — Video to Images 节点（窗口化解码，
  force-rate → trim → frame-cap → resize 管线，音频区间切片）。
* `lesca_video_combine.py` — Video Combine 节点（前缀扫描、文件名升序、
  `<prefix>_merged-YYYY_MM_DD_HH_MM_SS.mp4` 产物自排除、ffmpeg-python 流拷贝
  合并与重编码兜底、源文件集合执行缓存指纹）。
* `lesca_video_add_noise.py` — Video Add Noise 节点（head/tail 截帧走懒式
  `as_trimmed` 窗口、独立的逐帧噪声与分层强度调度、可选 seed、短视频钳制）。
* `lesca_context_clips.py` — Context Clips 节点（由容器元数据算出目标总帧数 /
  总片段数 / 末片段帧数；`target_fps = 0` 保持源帧率；输出节点，带只读文本框）。
* `web/extension.js` — 自定义前端：Load Video 的 **Update** 按钮 + 实时预览，
  以及 Video Info 与 Context Clips 节点的只读文本框。
* `pyproject.toml` — ComfyUI 自定义节点管理器用的元数据（`[tool.comfyui] web =
  "web"` 负责挂载前端）。
* `specs.md` — 原始需求规格（目标 1–7，中文）。
