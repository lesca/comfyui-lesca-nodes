# comfyui-lesca-nodes

## 目标 1: Load Video (Lesca) 节点

Comfy UI中自带的视频加载节点无法指定路径。请参考Comfy UI 自带的 Load Video 节点，在本目录中创建一个新的Load Video节点 - Load Video (Lesca) ：
* 输入：
  * filename: 字符串类型，用户指定视频文件路径
* 输出：
  * video: 和原节点一致。

要求：
* 默认的相对路径现在解析到 ComfyUI 根目录，以便用户可以指定input或者output目录。
* filename 字段更新后触发预览的显示。

## 目标 2: Save Video (Lesca) 节点

Comfy UI中自带的节点将图片保存成视频需要使用Create Video和Save Video两个节点。请参考Comfy UI 自带的这两个节点，在本目录中创建一个新的Save Video (Lesca)节点，合并Create Video和Save Video的功能。

输入输出参数如下。
* 输入：
  * images: 输入的图片，和 Create video的输入一致。
  * audio: 输入的音频，和 Create video的输入一致。
  * filename: 字符串类型，用户指定保存视频的文件路径
  * trim_frames: 整数类型。指定要移除的帧数量。该节点会从第0帧移除指定数量的帧数，同时会移除对应长度的音频，再保存成视频。默认值0，不移除任何帧。
  * Create video的其他参数：例如 fps, bit_depth, color_space 
  * Save video的其他参数：例如 format, codec, encoding, crf 
* 输出：
  * filename: 字符串类型。保存的视频文件路径，例如 output/video/xxx.mp4
  * video: 和save video的video输出一致。
  * images: trim 后的图片，类型和输入的 input 的 images一致
  * audio: trim 后的音频，类型和输入的 input 的 audio 一致

要求：
* 默认的相对路径现在解析到 ComfyUI 根目录，以便用户可以指定input或者output目录。
* 按照指定的文件名保存，保存的时候不要出现诸如 _000001_ 这样的文件名后缀（这是Save Video节点的默认行为）。
* trim_frames 表示从第0帧开始移除 trim_frames 数量的帧数，同时还要移除对应时长的音频，然后再合成视频。音频的移除可以参考ComfyUI自带的TrimAudioDuration节点。
* 音频移除需要考虑帧率fps，例如移除4帧的视频，那对应移除的音频应该是4* (1/24) 秒。

## 目标 3: Video Info (Lesca) 节点

* 输入：
  * video: io.Video 类型。可以接受来自 Load Video (Lesca) 节点的输入
* 输出：
  * video_info: 字符串类型，换行形式输出除自身外的所有输出字段。
  * fps: float 类型。视频的帧率
  * bit_depth: 字符串类型，视频的位深。
  * color_space: 字符串类型，视频的色彩空间。
  * width: 整数类型，视频的宽度。
  * height: 整数类型，视频的高度。
  * frame_count: 整数类型，视频的总帧数。

要求：
* 该节点是输出节点（is_output_node=True）：即使没有连接任何下游节点，Queue 也会执行它。
* 节点运行后，把 video_info 输出的内容在节点自带的只读文本框中显示（前端附加的多行文本框，未运行时显示占位提示）。


## 目标 4: Video to Images (Lesca) 节点

* 输入：
  * video: io.Video 类型。可以接受来自 Load Video (Lesca) 节点的输入
  * frame_cap: 整数类型。需要导出的帧数。默认为0，表示所有帧。
  * trim_mode: 跳过帧数的模式。head: 从第0帧开始去掉帧；tail: 去掉尾部的帧。
  * trim_frames: 整数类型。需要去掉的帧数。默认为0，不丢掉任何帧。
  * force_rate: 整数类型。强制帧率转换。默认0，帧率不变。如果force_rate=24，对于30帧的视频，将会强制转成24帧。
  * custom_width: 整数类型。设置视频的宽度。默认为0。
  * custom_height: 整数类型。设置视频的高度。默认为0。
* 输出：
  * images: 类型 io.Image。帧率强制转化后的图片。
  * audio: 类型 io.Audio。音频输出为导出视频所播放的源时间区间 [shift/E, (shift+K)/E)：shift 为 head 模式下的 trim_frames（tail 模式为 0），E 为 force_rate>0 时的输出帧率、否则源帧率 S，K 为导出帧数。音频时长恰为 K/E 秒 = 输出视频时长，force_rate 改变帧率时音画仍严格同步、相邻两段无缝相接（若按源帧区间 [q[0]/S,(q[-1]+1)/S) 切片，时长偏差可达 K*(1/T-1/S)，导致音频提前结束、拼接漂移）。head trim 丢掉被裁帧下的音频前缀，frame_cap 同时丢掉未导出帧对应的音频尾部（音频按源采样率切片）。无音频轨的视频输出 None。
  * fps: float 类型。视频的fps，如果有force_rate，则输出force_rate所指定的FPS。
  * video: io.Video 类型。video 输出。总帧数不在此输出：将该 video 输出接入 Video Info (Lesca) 节点的 frame_count 即可获取。

要求：
* 关于 custom_height 和 custom_width
  * 如果custom_width, custom_height 都为0，则视频宽高保持不变。
  * custom_height 或者 custom_width 其中一个为0，一个不为0。就等比例缩放。
  * custom_height 和 custom_width 两者都非 0 → 精确缩放到 (W,H)
* 顺序：force-rate → trim → frame-cap → resize
* 窗口化解码：对普通文件输入的 io.Video，节点只解码导出帧覆盖的窗口 [q[0]/S, (q[-1]+1)/S)（S 为源帧率），而不是整个文件——导出长视频的前 1 秒不再触发全量解码导致的内存爆炸。张量输入（VideoFromComponents）、已带裁剪窗口的输入、元数据缺失帧率/帧数的输入，或解码结果不足时，回退到全量解码 + 相同索引映射，两条路径输出逐像素一致。


## 对你可能有帮助的参考资料

### ComfyUI 源码

* comfyui 的源码位于 `workspace/sources/comfyui`
* comfyui_frontend 的源码位于 `workspace/sources/comfyui_frontend`

### 创建节点的资料参考

https://docs.comfy.org/custom-nodes/walkthrough