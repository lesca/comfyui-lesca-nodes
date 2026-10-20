// Custom frontend for the Load Video (Lesca) node.
//
// Adds an "Update" button directly below the filename field. Clicking it — or
// editing the filename — shows a live video preview WITHOUT running the node,
// by streaming the file from the backend's /lesca/load_video/preview endpoint
// (which handles arbitrary / absolute paths that the built-in /view cannot).
//
// This file is loaded as a standalone ES module by the ComfyUI frontend (see
// pyproject.toml [tool.comfyui] web = "web"). It therefore cannot import the
// frontend's bundled modules; it relies only on the globals the frontend
// exposes (window.app, window.graph) and the litegraph node API.

const NODE_NAME = 'LoadVideoLesca'
const VIDEO_WIDGET_NAME = 'lesca-video-preview'
const BUTTON_NAME = 'Update Preview'
// Guard so a node is only wired up once (nodeCreated + existing-node pass).
const WIRED = Symbol.for('lesca.load-video.wired')

// Diagnostic markers — confirm in the browser console (F12) that the extension
// is loaded and wired. Remove these lines once the button is verified working.
const log = (...a) => console.log('[Lesca.LoadVideoPreview]', ...a)
log('extension.js loaded')

const isOurNode = (node) =>
  !!node && (node.type === NODE_NAME || node.comfyClass === NODE_NAME)

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
  if (!isOurNode(node) || node[WIRED]) return
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

const register = () => {
  const app = window.app
  if (!app) return false
  app.registerExtension({
    name: 'Lesca.LoadVideoPreview',
    nodeCreated: (node) => {
      if (isOurNode(node)) setupNode(node)
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
    for (const n of graph._nodes) if (isOurNode(n)) { setupNode(n); count++ }
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
