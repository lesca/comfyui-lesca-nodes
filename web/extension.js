// Custom frontend for the Load Video (Lesca) and Video Info (Lesca) nodes.
//
// Load Video: adds an "Update" button directly below the filename field.
// Clicking it — or editing the filename — shows a live video preview WITHOUT
// running the node, by streaming the file from the backend's
// /lesca/load_video/preview endpoint (which handles arbitrary / absolute paths
// that the built-in /view cannot).
//
// Video Info: adds a read-only text box that shows the `video_info` string
// produced by the node. The node is an output node and its execute() returns
// `ui={"video_info": (...)}` so the backend sends an `executed` websocket
// message after every run; the frontend invokes node.onExecuted(output) with
// it, and we copy output.video_info into the text box.
//
// This file is loaded as a standalone ES module by the ComfyUI frontend (see
// pyproject.toml [tool.comfyui] web = "web"). It therefore cannot import the
// frontend's bundled modules; it relies only on the globals the frontend
// exposes (window.app, window.graph) and the litegraph node API.

const LOAD_VIDEO_NODE = 'LoadVideoLesca'
const VIDEO_INFO_NODE = 'VideoInfoLesca'
const CONTEXT_CLIPS_NODE = 'ContextClipsLesca'
const VIDEO_WIDGET_NAME = 'lesca-video-preview'
const BUTTON_NAME = 'Update Preview'
const VIDEO_INFO_WIDGET_NAME = 'video_info'
const CONTEXT_CLIPS_WIDGET_NAME = 'context_clips'
// Guard so a node is only wired up once (nodeCreated + existing-node pass).
const WIRED = Symbol.for('lesca.load-video.wired')
const WIRED_INFO = Symbol.for('lesca.video-info.wired')
const WIRED_CONTEXT = Symbol.for('lesca.context-clips.wired')

// Diagnostic markers — confirm in the browser console (F12) that the extension
// is loaded and wired. Remove these lines once the button is verified working.
const log = (...a) => console.log('[Lesca.LoadVideoPreview]', ...a)
log('extension.js loaded')

const isNode = (node, type) =>
  !!node && (node.type === type || node.comfyClass === type)

// Value of a named input: if the slot is linked (e.g. to a Text node), read
// the *source* node's widget value — the local widget still holds its last
// typed value and would give a stale/empty preview. Otherwise fall back to
// the node's own widget value.
const inputValue = (node, name) => {
  const slot = node.inputs?.find((i) => i.name === name)
  if (slot?.link && node.graph?.links?.[slot.link]) {
    const link = node.graph.links[slot.link]
    const src = node.graph.getNodeById?.(link.origin_id)
    const sw = src?.widgets?.[link.origin_slot]
    if (sw) return String(sw.value ?? '').trim()
  }
  return String(node.widgets?.find((w) => w.name === name)?.value ?? '').trim()
}

const readInputs = (node) => ({
  filename: inputValue(node, 'filename')
})

// Show the video preview for a node, streaming from the custom backend endpoint.
// ``filename`` holds the full video file path (relative paths are resolved
// against the ComfyUI root directory on the backend).
const showPreview = (node) => {
  const { filename } = readInputs(node)
  if (!filename) return

  const url = '/lesca/load_video/preview?filename=' + encodeURIComponent(filename)

  // Clear any previous preview element before loading a new one.
  if (node.videoContainer) node.videoContainer.replaceChildren()

  const video = document.createElement('video')
  video.playsInline = true
  video.controls = true
  video.loop = true
  video.preload = 'metadata'

  video.onloadeddata = () => {
    // Match the built-in preview sizing: the video spans the node's width,
    // height follows the aspect ratio (minimum 64px) — the same computation
    // as fitDimensionsToNodeWidth in the ComfyUI frontend.
    const minWidth = node.size?.[0] || 256
    const aspect = video.videoWidth / video.videoHeight
    const minHeight = aspect && !isNaN(aspect) ? Math.max(minWidth / aspect, 64) : 64

    if (!node.videoContainer) {
      const container = document.createElement('div')
      container.classList.add('comfy-img-preview')
      node.videoContainer = container
      const widget = node.addDOMWidget(VIDEO_WIDGET_NAME, 'video', container, {
        surfaces: { canvas: 'shown', vueNode: 'never', panel: 'never' },
        hideOnZoom: false
      })
      widget.serialize = false
      widget.computeLayoutSize = () => ({ minHeight, minWidth })
    }
    node.videoContainer.replaceChildren(video)
    node.graph?.setDirtyCanvas(true, true)
  }

  video.onerror = () => {
    node.videoContainer?.replaceChildren(
      Object.assign(document.createElement('div'), {
        textContent: '预览加载失败：文件不存在或不是视频',
        style: 'padding:8px;color:#e88;font-size:12px'
      })
    )
  }

  video.src = url
}

// Wire up a single Load Video (Lesca) node instance.
const setupNode = (node) => {
  if (!isNode(node, LOAD_VIDEO_NODE) || node[WIRED]) return
  node[WIRED] = true
  log('wiring node', node.id, 'type=', node.type, 'comfyClass=', node.comfyClass)

  // Append the button after the filename widget so it sits right below it.
  node.addWidget('button', BUTTON_NAME, 'Update', () => showPreview(node), {
    serialize: false,
    surfaces: { canvas: 'shown', vueNode: 'never', panel: 'never' }
  })

  // Auto-preview whenever the filename is edited (specs goal 1 requirement:
  // "filename 字段更新后触发预览的显示").
  const filenameWidget = node.widgets?.find((w) => w.name === 'filename')
  if (filenameWidget) {
    const prev = filenameWidget.callback
    filenameWidget.callback = (...args) => {
      prev?.apply(filenameWidget, args)
      showPreview(node)
    }
  }

  // Preview any value already present (e.g. a workflow restored on load).
  if (readInputs(node).filename) showPreview(node)
}

// Wire up a single Video Info (Lesca) node instance: add the read-only text
// box that displays the video_info string produced by the last run.
const setupVideoInfoNode = (node) => {
  if (!isNode(node, VIDEO_INFO_NODE) || node[WIRED_INFO]) return
  node[WIRED_INFO] = true
  log('wiring video info node', node.id, 'type=', node.type, 'comfyClass=', node.comfyClass)

  const ta = document.createElement('textarea')
  ta.readOnly = true
  ta.spellcheck = false
  ta.value = ''
  ta.placeholder = '运行节点后在此显示 video_info'
  ta.style.cssText =
    'width:100%;box-sizing:border-box;min-height:150px;resize:none;' +
    'background:rgba(0,0,0,.35);color:#ccc;border:1px solid #555;' +
    'border-radius:4px;padding:6px;outline:none;' +
    "font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace"

  // Raw-element DOM widget (the frontend's LGraphNode.addDOMWidget accepts any
  // HTMLElement); serialize:false keeps the transient output out of the
  // workflow JSON, hideOnZoom:false keeps it readable at low zoom.
  node.addDOMWidget(VIDEO_INFO_WIDGET_NAME, 'textarea', ta, {
    serialize: false,
    hideOnZoom: false,
    getMinHeight: () => 150,
    surfaces: { canvas: 'shown', vueNode: 'never', panel: 'never' }
  })

  // The frontend calls node.onExecuted(output) for the `executed` websocket
  // message; output.video_info is the string as a (batch) list.
  const prev = node.onExecuted
  node.onExecuted = (output) => {
    prev?.call(node, output)
    const v = output?.video_info
    ta.value = Array.isArray(v) ? v.join('\n\n') : String(v ?? '')
  }
}

// Wire up a single Context Clips (Lesca) node instance: add the read-only
// text box that shows the three output fields (target_frame_count, total_clips,
// last_clip_frames) produced by the last run, one per line. Same mechanism as
// Video Info: the node returns ui={"context_clips": (info,)} so the backend
// sends an `executed` websocket message, and node.onExecuted(output) fills the
// box from output.context_clips.
const setupContextClipsNode = (node) => {
  if (!isNode(node, CONTEXT_CLIPS_NODE) || node[WIRED_CONTEXT]) return
  node[WIRED_CONTEXT] = true
  log('wiring context clips node', node.id, 'type=', node.type, 'comfyClass=', node.comfyClass)

  const ta = document.createElement('textarea')
  ta.readOnly = true
  ta.spellcheck = false
  ta.value = ''
  ta.placeholder = '运行节点后在此显示 context_clips 输出'
  ta.style.cssText =
    'width:100%;box-sizing:border-box;min-height:90px;resize:none;' +
    'background:rgba(0,0,0,.35);color:#ccc;border:1px solid #555;' +
    'border-radius:4px;padding:6px;outline:none;' +
    "font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace"

  node.addDOMWidget(CONTEXT_CLIPS_WIDGET_NAME, 'textarea', ta, {
    serialize: false,
    hideOnZoom: false,
    getMinHeight: () => 90,
    surfaces: { canvas: 'shown', vueNode: 'never', panel: 'never' }
  })

  const prev = node.onExecuted
  node.onExecuted = (output) => {
    prev?.call(node, output)
    const v = output?.context_clips
    ta.value = Array.isArray(v) ? v.join('\n\n') : String(v ?? '')
  }
}

const register = () => {
  const app = window.app
  if (!app) return false
  app.registerExtension({
    name: 'Lesca.LoadVideoPreview',
    nodeCreated: (node) => {
      if (isNode(node, LOAD_VIDEO_NODE)) setupNode(node)
    }
  })
  app.registerExtension({
    name: 'Lesca.VideoInfoDisplay',
    nodeCreated: (node) => {
      if (isNode(node, VIDEO_INFO_NODE)) setupVideoInfoNode(node)
    }
  })
  app.registerExtension({
    name: 'Lesca.ContextClipsDisplay',
    nodeCreated: (node) => {
      if (isNode(node, CONTEXT_CLIPS_NODE)) setupContextClipsNode(node)
    }
  })
  log('extension registered')
  // Nodes that already exist (restored before this extension registered) won't
  // fire nodeCreated again — wire them up explicitly. window.graph is assigned
  // right after window.app, so retry briefly if it is not ready yet.
  const wireExisting = () => {
    const graph = window.graph
    if (!graph?._nodes) return false
    let count = 0
    for (const n of graph._nodes) {
      if (isNode(n, LOAD_VIDEO_NODE)) { setupNode(n); count++ }
      if (isNode(n, VIDEO_INFO_NODE)) { setupVideoInfoNode(n); count++ }
      if (isNode(n, CONTEXT_CLIPS_NODE)) { setupContextClipsNode(n); count++ }
    }
    log('existing nodes wired:', count, 'total nodes:', graph._nodes.length)
    return count > 0
  }
  let wired = wireExisting()
  if (!wired) {
    const t = setInterval(() => { if (wireExisting()) clearInterval(t) }, 50)
    setTimeout(() => clearInterval(t), 5000)
  }
  return true
}

if (!register()) {
  // window.app is only assigned after ComfyApp.setup(); poll until it exists.
  const timer = setInterval(() => {
    if (register()) clearInterval(timer)
  }, 50)
}
