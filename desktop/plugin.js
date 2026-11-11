// hermes-shaperotator — Desktop pane: live 3D view of the model, click a face to reference it in chat,
// FEA stress map, and the evidence log the claims checker uses.
//
// Disk plugin: plain ESM, no JSX, no build. Only @hermes/plugin-sdk / react / react/jsx-runtime.
// The renderer is plain WebGL 2 with a real depth test (no three.js, no network).
import { host, useValue } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const POLL_MS = 2500

// ------------------------------------------------------------------ colour helpers
function turbo(t) {
  // Google "Turbo" colormap, polynomial fit. t in [0, 1] -> [r, g, b] 0..255
  t = Math.min(1, Math.max(0, t))
  const r = 34.61 + t * (1172.33 - t * (10793.56 - t * (33300.12 - t * (38394.49 - t * 14825.05))))
  const g = 23.31 + t * (557.33 + t * (1225.33 - t * (3574.96 - t * (1073.77 + t * 707.56))))
  const b = 27.2 + t * (3211.1 - t * (15327.97 - t * (27814 - t * (22569.18 - t * 6838.66))))
  return [r, g, b].map(v => Math.max(0, Math.min(255, Math.round(v))))
}

function ramp(stops) {
  return (t) => {
    t = Math.min(1, Math.max(0, t)) * (stops.length - 1)
    const i = Math.min(stops.length - 2, Math.floor(t)), f = t - i
    return [0, 1, 2].map(k => Math.round(stops[i][k] + (stops[i + 1][k] - stops[i][k]) * f))
  }
}

const PALETTES = {
  Turbo: turbo,
  Rainbow: ramp([[0, 0, 255], [0, 255, 255], [0, 255, 0], [255, 255, 0], [255, 0, 0]]),   // the classic FEA look
  Viridis: ramp([[68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37]]),
  Heat: ramp([[20, 10, 40], [120, 20, 90], [220, 60, 40], [250, 170, 30], [252, 250, 200]]),
  Grey: ramp([[40, 40, 40], [235, 235, 235]]),
}
const OVER_RANGE = [230, 40, 200]      // values above a manual maximum

function fmt(v) {
  if (v == null || !isFinite(v)) return '—'
  const a = Math.abs(v)
  return a >= 100 ? v.toFixed(0) : a >= 1 ? v.toFixed(1) : a === 0 ? '0' : v.toPrecision(2)
}

function cssVar(el, name, fallback) {
  try {
    const v = getComputedStyle(el).getPropertyValue(name).trim()
    return v || fallback
  } catch {
    return fallback
  }
}

function parseColor(str, fallback) {
  // canvas-normalise any CSS colour to [r,g,b]
  try {
    // Paint one pixel and read it back: works for any CSS colour syntax the browser knows
    // (oklch(), color(srgb …), color-mix() …), which a regex over fillStyle does not.
    const cv = document.createElement('canvas')
    cv.width = cv.height = 1
    const c = cv.getContext('2d', { willReadFrequently: true })
    c.fillStyle = fallback
    c.fillStyle = str
    c.fillRect(0, 0, 1, 1)
    const d = c.getImageData(0, 0, 1, 1).data
    if (d[3] < 128) {           // transparent / unresolved → use the fallback
      c.clearRect(0, 0, 1, 1)
      c.fillStyle = fallback
      c.fillRect(0, 0, 1, 1)
      const f = c.getImageData(0, 0, 1, 1).data
      return [f[0], f[1], f[2]]
    }
    return [d[0], d[1], d[2]]
  } catch {
    return [128, 128, 128]
  }
}

// ------------------------------------------------------------------ geometry prep
// Assembly parts: a colour per kind of part (name keywords) and the option to hide some
const PART_COLOURS = [[196, 52, 44], [44, 46, 52], [184, 190, 198], [40, 52, 84], [150, 156, 166], [214, 150, 40], [96, 120, 150], [120, 120, 120]]
function partColour(name) {
  const n = (name || '').toLowerCase()
  if (/frame|chassis|rail/.test(n)) return 0                                     // red: the analysed structure
  if (/tyre|tire/.test(n) || (/wheel/.test(n) && !/steering/.test(n))) return 1  // black rubber
  if (/steering ?wheel|steeringwheel|chain|seat|tank/.test(n) && !/stay/.test(n)) return /seat|tank/.test(n) ? 3 : 1
  if (/nose|pod|fairing|body|panel/.test(n)) return 5                           // amber bodywork
  if (/rim|engine|motor|tray|pedal|sprocket|disc|brake|hub/.test(n)) return 4    // aluminium / steel grey
  if (/axle|kingpin|stub|column|bearing|shaft|pin|bumper|nerf|stay|tie|spindle/.test(n)) return 2  // chrome
  return 6
}

// The mesh without the hidden parts (faces, triangles and edges); the same object when nothing is hidden
function withoutParts(mesh, hidden) {
  if (!mesh || !mesh.parts || !hidden || !hidden.size) return mesh
  const keepFace = new Set((mesh.faces || []).filter(f => !hidden.has(f.part)).map(f => f.id))
  const T = [], F = []
  for (let k = 0; k < mesh.face_of_tri.length; k++) {
    if (!keepFace.has(mesh.face_of_tri[k])) continue
    T.push(mesh.triangles[3 * k], mesh.triangles[3 * k + 1], mesh.triangles[3 * k + 2]); F.push(mesh.face_of_tri[k])
  }
  const ep = mesh.edge_part || []
  const edges = (mesh.edges || []).filter((e, i) => !hidden.has(ep[i]))
  let bb = [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity]
  for (const e of edges) for (let i = 0; i < e.length; i += 3) for (let j = 0; j < 3; j++) { bb[j] = Math.min(bb[j], e[i + j]); bb[3 + j] = Math.max(bb[3 + j], e[i + j]) }
  if (!isFinite(bb[0])) bb = mesh.bbox
  return { ...mesh, triangles: T, face_of_tri: F, faces: mesh.faces.filter(f => keepFace.has(f.id)), edges, edge_part: null, bbox: bb }
}

function prepare(mesh) {
  const V = Float32Array.from(mesh.vertices)
  const T = Uint32Array.from(mesh.triangles)
  const F = Uint32Array.from(mesh.face_of_tri)
  const nt = F.length
  const N = new Float32Array(nt * 3)
  for (let k = 0; k < nt; k++) {
    const a = T[3 * k] * 3, b = T[3 * k + 1] * 3, c = T[3 * k + 2] * 3
    const ux = V[b] - V[a], uy = V[b + 1] - V[a + 1], uz = V[b + 2] - V[a + 2]
    const vx = V[c] - V[a], vy = V[c + 1] - V[a + 1], vz = V[c + 2] - V[a + 2]
    let nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx
    const l = Math.hypot(nx, ny, nz) || 1
    N[3 * k] = nx / l; N[3 * k + 1] = ny / l; N[3 * k + 2] = nz / l
  }
  const bb = mesh.bbox
  const center = [(bb[0] + bb[3]) / 2, (bb[1] + bb[4]) / 2, (bb[2] + bb[5]) / 2]
  const radius = Math.hypot(bb[3] - bb[0], bb[4] - bb[1], bb[5] - bb[2]) / 2 || 1
  let triVal = null, vmin = 0, vmax = 1, values = null
  if (Array.isArray(mesh.values) && mesh.values.length * 3 === V.length) {
    const vals = mesh.values
    values = Float32Array.from(vals)
    triVal = new Float32Array(nt)
    vmin = Infinity; vmax = -Infinity
    for (const v of vals) { if (v < vmin) vmin = v; if (v > vmax) vmax = v }
    for (let k = 0; k < nt; k++) triVal[k] = (vals[T[3 * k]] + vals[T[3 * k + 1]] + vals[T[3 * k + 2]]) / 3
    if (!(vmax > vmin)) vmax = vmin + 1
  }
  // CAD meshes have separate vertices per face, so averaging gives smooth cylinders but keeps sharp edges.
  // FE result meshes share vertices across faces, so those stay flat-shaded.
  let VN = null
  if (!values) {
    VN = new Float32Array(V.length)
    for (let k = 0; k < nt; k++) {
      for (let c = 0; c < 3; c++) {
        const v = T[3 * k + c] * 3
        VN[v] += N[3 * k]; VN[v + 1] += N[3 * k + 1]; VN[v + 2] += N[3 * k + 2]
      }
    }
    for (let i = 0; i < VN.length; i += 3) {
      const l = Math.hypot(VN[i], VN[i + 1], VN[i + 2]) || 1
      VN[i] /= l; VN[i + 1] /= l; VN[i + 2] /= l
    }
  }
  const faces = new Map((mesh.faces || []).map(f => [f.id, f]))
  const partCol = mesh.parts ? new Map((mesh.faces || []).filter(f => f.part != null).map(f => [f.id, partColour(mesh.parts[f.part] && mesh.parts[f.part].name)])) : null
  const D = Array.isArray(mesh.displacements) && mesh.displacements.length === V.length ? Float32Array.from(mesh.displacements) : null
  let dmax = 0
  const fields = []
  if (values) fields.push({ label: mesh.value_label || 'value', values, vmin, vmax })
  if (D) {
    const mag = new Float32Array(V.length / 3)
    for (let i = 0; i < mag.length; i++) { mag[i] = Math.hypot(D[3 * i], D[3 * i + 1], D[3 * i + 2]); if (mag[i] > dmax) dmax = mag[i] }
    fields.push({ label: 'Displacement (mm)', values: mag, vmin: 0, vmax: dmax || 1 })
  }
  return { V, T, F, N, VN, nt, center, radius, triVal, values, vmin, vmax, D, dmax, fields, faces, edges: mesh.edges || [],
           supports: new Set(mesh.support_faces || []), loads: new Set(mesh.load_faces || []),
           valueLabel: mesh.value_label || 'value', partCol }
}

function rot(cam) {
  const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw), cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch)
  // world Z is up. yaw about Z, then pitch about the camera X axis
  return [
    [cy, -sy, 0],
    [sy * sp, cy * sp, cp],
    [sy * cp, cy * cp, -sp],   // depth axis (into the screen)
  ]
}

// ------------------------------------------------------------------ renderer (WebGL 2, real depth test)
const VS_MESH = `#version 300 es
layout(location=0) in vec3 aPos;
layout(location=1) in vec3 aNormal;
layout(location=2) in float aValue;
layout(location=3) in float aFace;
layout(location=4) in float aKind;
layout(location=5) in vec3 aDisp;
uniform vec3 uR0, uR1, uR2;
uniform vec2 uScale, uOffset;
uniform float uDepth, uDeform;
out float vValue, vShade;
flat out float vFace, vKind;
void main() {
  vec3 p = aPos + aDisp * uDeform;
  gl_Position = vec4(uScale.x * dot(uR0, p) + uOffset.x, uScale.y * dot(uR1, p) + uOffset.y, dot(uR2, p) * uDepth, 1.0);
  vec3 n = vec3(dot(uR0, aNormal), dot(uR1, aNormal), dot(uR2, aNormal));
  vShade = 0.4 + 0.6 * abs(dot(n, vec3(0.35, 0.55, -0.76)));   // two-sided: tessellation winding varies by face
  vValue = aValue; vFace = aFace; vKind = aKind;
}`
const FS_MESH = `#version 300 es
precision highp float;
in float vValue, vShade;
flat in float vFace, vKind;
uniform int uStress;
uniform float uSelected;
uniform vec3 uBase, uSupport, uLoad, uAccent, uOverColor;
uniform vec3 uPartCol[8];
uniform sampler2D uLut;
uniform vec2 uRange;
uniform float uBands, uCut;
uniform int uOver, uMapping;
out vec4 color;
void main() {
  vec3 c;
  if (uStress == 1) {
    float t = (vValue - uRange.x) / max(uRange.y - uRange.x, 1e-9);
    if (uCut > 0.0 && vValue < uCut) c = uBase * vShade;                // below the highlight level: plain model
    else if (uOver == 1 && t > 1.0) c = uOverColor * (0.75 + 0.25 * vShade);
    else {
      t = clamp(t, 0.0, 1.0);
      if (uMapping == 1) t = pow(t, 0.45);                                  // balanced: spreads the low range
      else if (uMapping == 2) t = log(1.0 + 99.0 * t) / log(100.0);         // log
      if (uBands > 0.5) t = (floor(min(t, 0.9999) * uBands) + 0.5) / uBands;
      c = texture(uLut, vec2(t, 0.5)).rgb * (0.75 + 0.25 * vShade);
    }
  }
  else c = (vKind > 9.5 ? uPartCol[int(vKind - 9.5)] : vKind > 1.5 ? uLoad : vKind > 0.5 ? uSupport : uBase) * vShade;
  if ((uSelected >= 0.0 && abs(vFace - uSelected) < 0.5) || (vKind > 2.5 && vKind < 3.5)) c = mix(c, uAccent, 0.6);
  color = vec4(c, 1.0);
}`
const VS_LINE = `#version 300 es
layout(location=0) in vec3 aPos;
uniform vec3 uR0, uR1, uR2;
uniform vec2 uScale, uOffset;
uniform float uDepth;
void main() {
  gl_Position = vec4(uScale.x * dot(uR0, aPos) + uOffset.x, uScale.y * dot(uR1, aPos) + uOffset.y, dot(uR2, aPos) * uDepth, 1.0);
}`
const FS_LINE = `#version 300 es
precision highp float;
uniform vec3 uColor;
out vec4 color;
void main() { color = vec4(uColor, 1.0); }`

function program(gl, vs, fs) {
  const shader = (type, src) => {
    const s = gl.createShader(type)
    gl.shaderSource(s, src)
    gl.compileShader(s)
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || 'shader failed')
    return s
  }
  const p = gl.createProgram()
  gl.attachShader(p, shader(gl.VERTEX_SHADER, vs))
  gl.attachShader(p, shader(gl.FRAGMENT_SHADER, fs))
  gl.linkProgram(p)
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) || 'link failed')
  const u = {}
  const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS)
  for (let i = 0; i < n; i++) {
    const name = gl.getActiveUniform(p, i).name
    u[name] = gl.getUniformLocation(p, name)
  }
  return { p, u }
}

const RENDERERS = new WeakMap()

function rendererFor(canvas) {
  let r = RENDERERS.get(canvas)
  if (r) return r.gl.isContextLost() ? null : r
  const gl = canvas.getContext('webgl2', { antialias: true, alpha: true, premultipliedAlpha: true })
  if (!gl) return null
  canvas.addEventListener('webglcontextlost', e => e.preventDefault())
  canvas.addEventListener('webglcontextrestored', () => RENDERERS.delete(canvas))
  r = { gl, mesh: program(gl, VS_MESH, FS_MESH), line: program(gl, VS_LINE, FS_LINE), geo: null,
        meshVao: gl.createVertexArray(), meshBuf: gl.createBuffer(), edgeVao: gl.createVertexArray(), edgeBuf: gl.createBuffer(),
        markVao: gl.createVertexArray(), markBuf: gl.createBuffer(), meshCount: 0, edgeCount: 0,
        lut: gl.createTexture(), lutName: null, field: -1 }
  RENDERERS.set(canvas, r)
  return r
}

function setLut(r, name) {
  if (r.lutName === name) return
  const gl = r.gl, fn = PALETTES[name] || turbo, px = new Uint8Array(256 * 4)
  for (let i = 0; i < 256; i++) { const c = fn(i / 255); px.set([c[0], c[1], c[2], 255], i * 4) }
  gl.bindTexture(gl.TEXTURE_2D, r.lut)
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, px)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
  r.lutName = name
}

function upload(r, geo, fieldIndex, picked) {
  const gl = r.gl
  const { V, T, F, N, VN, nt, center, D } = geo
  const values = geo.fields[fieldIndex] ? geo.fields[fieldIndex].values : null
  r.field = fieldIndex
  r.pickedKey = picked ? picked.key : ''
  const many = picked ? picked.faces : null
  // one vertex per triangle corner: position 3, normal 3, value, face, kind, displacement 3
  const data = new Float32Array(nt * 3 * 12)
  let o = 0
  for (let k = 0; k < nt; k++) {
    const fid = F[k]
    const kind = many && many.has(fid) ? 3 : geo.loads.has(fid) ? 2 : geo.supports.has(fid) ? 1
      : geo.partCol && geo.partCol.has(fid) ? 10 + geo.partCol.get(fid) : 0
    for (let c = 0; c < 3; c++) {
      const v = T[3 * k + c]
      data[o++] = V[3 * v] - center[0]; data[o++] = V[3 * v + 1] - center[1]; data[o++] = V[3 * v + 2] - center[2]
      if (VN) { data[o++] = VN[3 * v]; data[o++] = VN[3 * v + 1]; data[o++] = VN[3 * v + 2] }
      else { data[o++] = N[3 * k]; data[o++] = N[3 * k + 1]; data[o++] = N[3 * k + 2] }
      data[o++] = values ? values[v] : 0
      data[o++] = fid
      data[o++] = kind
      data[o++] = D ? D[3 * v] : 0; data[o++] = D ? D[3 * v + 1] : 0; data[o++] = D ? D[3 * v + 2] : 0
    }
  }
  gl.bindVertexArray(r.meshVao)
  gl.bindBuffer(gl.ARRAY_BUFFER, r.meshBuf)
  gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW)
  const stride = 12 * 4
  ;[[0, 3, 0], [1, 3, 12], [2, 1, 24], [3, 1, 28], [4, 1, 32], [5, 3, 36]].forEach(([loc, size, off]) => {
    gl.enableVertexAttribArray(loc)
    gl.vertexAttribPointer(loc, size, gl.FLOAT, false, stride, off)
  })
  r.meshCount = nt * 3
  let segs = 0
  for (const e of geo.edges) segs += Math.max(0, e.length / 3 - 1)
  const lines = new Float32Array(segs * 6)
  let p = 0
  for (const e of geo.edges) {
    for (let i = 0; i + 5 < e.length; i += 3) {
      for (let j = 0; j < 6; j++) lines[p++] = e[i + j] - center[j % 3]
    }
  }
  gl.bindVertexArray(r.edgeVao)
  gl.bindBuffer(gl.ARRAY_BUFFER, r.edgeBuf)
  gl.bufferData(gl.ARRAY_BUFFER, lines, gl.STATIC_DRAW)
  gl.enableVertexAttribArray(0)
  gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 12, 0)
  r.edgeCount = segs * 2
  gl.bindVertexArray(r.markVao)
  gl.bindBuffer(gl.ARRAY_BUFFER, r.markBuf)
  gl.enableVertexAttribArray(0)
  gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 12, 0)
  gl.bindVertexArray(null)
  r.geo = geo
}

function unit(rgb) { return rgb.map(v => v / 255) }

// Returns the camera used for picking, or null when WebGL 2 isn't available.
function draw(canvas, geo, cam, opts) {
  const dpr = window.devicePixelRatio || 1
  const w = canvas.clientWidth, h = canvas.clientHeight
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr)
  }
  const r = rendererFor(canvas)
  if (!r) return null
  const gl = r.gl
  gl.viewport(0, 0, canvas.width, canvas.height)
  gl.clearColor(0, 0, 0, 0)
  gl.clearDepth(1)
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT)
  if (!geo || !w || !h) return { R: rot(cam), s: 1, cx: 0, cy: 0, w, h }
  const rv = opts.result || {}
  const fieldIndex = Math.min(rv.field || 0, Math.max(0, geo.fields.length - 1))
  const picked = opts.picked || null
  if (r.geo !== geo || r.field !== fieldIndex || r.pickedKey !== (picked ? picked.key : '')) upload(r, geo, fieldIndex, picked)
  const R = rot(cam)
  const s = (Math.min(w, h) * 0.42 / geo.radius) * cam.zoom
  const cx = w / 2 + cam.panX, cy = h / 2 + cam.panY
  const common = (u) => {
    gl.uniform3fv(u.uR0, R[0]); gl.uniform3fv(u.uR1, R[1]); gl.uniform3fv(u.uR2, R[2])
    gl.uniform2f(u.uScale, 2 * s / w, 2 * s / h)
    gl.uniform2f(u.uOffset, 2 * cx / w - 1, 1 - 2 * cy / h)
    gl.uniform1f(u.uDepth, 1 / (geo.radius * 1.05))
  }
  gl.enable(gl.DEPTH_TEST)
  gl.depthFunc(gl.LEQUAL)
  gl.disable(gl.CULL_FACE)
  // surfaces sit a hair behind their own edges, so edges on visible faces show and hidden ones don't
  gl.enable(gl.POLYGON_OFFSET_FILL)
  gl.polygonOffset(1, 1)
  gl.useProgram(r.mesh.p)
  common(r.mesh.u)
  gl.uniform1i(r.mesh.u.uStress, opts.mode === 'stress' && geo.values ? 1 : 0)
  const fd = geo.fields[fieldIndex]
  setLut(r, rv.palette || 'Turbo')
  gl.activeTexture(gl.TEXTURE0)
  gl.bindTexture(gl.TEXTURE_2D, r.lut)
  gl.uniform1i(r.mesh.u.uLut, 0)
  gl.uniform2f(r.mesh.u.uRange, rv.lo != null ? rv.lo : (fd ? fd.vmin : 0), rv.hi != null ? rv.hi : (fd ? fd.vmax : 1))
  gl.uniform1f(r.mesh.u.uBands, rv.bands || 0)
  gl.uniform1f(r.mesh.u.uCut, rv.cut || 0)
  gl.uniform1i(r.mesh.u.uMapping, SCALES.indexOf(rv.scale || 'balanced'))
  gl.uniform1i(r.mesh.u.uOver, rv.hi != null ? 1 : 0)
  gl.uniform3fv(r.mesh.u.uOverColor, unit(OVER_RANGE))
  const deform = opts.mode === 'stress' ? (opts.deform || 0) : 0
  gl.uniform1f(r.mesh.u.uDeform, deform)
  gl.uniform1f(r.mesh.u.uSelected, opts.selected == null ? -1 : opts.selected)
  gl.uniform3fv(r.mesh.u.uBase, unit(opts.base))
  gl.uniform3fv(r.mesh.u.uSupport, unit(opts.supColor))
  gl.uniform3fv(r.mesh.u.uLoad, unit(opts.loadColor))
  if (r.mesh.u['uPartCol[0]']) gl.uniform3fv(r.mesh.u['uPartCol[0]'], new Float32Array(PART_COLOURS.flatMap(unit)))
  gl.uniform3fv(r.mesh.u.uAccent, unit(opts.selColor))
  gl.bindVertexArray(r.meshVao)
  gl.drawArrays(gl.TRIANGLES, 0, r.meshCount)
  gl.disable(gl.POLYGON_OFFSET_FILL)
  gl.useProgram(r.line.p)
  common(r.line.u)
  gl.uniform3fv(r.line.u.uColor, unit(opts.edgeColor))
  gl.bindVertexArray(r.edgeVao)
  if (!deform) gl.drawArrays(gl.LINES, 0, r.edgeCount)     // edges are undeformed geometry, so hide them while deflected
  if (opts.mode === 'stress' && opts.peak) {           // the peak marker stays visible even when it's behind something
    const q = [opts.peak[0] - geo.center[0], opts.peak[1] - geo.center[1], opts.peak[2] - geo.center[2]]
    const px = R[0][0] * q[0] + R[0][1] * q[1] + R[0][2] * q[2], py = R[1][0] * q[0] + R[1][1] * q[1] + R[1][2] * q[2]
    const d = 6 / s
    gl.disable(gl.DEPTH_TEST)
    gl.uniform3fv(r.line.u.uR0, [1, 0, 0]); gl.uniform3fv(r.line.u.uR1, [0, 1, 0]); gl.uniform3fv(r.line.u.uR2, [0, 0, 0])
    gl.uniform3fv(r.line.u.uColor, unit(opts.textColor))
    gl.bindVertexArray(r.markVao)
    gl.bindBuffer(gl.ARRAY_BUFFER, r.markBuf)
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([px - d, py - d, 0, px + d, py + d, 0, px + d, py - d, 0, px - d, py + d, 0]), gl.DYNAMIC_DRAW)
    gl.drawArrays(gl.LINES, 0, 4)
  }
  if (opts.glyphs && (opts.glyphs.loads.length || opts.glyphs.supports.length)) {   // load arrows and support symbols
    const L = geo.radius * 0.22, c = geo.center
    const rel = (p) => [p[0] - c[0], p[1] - c[1], p[2] - c[2]]
    const right = R[0], up = R[1]
    const seg = (out, a, b) => out.push(...rel(a), ...rel(b))
    const arrows = [], sups = []
    for (const g of opts.glyphs.loads) {
      const n = Math.hypot(...g.dir) || 1, d = g.dir.map(v => v / n)
      const tail = [0, 1, 2].map(i => g.at[i] - d[i] * L)
      seg(arrows, tail, g.at)
      let side = [d[1] * R[2][2] - d[2] * R[2][1], d[2] * R[2][0] - d[0] * R[2][2], d[0] * R[2][1] - d[1] * R[2][0]]
      const sn = Math.hypot(...side) || 1
      side = side.map(v => v / sn)
      for (const sgn of [1, -1]) seg(arrows, g.at, [0, 1, 2].map(i => g.at[i] - d[i] * L * 0.28 + sgn * side[i] * L * 0.12))
    }
    for (const p of opts.glyphs.supports) {
      const a = 0.05 * geo.radius
      const l = [0, 1, 2].map(i => p[i] - up[i] * a * 1.6 - right[i] * a), rr = [0, 1, 2].map(i => p[i] - up[i] * a * 1.6 + right[i] * a)
      seg(sups, p, l); seg(sups, l, rr); seg(sups, rr, p)
    }
    gl.disable(gl.DEPTH_TEST)
    gl.useProgram(r.line.p)
    common(r.line.u)
    gl.bindVertexArray(r.markVao)
    gl.bindBuffer(gl.ARRAY_BUFFER, r.markBuf)
    for (const [list, col] of [[arrows, opts.loadColor], [sups, opts.supColor]]) {
      if (!list.length) continue
      gl.uniform3fv(r.line.u.uColor, unit(col))
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(list), gl.DYNAMIC_DRAW)
      gl.drawArrays(gl.LINES, 0, list.length / 3)
    }
  }
  gl.bindVertexArray(null)
  return { R, s, cx, cy, w, h, deform }
}

function pick(frame, geo, mx, my) {
  if (!frame || !geo) return null
  const { R, s, cx, cy } = frame
  const { T, F, center } = geo
  let V = geo.V
  if (frame.deform && geo.D) {                      // pick what is drawn: the deflected shape
    V = Float32Array.from(geo.V, (v, i) => v + geo.D[i] * frame.deform)
  }
  const nv = V.length / 3
  const P = new Float32Array(nv * 3)
  for (let i = 0; i < nv; i++) {
    const x = V[3 * i] - center[0], y = V[3 * i + 1] - center[1], z = V[3 * i + 2] - center[2]
    P[3 * i] = cx + s * (R[0][0] * x + R[0][1] * y + R[0][2] * z)
    P[3 * i + 1] = cy - s * (R[1][0] * x + R[1][1] * y + R[1][2] * z)
    P[3 * i + 2] = R[2][0] * x + R[2][1] * y + R[2][2] * z
  }
  let best = null, bestZ = Infinity
  for (let k = 0; k < geo.nt; k++) {
    const a = T[3 * k], b = T[3 * k + 1], c = T[3 * k + 2]
    const x0 = P[3 * a], y0 = P[3 * a + 1], x1 = P[3 * b], y1 = P[3 * b + 1], x2 = P[3 * c], y2 = P[3 * c + 1]
    if (mx < Math.min(x0, x1, x2) || mx > Math.max(x0, x1, x2) || my < Math.min(y0, y1, y2) || my > Math.max(y0, y1, y2)) continue
    const d = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    if (Math.abs(d) < 1e-9) continue
    const l0 = ((y1 - y2) * (mx - x2) + (x2 - x1) * (my - y2)) / d
    const l1 = ((y2 - y0) * (mx - x2) + (x0 - x2) * (my - y2)) / d
    const l2 = 1 - l0 - l1
    if (l0 < -1e-4 || l1 < -1e-4 || l2 < -1e-4) continue
    const z = l0 * P[3 * a + 2] + l1 * P[3 * b + 2] + l2 * P[3 * c + 2]     // nearest surface at this pixel
    if (z < bestZ) {
      bestZ = z
      const U = geo.V                                // report the point on the undeformed model
      const pt = [0, 1, 2].map(j => l0 * U[3 * a + j] + l1 * U[3 * b + j] + l2 * U[3 * c + j])
      best = { face: F[k], point: pt.map(v => Math.round(v * 100) / 100), bary: [a, b, c, l0, l1, l2] }
    }
  }
  return best
}

// ------------------------------------------------------------------ selector text
function axisPlane(face) {
  if (!face || face.type !== 'Plane' || !face.normal) return null
  const n = face.normal
  for (let i = 0; i < 3; i++) {
    if (Math.abs(Math.abs(n[i]) - 1) < 1e-3) return { plane: 'xyz'[i], value: face.centroid[i] }
  }
  return null
}

function selectorFor(face, point) {
  const ap = axisPlane(face)
  if (ap) return { plane: ap.plane, value: Math.round(ap.value * 1000) / 1000 }
  return { near: point }
}

// Arrows for the latest plan's loads and markers for its supports, at the points the plan refers to
function glyphsFrom(scene) {
  const plans = Object.values((scene && scene.plans) || {})
  const out = { loads: [], supports: [] }
  if (!plans.length) return out
  const p = plans[plans.length - 1]
  for (const ld of p.loads || []) for (const f of ld.faces || []) if (f.near) out.loads.push({ at: f.near, dir: ld.force_N })
  for (const s of p.supports || []) if (s.ref && s.ref.near) out.supports.push(s.ref.near)
  return out
}

// Inventor-style boundary-condition glyphs: several filled arrows spread over each load face (with the force
// written once per load) and hatched ground symbols on supports. Drawn as SVG over the canvas: WebGL lines are 1 px.
function buildGlyphs(geo, scene, visibleGeo = geo) {
  const g = glyphsFrom(scene)
  if (!geo || (!g.loads.length && !g.supports.length)) return null
  const { T, F, V } = geo
  const cen = new Float32Array(geo.nt * 3)
  for (let k = 0; k < geo.nt; k++) for (let j = 0; j < 3; j++)
    cen[3 * k + j] = (V[3 * T[3 * k] + j] + V[3 * T[3 * k + 1] + j] + V[3 * T[3 * k + 2] + j]) / 3
  const d2 = (k, p) => (cen[3 * k] - p[0]) ** 2 + (cen[3 * k + 1] - p[1]) ** 2 + (cen[3 * k + 2] - p[2]) ** 2
  const spread = (near, n) => {                     // n well-spread points on the face nearest `near`
    let best = 0, bd = Infinity
    for (let k = 0; k < geo.nt; k++) { const d = d2(k, near); if (d < bd) { bd = d; best = k } }
    if (!geo.nt || !visibleGeo || !visibleGeo.faces.has(F[best])) return []
    const anchor = k => {
      const point = [cen[3 * k], cen[3 * k + 1], cen[3 * k + 2]]
      point.triangle = [T[3 * k], T[3 * k + 1], T[3 * k + 2]]
      point.barycentric = [1 / 3, 1 / 3, 1 / 3]
      point.face = F[k]
      point.displacement = [0, 1, 2].map(j => geo.D ? point.triangle.reduce((sum, v) => sum + geo.D[3 * v + j] / 3, 0) : 0)
      return point
    }
    const tris = []
    for (let k = 0; k < geo.nt; k++) if (F[k] === F[best]) tris.push(k)
    const pts = [anchor(best)]
    while (pts.length < n && pts.length < tris.length) {
      let far = -1, fd = 0
      for (const k of tris) { const m = Math.min(...pts.map(q => d2(k, q))); if (m > fd) { fd = m; far = k } }
      if (far < 0 || fd < (geo.radius * 0.02) ** 2) break
      pts.push(anchor(far))
    }
    return pts
  }
  const plans = Object.values(scene.plans || {}), plan = plans[plans.length - 1]
  const loads = (plan.loads || []).map(ld => {
    const pts = (ld.faces || []).filter(f => f.near).flatMap(f => spread(f.near, 3))
    return { pts, dir: ld.force_N, label: `${fmt(Math.hypot(...ld.force_N))} N`, name: ld.name }
  }).filter(l => l.pts.length)
  const supports = (plan.supports || []).filter(sp => sp.ref && sp.ref.near).map(sp => ({ pts: spread(sp.ref.near, 2), kind: sp.kind }))
  return { loads, supports }
}

function renderGlyphSvg(svg, frame, geo, gly, deform) {
  if (!svg) return
  if (!frame || !gly) { svg.innerHTML = ''; return }
  const L = geo.radius * 0.16
  const moved = p => p.map((v, j) => v + ((p.displacement || [])[j] || 0) * (deform || 0))
  const P = (p) => project(frame, geo, p)
  let out = ''
  for (const ld of gly.loads) {
    const n = Math.hypot(...ld.dir) || 1, d = ld.dir.map(v => v / n)
    ld.pts.forEach((anchor, i) => {
      const pt = moved(anchor)
      const [tx, ty] = P(pt), [bx, by] = P(pt.map((v, j) => v - d[j] * L))
      const ux = tx - bx, uy = ty - by, ul = Math.hypot(ux, uy) || 1, ex = ux / ul, ey = uy / ul
      const hl = Math.min(13, ul * 0.45), hw = hl * 0.5
      const hx = tx - ex * hl, hy = ty - ey * hl
      out += `<line x1="${bx}" y1="${by}" x2="${hx}" y2="${hy}" stroke="#3b2f00" stroke-width="4.5" stroke-linecap="round"/>`
      out += `<line x1="${bx}" y1="${by}" x2="${hx}" y2="${hy}" stroke="#facc15" stroke-width="2.5" stroke-linecap="round"/>`
      out += `<polygon points="${tx},${ty} ${hx - ey * hw},${hy + ex * hw} ${hx + ey * hw},${hy - ex * hw}" fill="#facc15" stroke="#3b2f00" stroke-width="1"/>`
      if (i === 0) out += `<g transform="translate(${bx},${by - 10})"><rect x="-4" y="-12" rx="4" width="${ld.label.length * 6.6 + 8}" height="16" fill="rgba(17,24,39,.82)"/>` +
        `<text x="0" y="0" fill="#fde68a" font-size="11" font-family="ui-monospace,Consolas,monospace">${ld.label}</text></g>`
    })
  }
  for (const sp of gly.supports) for (const pt of sp.pts) {
    const [x, y] = P(moved(pt)), a = 7
    out += `<polygon points="${x},${y} ${x - a},${y + a * 1.5} ${x + a},${y + a * 1.5}" fill="#22c55e" stroke="#14532d" stroke-width="1"/>`
    out += `<line x1="${x - a - 3}" y1="${y + a * 1.5 + 1}" x2="${x + a + 3}" y2="${y + a * 1.5 + 1}" stroke="#14532d" stroke-width="1.5"/>`
    for (let h = -a; h <= a; h += 4) out += `<line x1="${x + h}" y1="${y + a * 1.5 + 1}" x2="${x + h - 3}" y2="${y + a * 1.5 + 5}" stroke="#14532d" stroke-width="1"/>`
  }
  svg.innerHTML = out
}

function round1(v) { return Math.round(v * 10) / 10 }

// A short reference the agent can resolve: the face's id in this view plus a point on it (Inventor's own face numbers
// differ from the viewer's, so the point is what the tools use).
function refToken(face, point, name) {
  return `@${name || 'Face_' + face.id}{near:[${point.map(round1).join(',')}]}`
}

function project(frame, geo, p) {
  const { R, s, cx, cy } = frame
  const q = [p[0] - geo.center[0], p[1] - geo.center[1], p[2] - geo.center[2]]
  return [cx + s * (R[0][0] * q[0] + R[0][1] * q[1] + R[0][2] * q[2]), cy - s * (R[1][0] * q[0] + R[1][1] * q[1] + R[1][2] * q[2])]
}

function inPolygon(x, y, poly) {
  let inside = false
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j]
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside
  }
  return inside
}

// Faces whose triangles fall inside a lasso drawn on the view
function lassoFaces(frame, geo, poly) {
  const out = new Set()
  if (!frame || poly.length < 3) return out
  const { T, F } = geo
  const V = frame.deform && geo.D ? Float32Array.from(geo.V, (v, i) => v + geo.D[i] * frame.deform) : geo.V
  for (let k = 0; k < geo.nt; k++) {
    if (out.has(F[k])) continue
    const c = [0, 1, 2].map(j => (V[3 * T[3 * k] + j] + V[3 * T[3 * k + 1] + j] + V[3 * T[3 * k + 2] + j]) / 3)
    const [x, y] = project(frame, geo, c)
    if (inPolygon(x, y, poly)) out.add(F[k])
  }
  return out
}

// Connected regions where the shown result is above a level, largest peak first: "high-stress zones"
function zonesAbove(geo, fd, level) {
  const { T, F, V } = geo
  const parent = new Int32Array(V.length / 3).map((_, i) => i)
  const find = (i) => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i] } return i }
  const hot = []
  for (let k = 0; k < geo.nt; k++) {
    const a = T[3 * k], b = T[3 * k + 1], c = T[3 * k + 2]
    if ((fd.values[a] + fd.values[b] + fd.values[c]) / 3 < level) continue
    hot.push(k)
    const ra = find(a); parent[find(b)] = ra; parent[find(c)] = ra
  }
  const zones = new Map()
  for (const k of hot) {
    const root = find(T[3 * k])
    let z = zones.get(root)
    if (!z) { z = { faces: new Set(), tris: 0, peak: -Infinity, at: null }; zones.set(root, z) }
    z.faces.add(F[k]); z.tris++
    for (let j = 0; j < 3; j++) {
      const v = T[3 * k + j]
      if (fd.values[v] > z.peak) { z.peak = fd.values[v]; z.at = [V[3 * v], V[3 * v + 1], V[3 * v + 2]] }
    }
  }
  return [...zones.values()].sort((p, q) => q.peak - p.peak).slice(0, 8)
    .map((z, i) => ({ ...z, name: `HighStress_Zone_${String.fromCharCode(65 + i)}` }))
}

function describe(face, point) {
  if (!face) return ''
  const bits = [`face F${face.id}`, face.type]
  if (face.diameter) bits.push(`Ø${face.diameter}`)
  bits.push(`${Math.round(face.area)} mm²`)
  if (face.normal) bits.push(`normal (${face.normal.map(v => +v.toFixed(2)).join(', ')})`)
  bits.push(`picked at (${point.join(', ')})`)
  return bits.join(' · ')
}

// ------------------------------------------------------------------ UI bits
const css = {
  root: { display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden', fontSize: 12, color: 'var(--ui-text-secondary)' },
  below: { flex: '0 1 auto', maxHeight: '42%', overflowY: 'auto', overscrollBehavior: 'contain' },
  bar: { display: 'flex', alignItems: 'center', gap: 6, padding: '6px 8px', borderBottom: '1px solid var(--ui-stroke-secondary)' },
  title: { flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--ui-text-primary, inherit)', fontWeight: 500 },
  btn: (on) => ({ padding: '2px 8px', borderRadius: 6, border: '1px solid var(--ui-stroke-secondary)', background: on ? 'var(--ui-accent)' : 'transparent', color: on ? 'var(--ui-text-on-accent, #fff)' : 'inherit', cursor: 'pointer', fontSize: 11 }),
  canvasWrap: { position: 'relative', flex: '1 1 58%', minHeight: 200 },
  canvas: { width: '100%', height: '100%', display: 'block', cursor: 'grab', touchAction: 'none' },
  hint: { position: 'absolute', left: 8, bottom: 6, fontSize: 10, color: 'var(--ui-text-quaternary)' },
  section: { padding: '5px 10px', borderTop: '1px solid var(--ui-stroke-secondary)', lineHeight: 1.35 },
  label: { fontSize: 10, textTransform: 'uppercase', letterSpacing: 0.4, color: 'var(--ui-text-tertiary)', marginBottom: 4 },
  row: { display: 'flex', gap: 5, flexWrap: 'wrap', marginTop: 4 },
  kv: { display: 'grid', gridTemplateColumns: 'auto 1fr', columnGap: 10, rowGap: 2 },
  warn: { color: 'var(--ui-warning, #b45309)', marginTop: 4 },
  ev: { padding: '2px 0', borderBottom: '1px dashed var(--ui-stroke-secondary)' },
  mono: { fontFamily: 'var(--ui-font-mono, ui-monospace, monospace)', fontSize: 11 },
}

// colour-scale shapes: t in 0..1 -> position on the colour bar (must match the shader)
const SCALES = ['linear', 'balanced', 'log']
const scaleFwd = (scale, t) => scale === 'linear' ? t : scale === 'log' ? Math.log(1 + 99 * t) / Math.log(100) : Math.pow(t, 0.45)
const scaleInv = (scale, u) => scale === 'linear' ? u : scale === 'log' ? (Math.pow(100, u) - 1) / 99 : Math.pow(u, 1 / 0.45)

function fieldRange(geo, rv) {
  const fd = geo.fields[Math.min(rv.field || 0, geo.fields.length - 1)]
  return { fd, lo: rv.lo != null ? rv.lo : fd.vmin, hi: rv.hi != null ? rv.hi : fd.vmax }
}

function Legend({ geo, rv }) {
  if (!geo || !geo.triVal || !geo.fields.length) return null
  const { fd, lo, hi } = fieldRange(geo, rv)
  const fn = PALETTES[rv.palette] || turbo
  const n = rv.bands || 0
  const parts = []
  if (n) {
    for (let i = 0; i < n; i++) {
      const [r, g, b] = fn((i + 0.5) / n)
      parts.push(`rgb(${r},${g},${b}) ${(i * 100 / n).toFixed(2)}% ${((i + 1) * 100 / n).toFixed(2)}%`)
    }
  } else {
    for (let i = 0; i <= 12; i++) { const [r, g, b] = fn(i / 12); parts.push(`rgb(${r},${g},${b}) ${(i * 100 / 12).toFixed(2)}%`) }
  }
  return jsxs('div', {
    'data-testid': 'legend',
    style: { position: 'absolute', right: 8, top: 8, width: 150, fontSize: 10, color: 'var(--ui-text-tertiary)' },
    children: [
      jsx('div', { children: fd.label }),
      jsx('div', { style: { height: 10, borderRadius: 3, background: `linear-gradient(90deg, ${parts.join(', ')})`, margin: '2px 0' } }),
      jsxs('div', { style: { display: 'flex', justifyContent: 'space-between' },
        children: [0, 0.25, 0.5, 0.75, 1].map(u => jsx('span', { children: fmt(lo + (hi - lo) * scaleInv(rv.scale || 'balanced', u)) }, u)) }),
      rv.hi != null && jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 4, marginTop: 2 }, children: [
        jsx('span', { style: { width: 10, height: 8, borderRadius: 2, background: `rgb(${OVER_RANGE.join(',')})` } }),
        jsx('span', { children: `above ${fmt(hi)}` }),
      ] }),
    ],
  })
}

function ResultControls({ geo, rv, setRv, fea, animate, setAnimate }) {
  if (!geo || !geo.fields.length) return null
  const { fd, lo, hi } = fieldRange(geo, rv)
  const up = (patch) => setRv(v => ({ ...v, ...patch }))
  const field = { padding: '1px 4px', fontSize: 11, borderRadius: 5, border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'inherit' }
  const num = { ...field, width: 58 }
  const lab = { display: 'flex', alignItems: 'center', gap: 4 }
  const step = (fd.vmax - fd.vmin) / 200 || 0.01
  const isStress = (rv.field || 0) === 0
  return jsxs('div', { style: css.section, 'data-testid': 'result-controls', children: [
    jsx('div', { style: css.label, children: 'Results display' }),
    jsxs('div', { style: css.row, children: [
      geo.fields.length > 1 && jsx('select', { 'aria-label': 'Result', style: field, value: rv.field || 0, onChange: e => up({ field: +e.target.value, lo: null, hi: null, cut: 0 }),
        children: geo.fields.map((f, i) => jsx('option', { value: i, children: f.label }, i)) }),
      jsx('select', { 'aria-label': 'Colours', style: field, value: rv.palette, onChange: e => up({ palette: e.target.value }),
        children: Object.keys(PALETTES).map(p => jsx('option', { value: p, children: p }, p)) }),
      jsx('select', { 'aria-label': 'Bands', style: field, value: rv.bands, onChange: e => up({ bands: +e.target.value }),
        children: [[0, 'Smooth'], [6, '6 bands'], [10, '10 bands'], [14, '14 bands']].map(([v, t]) => jsx('option', { value: v, children: t }, v)) }),
      jsx('select', { 'aria-label': 'Scale', style: field, value: rv.scale || 'balanced', title: 'How values map onto the colour bar (the numbers stay exact)', onChange: e => up({ scale: e.target.value }),
        children: SCALES.map(k => jsx('option', { value: k, children: k[0].toUpperCase() + k.slice(1) }, k)) }),
    ] }),
    jsxs('div', { style: { ...css.row, alignItems: 'center' }, children: [
      jsxs('label', { style: lab, children: ['Min', jsx('input', { type: 'number', 'aria-label': 'Minimum', style: num, step: 'any', value: rv.lo != null ? rv.lo : +fmt(fd.vmin), onChange: e => up({ lo: e.target.value === '' ? null : +e.target.value }) })] }),
      jsxs('label', { style: lab, children: ['Max', jsx('input', { type: 'number', 'aria-label': 'Maximum', style: num, step: 'any', value: rv.hi != null ? rv.hi : +fmt(fd.vmax), onChange: e => up({ hi: e.target.value === '' ? null : +e.target.value }) })] }),
      jsx('button', { type: 'button', style: css.btn(rv.lo == null && rv.hi == null), onClick: () => up({ lo: null, hi: null }), children: 'Auto' }),
      isStress && fea && fea.yield_MPa != null && jsx('button', { type: 'button', style: css.btn(rv.hi === fea.yield_MPa), title: 'Colour scale ends at yield; anything above shows in magenta', onClick: () => up({ lo: 0, hi: fea.yield_MPa }), children: 'Yield' }),
    ] }),
    jsxs('label', { style: { ...lab, marginTop: 6 }, children: [
      jsx('span', { style: { whiteSpace: 'nowrap' }, children: 'Highlight above' }),
      jsx('input', { type: 'range', 'aria-label': 'Highlight above', style: { flex: 1, minWidth: 0 }, min: 0, max: fd.vmax, step, value: rv.cut || 0, onChange: e => up({ cut: +e.target.value }) }),
      jsx('span', { style: { ...css.mono, width: 44, textAlign: 'right' }, children: rv.cut ? fmt(rv.cut) : 'off' }),
    ] }),
    geo.D && jsxs('label', { style: { ...lab, marginTop: 6 }, children: [
      jsx('span', { style: { whiteSpace: 'nowrap' }, children: 'Deformation' }),
      jsx('input', { type: 'range', 'aria-label': 'Deformation', style: { flex: 1, minWidth: 0 }, min: 0, max: 1, step: 0.01, value: rv.def || 0, onChange: e => up({ def: +e.target.value }) }),
      jsx('span', { style: { ...css.mono, width: 44, textAlign: 'right' }, children: rv.def ? `×${fmt(rv.def * maxDeform(geo))}` : 'off' }),
      jsx('button', { type: 'button', style: css.btn(animate), onClick: () => setAnimate(!animate), children: animate ? 'Stop' : 'Animate' }),
    ] }),
  ] })
}

// the deflection scale at which the largest movement is 12 % of the model's size
function maxDeform(geo) { return geo.dmax > 0 ? geo.radius * 0.24 / geo.dmax : 0 }

function FeaCard({ fea }) {
  if (!fea) return null
  const sf = fea.safety_factor_yield
  return jsxs('div', {
    style: css.section,
    children: [
      jsx('div', { style: css.label, children: `Analysis ${fea.evidence_id} · ${fea.label || ''}` }),
      jsxs('div', { style: css.kv, children: [
        jsx('span', { children: 'peak von Mises' }), jsx('span', { style: css.mono, children: `${fea.vm_max_MPa} MPa` }),
        jsx('span', { children: 'max displacement' }), jsx('span', { style: css.mono, children: `${fea.disp_max_mm} mm` }),
        jsx('span', { children: 'safety factor (yield)' }),
        jsx('span', { style: { ...css.mono, color: sf != null && sf < 1 ? 'var(--ui-danger, #dc2626)' : 'inherit' },
          children: sf != null ? `${sf}  (fy ${fea.yield_MPa} MPa, ${fea.material})` : '—' }),
        jsx('span', { children: 'equilibrium' }),
        jsx('span', { style: css.mono, children: fea.equilibrium_error != null ? `${(fea.equilibrium_error * 100).toFixed(2)} % error` : 'not checked' }),
        jsx('span', { children: 'mesh' }),
        jsx('span', { style: css.mono, children: fea.convergence ? (fea.convergence.converged ? `converged (Δσ ${(fea.convergence.vm_change * 100).toFixed(1)} %)` : `NOT converged (Δσ ${(fea.convergence.vm_change * 100).toFixed(0)} %)`) : 'single mesh' }),
      ] }),
      ...(fea.warnings || []).map((w, i) => jsx('div', { key: i, style: css.warn, children: `⚠ ${w}` })),
    ],
  })
}

function Evidence({ items }) {
  if (!items || !items.length) return null
  return jsxs('div', {
    style: css.section,
    children: [
      jsx('div', { style: css.label, children: 'Evidence log — figures in replies are checked against this' }),
      ...items.slice().reverse().map(r => jsxs('div', {
        key: r.id, style: css.ev,
        children: [
          jsx('span', { style: { ...css.mono, marginRight: 6 }, children: r.id }),
          jsx('span', { children: r.verified ? '✅ ' : '⚠️ ' }),
          jsx('span', { children: r.summary }),
        ],
      })),
    ],
  })
}

function ModelInfo({ info }) {
  if (!info || !Object.keys(info).length) return null
  const rows = [['part', info.name], ['material', info.material], ['mass', info.mass_kg != null ? `${fmt(info.mass_kg)} kg` : null],
                ['size', info.size_mm ? info.size_mm.map(v => fmt(v)).join(' × ') + ' mm' : null],
                ['volume', info.volume_mm3 != null ? `${fmt(info.volume_mm3 / 1000)} cm³` : null]].filter(r => r[1] != null)
  const params = Object.entries(info.parameters || {})
  return jsxs('div', { style: css.section, 'data-testid': 'model-info', children: [
    jsx('div', { style: css.label, children: 'Model' }),
    jsx('div', { style: css.kv, children: rows.flatMap(([k, v]) => [jsx('span', { children: k }, k), jsx('span', { style: css.mono, children: String(v) }, k + 'v')]) }),
    params.length > 0 && jsx('div', { style: { ...css.mono, marginTop: 4, color: 'var(--ui-text-tertiary)' },
      children: params.slice(0, 12).map(([k, v]) => `${k}=${typeof v === 'number' ? fmt(v) : v}`).join('  ') }),
  ] })
}

function PlanCards({ plans, edits, setEdits, send, submit }) {
  const list = Object.values(plans || {})
  if (!list.length) return null
  const p = list[list.length - 1]
  const e = edits[p.id] || {}
  const set = (patch) => setEdits({ ...edits, [p.id]: { ...e, ...patch } })
  const input = { width: 62, fontSize: 11, padding: '1px 3px' }
  const loadEdit = (i, axis, v) => {
    const loads = (e.loads || p.loads.map(l => ({ force_N: [...l.force_N] }))).map(l => ({ force_N: [...l.force_N] }))
    loads[i].force_N[axis] = v
    set({ loads })
  }
  const shownForce = (i) => (e.loads ? e.loads[i].force_N : p.loads[i].force_N)
  const changed = Object.keys(e).length > 0
  return jsxs('div', { style: css.section, 'data-testid': 'plan', children: [
    jsx('div', { style: css.label, children: `Plan ${p.id} · ${p.title} · ${p.status}` }),
    p.intent && jsx('div', { style: { marginBottom: 4, fontStyle: 'italic' }, children: p.intent }),
    jsx('div', { style: { ...css.mono, color: 'var(--ui-text-tertiary)', marginBottom: 4 }, 'data-testid': 'plan-meta',
      children: [p.material || 'model material', `mesh ${p.mesh || 'default'}`, p.gravity ? 'gravity on' : 'no gravity',
                 p.backend ? `on ${p.backend}` : null].filter(Boolean).join(' · ') }),
    p.estimates && jsxs('div', { style: { ...css.ev, borderLeft: '3px solid var(--ui-accent)' }, 'data-testid': 'plan-estimate', children: [
      p.estimates.exempt && jsx('div', { children: `CHECK SKIPPED: ${p.estimates.exempt}` }),
      !p.estimates.exempt && jsx('div', { children: [jsx('b', { children: 'Hand calc, before solving: ' }),
        [p.estimates.vm_max_MPa != null ? `σ ${fmt(p.estimates.vm_max_MPa)} MPa` : null,
         p.estimates.disp_max_mm != null ? `δ ${fmt(p.estimates.disp_max_mm)} mm` : null].filter(Boolean).join(' · ')] }),
      p.estimates.location && jsx('div', { children: `${p.estimates.stress_definition} at ${p.estimates.location} ? ${p.estimates.applicability}` }),
      p.estimates.working && jsx('div', { style: { ...css.mono, color: 'var(--ui-text-tertiary)', whiteSpace: 'pre-wrap' }, children: p.estimates.working }),
    ] }),
    p.assumptions && p.assumptions.length > 0 && jsxs('details', { style: { margin: '2px 0 4px' }, children: [
      jsx('summary', { style: { cursor: 'pointer', color: 'var(--ui-text-secondary)' }, children: `Assumptions (${p.assumptions.length})` }),
      ...p.assumptions.map((a, i) => jsx('div', { style: { ...css.mono, color: 'var(--ui-text-tertiary)' }, children: '· ' + a }, 'a' + i)),
    ] }),
    ...p.loads.map((l, i) => jsxs('div', { key: 'l' + i, style: { ...css.ev }, children: [
      jsx('div', { children: `${l.name} — ${l.basis}` }),
      jsx('div', { style: { ...css.mono, color: 'var(--ui-text-tertiary)' }, children: l.faces.map(f => f.label).join(', ') }),
      jsxs('div', { style: { ...css.row, marginTop: 2 }, children: ['Fx', 'Fy', 'Fz'].map((ax, k) => jsxs('label', { key: ax, style: { display: 'flex', gap: 3, alignItems: 'center' }, children: [ax,
        jsx('input', { type: 'number', step: 'any', style: input, 'aria-label': `${l.name} ${ax}`, disabled: p.status !== 'proposed',
          value: shownForce(i)[k], onChange: ev => loadEdit(i, k, +ev.target.value) })] })) }),
    ] })),
    ...p.supports.map((s, i) => jsxs('div', { key: 's' + i, style: { ...css.ev, display: 'flex', gap: 6, alignItems: 'center' }, children: [
      jsx('select', { style: { fontSize: 11 }, disabled: p.status !== 'proposed', value: (e.supports && e.supports[i] && e.supports[i].kind) || s.kind,
        onChange: ev => { const sp = (e.supports || p.supports.map(x => ({ kind: x.kind }))).map(x => ({ ...x })); sp[i].kind = ev.target.value; set({ supports: sp }) },
        children: ['fixed', 'frictionless', 'pin'].map(k => jsx('option', { value: k, children: k }, k)) }),
      jsx('span', { style: css.mono, children: s.ref.label }),
    ] })),
    jsxs('div', { style: { ...css.row, alignItems: 'center' }, children: [
      jsx('span', { children: 'SF ≥' }),
      jsx('input', { type: 'number', step: 0.1, style: input, disabled: p.status !== 'proposed', value: e.min_safety_factor != null ? e.min_safety_factor : p.criteria.min_safety_factor,
        onChange: ev => set({ min_safety_factor: +ev.target.value }) }),
      jsx('span', { style: css.mono, 'data-testid': 'plan-total', children: `total ${[0, 1, 2].map(k => fmt(p.loads.reduce((s, l, i) => s + (+shownForce(i)[k] || 0), 0))).join(', ')} N` }),
    ] }),
    p.status === 'proposed' && jsxs('div', { style: css.row, children: [
      jsx('button', { type: 'button', style: css.btn(true), 'data-testid': 'approve',
        onClick: () => (submit || send)(changed ? `Run plan ${p.id} with these edits: ${JSON.stringify(e)}` : `Run plan ${p.id} as proposed.`), children: changed ? 'Approve with edits' : 'Approve & run' }),
      changed && jsx('button', { type: 'button', style: css.btn(false), onClick: () => set({}) || setEdits({ ...edits, [p.id]: {} }), children: 'Undo edits' }),
    ] }),
    p.verdict && p.verdict.map((v, i) => jsx('div', { key: 'v' + i, 'data-testid': 'verdict', style: { ...css.mono, fontWeight: 600,
      color: v.startsWith('PASS') || v.startsWith('CHECK OK') ? 'var(--ui-success, #15803d)' : v.startsWith('CHECK') ? 'var(--ui-warning, #b45309)' : 'var(--ui-danger, #dc2626)' },
      children: (v.startsWith('PASS') || v.startsWith('CHECK OK') ? '✓ ' : '⚠ ') + v })),
    p.result && p.result.cross_check && !p.result.cross_check.error && jsx('div', { style: { ...css.mono, color: 'var(--ui-text-tertiary)' },
      children: `cross-check ${p.result.cross_check.solver}: σ ${fmt(p.result.cross_check.vm_max_MPa)} MPa` +
        (p.result.cross_check.peak_difference_pct != null ? ` (${p.result.cross_check.peak_difference_pct > 0 ? '+' : ''}${p.result.cross_check.peak_difference_pct}%)` : '') +
        ((p.result.cross_check.notes || []).length ? ' · ' + p.result.cross_check.notes.join('; ') : '') }),
  ] })
}

function History({ versions }) {
  if (!versions || !versions.length) return null
  return jsxs('div', { style: css.section, 'data-testid': 'history', children: [
    jsx('div', { style: css.label, children: 'Design history' }),
    ...versions.slice().reverse().slice(0, 12).map(v => jsxs('div', { key: v.id, style: css.ev, children: [
      jsxs('div', { children: [jsx('b', { children: v.id }), ` ${v.label}`,
        v.result && v.result.vm_max_MPa != null ? jsx('span', { style: { ...css.mono, marginLeft: 6 }, children: `σ ${fmt(v.result.vm_max_MPa)} MPa · SF ${fmt(v.result.safety_factor)}` }) : null] }),
      v.diff_text && v.diff_text !== 'first version' && jsx('div', { style: { ...css.mono, color: 'var(--ui-text-tertiary)' }, children: v.diff_text }),
    ] })),
  ] })
}

function Artifacts({ items }) {
  if (!items || !items.length) return null
  return jsxs('div', { style: css.section, 'data-testid': 'artifacts', children: [
    jsx('div', { style: css.label, children: 'Artifacts' }),
    ...items.slice().reverse().slice(0, 10).map((a, i) => jsxs('div', { key: i, style: { ...css.ev, display: 'flex', gap: 6, alignItems: 'center' }, children: [
      jsx('span', { style: { flex: 1 }, children: `${a.kind}: ${a.title}` }),
      jsx('button', { type: 'button', style: css.btn(false), title: a.path, onClick: () => navigator.clipboard && navigator.clipboard.writeText(a.pdf || a.path), children: 'Copy path' }),
    ] })),
  ] })
}

// ------------------------------------------------------------------ the pane
function makePane(ctx) {
  return function CadPane() {
    const canvasRef = useRef(null)
    const frameRef = useRef(null)
    const dragRef = useRef(null)
    const [scene, setScene] = useState(null)
    const [error, setError] = useState(null)
    const [cam, setCam] = useState({ yaw: -0.75, pitch: 0.6, zoom: 1, panX: 0, panY: 0 })
    const [mode, setMode] = useState('model')
    const [sel, setSel] = useState(null)
    const [size, setSize] = useState(0)
    const [noGl, setNoGl] = useState(false)
    const [picked, setPicked] = useState(new Set())      // multi-selection (ctrl-click, lasso, zones)
    const [lasso, setLasso] = useState(false)
    const [poly, setPoly] = useState(null)
    const [autoRef, setAutoRef] = useState(true)
    const [tip, setTip] = useState(null)
    const tipAt = useRef(0)
    const [zoneLevel, setZoneLevel] = useState(null)
    const [planEdits, setPlanEdits] = useState({})
    const [rv, setRv] = useState({ field: 0, palette: 'Rainbow', bands: 0, lo: null, hi: null, cut: 0, def: 0, scale: 'balanced' })
    const [animate, setAnimate] = useState(false)
    const [phase, setPhase] = useState(0)
    useEffect(() => {
      if (!animate) return undefined
      const id = setInterval(() => setPhase(p => p + 0.12), 40)
      return () => clearInterval(id)
    }, [animate])
    const revRef = useRef(null)
    const focused = useValue(host.state.focusedSessionId)

    const [hiddenParts, setHiddenParts] = useState(new Set())
    const [showParts, setShowParts] = useState(false)
    const geo = useMemo(() => (scene && scene.mesh ? prepare(withoutParts(scene.mesh, hiddenParts)) : null), [scene, hiddenParts])
    const glyphRef = useRef(null)
    const [showBc, setShowBc] = useState(true)
    const fullGeo = useMemo(() => scene && scene.mesh ? prepare(scene.mesh) : null, [scene && scene.mesh])
    const glyphData = useMemo(() => buildGlyphs(fullGeo, scene, geo), [fullGeo, geo, scene && scene.plans])
    const meshKey = scene && scene.mesh ? scene.mesh.rev || JSON.stringify(scene.mesh) : ''
    useEffect(() => { setPicked(new Set()); setHiddenParts(new Set()); setZoneLevel(null); setSel(null) }, [meshKey])
    useEffect(() => {
      setPicked(prev => new Set([...prev].filter(fid => geo && geo.faces.has(fid))))
      setSel(prev => prev && geo && geo.faces.has(prev.face) ? prev : null)
    }, [geo])
    const [sheet, setSheet] = useState(null)          // { data_url, title, issues, at }
    const [showSheet, setShowSheet] = useState(false)
    const sheetAt = scene && scene.drawing ? scene.drawing.at : null
    useEffect(() => {
      if (!sheetAt) return
      let live = true
      ctx.rest('/drawing').then(d => {
        if (!live || !d || !d.data_url) return
        setSheet(d)
        setShowSheet(true)                            // a new sheet was rendered: show it
      }).catch(() => {})
      return () => { live = false }
    }, [sheetAt])

    const load = useCallback(async (force) => {
      try {
        const r = await ctx.rest('/rev')
        if (!force && r && r.rev === revRef.current) return
        const s = await ctx.rest('/scene')
        revRef.current = s && s.rev
        setScene(s)
        setError(null)
        if (s && s.kind === 'fea') setMode('stress')
        if (s && s.drawing && s.drawing.kind === 'result' && (s.drawing.at || 0) >= (s.at || 0) - 2) { /* a fresh Inventor result stays on show */ }
        else if (s && (!s.drawing || (s.kind !== 'model' && s.kind !== 'fea') || s.at > (s.drawing.at || 0))) setShowSheet(false)
        setSel(null)
      } catch (e) {
        setError(String((e && e.message) || e))
      }
    }, [])

    useEffect(() => {
      load(true)
      const stopPoll = ctx.setInterval(() => load(false), POLL_MS)
      const stopEvt = ctx.onEvent('plugin.hermes-shaperotator.scene.updated', () => load(false))
      return () => { stopPoll(); stopEvt() }
    }, [load])

    // redraw on any change, and on resize
    useEffect(() => {
      const el = canvasRef.current
      if (!el) return
      const ro = new ResizeObserver(() => setSize(n => n + 1))
      ro.observe(el)
      return () => ro.disconnect()
    }, [])

    useEffect(() => {
      const el = canvasRef.current
      if (!el) return
      const opts = {
        mode, selected: sel ? sel.face : null,
        base: [154, 164, 178],   // neutral steel grey; theme text colours can be near-black
        selColor: parseColor(cssVar(el, '--ui-accent', '#6366f1'), '#6366f1'),
        supColor: [80, 140, 230], loadColor: [235, 140, 60],
        edgeColor: [38, 44, 54],   // dark in every theme: edges are drawn on the model, not the page
        textColor: parseColor(cssVar(el, '--ui-text-secondary', '#111827'), '#111827'),
        peak: scene && scene.fea && (rv.field || 0) === 0 ? scene.fea.vm_max_at : null,
        result: rv,
        picked: picked.size ? { faces: picked, key: [...picked].sort().join(',') } : null,
        glyphs: null,
        deform: geo ? (animate ? (0.5 - 0.5 * Math.cos(phase)) * (rv.def || 1) : rv.def || 0) * maxDeform(geo) : 0,
      }
      let frame = null
      try { frame = draw(el, geo, cam, opts) } catch (err) { console.error('hermes-shaperotator: render failed', err) }
      frameRef.current = frame
      renderGlyphSvg(glyphRef.current, showBc ? frame : null, geo, glyphData, opts.deform)
      setNoGl(Boolean(geo && !frame))
    }, [geo, cam, mode, sel, size, scene, rv, animate, phase, picked, showBc, glyphData])

    const onPointerDown = (e) => {
      e.currentTarget.setPointerCapture(e.pointerId)
      const r = e.currentTarget.getBoundingClientRect()
      dragRef.current = { x: e.clientX, y: e.clientY, moved: false, pan: e.shiftKey || e.button === 2, cam, ctrl: e.ctrlKey || e.metaKey }
      if (lasso) setPoly([[e.clientX - r.left, e.clientY - r.top]])
      setTip(null)
    }
    const onPointerMove = (e) => {
      const d = dragRef.current
      const r = e.currentTarget.getBoundingClientRect()
      if (!d) {                                          // hover: what's under the pointer
        const now = performance.now()
        if (now - tipAt.current < 90 || !geo) return
        tipAt.current = now
        const hit = pick(frameRef.current, geo, e.clientX - r.left, e.clientY - r.top)
        if (!hit) { setTip(null); return }
        const f = geo.faces.get(hit.face)
        let val = ''
        if (mode === 'stress' && geo.fields.length) {
          const fd = fieldRange(geo, rv).fd, [a, b, c, l0, l1, l2] = hit.bary
          val = ` · ${fd.label.split(' (')[0]} ${fmt(l0 * fd.values[a] + l1 * fd.values[b] + l2 * fd.values[c])}`
        }
        setTip({ x: e.clientX - r.left, y: e.clientY - r.top,
                 text: `@Face_${hit.face} · ${f ? f.type : ''}${f && f.diameter ? ' Ø' + f.diameter : ''}${f ? ' · ' + Math.round(f.area) + ' mm²' : ''}${val}` })
        return
      }
      if (lasso) { setPoly(p => (p ? [...p, [e.clientX - r.left, e.clientY - r.top]] : p)); d.moved = true; return }
      const dx = e.clientX - d.x, dy = e.clientY - d.y
      if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true
      if (!d.moved) return
      if (d.pan) setCam({ ...d.cam, panX: d.cam.panX + dx, panY: d.cam.panY + dy })
      else setCam({ ...d.cam, yaw: d.cam.yaw + dx * 0.01, pitch: Math.max(-1.55, Math.min(1.55, d.cam.pitch + dy * 0.01)) })
    }
    const onPointerUp = (e) => {
      const d = dragRef.current
      dragRef.current = null
      if (lasso && poly) {
        const faces = lassoFaces(frameRef.current, geo, poly)
        setPicked(prev => (d && d.ctrl ? new Set([...prev, ...faces]) : faces))
        setPoly(null)
        return
      }
      if (!d || d.moved) return
      const r = e.currentTarget.getBoundingClientRect()
      const hit = pick(frameRef.current, geo, e.clientX - r.left, e.clientY - r.top)
      if (hit && d.ctrl) {                               // ctrl-click: add to / remove from the selection
        setPicked(prev => { const n = new Set(prev); n.has(hit.face) ? n.delete(hit.face) : n.add(hit.face); return n })
        return
      }
      const fresh = hit && (!sel || hit.face !== sel.face || hit.point.join() !== sel.point.join())
      setSel(fresh ? hit : null)
      if (fresh && autoRef) sendText(refToken(geo.faces.get(hit.face) || { id: hit.face }, hit.point) + ' ')
    }
    const onWheel = (e) => {
      setCam(c => ({ ...c, zoom: Math.max(0.2, Math.min(20, c.zoom * Math.exp(-e.deltaY * 0.0015))) }))
    }

    const face = sel && geo ? geo.faces.get(sel.face) : null
    let probe = null
    if (sel && sel.bary && geo && mode === 'stress' && geo.fields.length) {
      const fd = fieldRange(geo, rv).fd, [a, b, c, l0, l1, l2] = sel.bary
      probe = { label: fd.label, value: l0 * fd.values[a] + l1 * fd.values[b] + l2 * fd.values[c] }
    }
    const sendText = async (text) => {
      let ok = false
      try { ok = await host.composer.insertText(focused || null, text, { mode: 'inline' }) } catch { ok = false }
      if (!ok) {
        try { await navigator.clipboard.writeText(text) } catch { /* ignore */ }
        host.notify({ kind: 'info', message: 'No chat input focused — copied to the clipboard.' })
      }
    }
    const submitText = async (text) => {             // Approve: send it as the user's message
      try {
        if (host.composer.submit && await host.composer.submit(focused || null, text)) return
      } catch { /* fall back to inserting */ }
      await sendText(text)
    }
    const pointOfFace = (fid) => {                       // a point on a face: the centre of its first triangle
      const k = geo.F.indexOf(fid)
      if (k < 0) return null
      return [0, 1, 2].map(j => (geo.V[3 * geo.T[3 * k] + j] + geo.V[3 * geo.T[3 * k + 1] + j] + geo.V[3 * geo.T[3 * k + 2] + j]) / 3)
    }
    const insertPicked = () => {
      if (!picked.size) return
      const refs = [...picked].filter(fid => geo && geo.faces.has(fid) && pointOfFace(fid)).slice(0, 24)
      if (refs.length) sendText(refs.map(fid => refToken(geo.faces.get(fid), pointOfFace(fid))).join(' ') + ' ')
    }
    const zones = useMemo(() => (geo && geo.fields.length && zoneLevel != null
      ? zonesAbove(geo, fieldRange(geo, rv).fd, zoneLevel) : []), [geo, zoneLevel, rv.field])
    const insert = async (role) => {
      if (!face) return
      const tok = refToken(face, sel.point)
      const text = role === 'support'
        ? `Support ${tok} `
        : role === 'load'
          ? `Load on ${tok} `
          : role === 'ask' && probe
            ? `The ${probe.label} is ${fmt(probe.value)} at ${tok}. Is that acceptable there, and what, if anything, would you change? `
            : `${tok} `
      sendText(text)
    }

    const empty = !geo
    return jsxs('div', {
      style: css.root,
      children: [
        jsxs('div', { style: css.bar, children: [
          jsx('div', { style: css.title, title: scene && scene.title,
            children: scene && scene.title ? `${scene.title}  ·  ${scene.backend}` : 'hermes-shaperotator' }),
          sheet && jsx('button', { type: 'button', style: css.btn(showSheet), onClick: () => setShowSheet(!showSheet), children: scene && scene.drawing && scene.drawing.kind === 'result' ? 'Inventor result' : 'Drawing' }),
          geo && geo.triVal && jsx('button', { type: 'button', style: css.btn(mode === 'stress' && !showSheet), onClick: () => { setShowSheet(false); setMode(mode === 'stress' ? 'model' : 'stress') }, children: 'Stress' }),
          geo && jsx('button', { type: 'button', style: css.btn(lasso), title: 'Draw round faces to select them (ctrl adds)', onClick: () => setLasso(!lasso), children: 'Lasso' }),
          scene && scene.mesh && scene.mesh.parts && jsx('button', { type: 'button', style: css.btn(showParts), 'data-testid': 'parts-btn',
            onClick: () => setShowParts(!showParts), children: hiddenParts.size ? `Parts (${scene.mesh.parts.length - hiddenParts.size}/${scene.mesh.parts.length})` : 'Parts' }),
          glyphData && jsx('button', { type: 'button', style: css.btn(showBc), title: 'Show loads and supports', onClick: () => setShowBc(!showBc), children: 'Loads' }),
          geo && jsx('button', { type: 'button', style: css.btn(autoRef), title: 'Clicking a face puts @Face_N into the chat box', onClick: () => setAutoRef(!autoRef), children: '@ on click' }),
          jsx('button', { type: 'button', style: css.btn(false), onClick: () => setCam({ yaw: -0.75, pitch: 0.6, zoom: 1, panX: 0, panY: 0 }), children: 'Reset' }),
        ] }),
        jsxs('div', { style: css.canvasWrap, children: [
          jsx('canvas', {
            ref: canvasRef, style: css.canvas,
            onPointerDown, onPointerMove, onPointerUp, onWheel,
            onContextMenu: (e) => e.preventDefault(),
          }),
          mode === 'stress' && !showSheet && jsx(Legend, { geo, rv }),
          poly && poly.length > 1 && jsx('svg', { style: { position: 'absolute', inset: 0, pointerEvents: 'none', width: '100%', height: '100%' },
            children: jsx('polygon', { points: poly.map(p => p.join(',')).join(' '), fill: 'rgba(99,102,241,0.12)', stroke: 'var(--ui-accent, #6366f1)', strokeWidth: 1.5 }) }),
          tip && !poly && jsx('div', { 'data-testid': 'tooltip', style: { position: 'absolute', left: tip.x + 12, top: tip.y + 12, transform: tip.x > 200 ? 'translateX(calc(-100% - 24px))' : 'none', pointerEvents: 'none',
            background: 'rgba(17,24,39,0.88)', color: '#fff', padding: '2px 6px', borderRadius: 4, fontSize: 11, whiteSpace: 'nowrap' }, children: tip.text }),
          noGl && jsx('div', { style: { position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', padding: 16, textAlign: 'center', color: 'var(--ui-text-tertiary)' },
            children: 'The 3D view could not start (WebGL 2 is unavailable here).' }),
          empty && jsx('div', { style: { position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', padding: 16, textAlign: 'center', color: 'var(--ui-text-tertiary)' },
            children: error ? `Can't reach the hermes-shaperotator backend: ${error}` : 'No model yet — ask Hermes to build or open a part (cad_* tools). It appears here live.' }),
          showParts && scene && scene.mesh && scene.mesh.parts && jsxs('div', { 'data-testid': 'parts', style: { position: 'absolute', left: 8, top: 8, maxHeight: '80%', overflowY: 'auto',
            background: 'rgba(255,255,255,0.94)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, padding: '6px 8px', fontSize: 11, zIndex: 2, minWidth: 150 }, children: [
            jsxs('div', { style: { display: 'flex', gap: 4, marginBottom: 4 }, children: [
              jsx('button', { type: 'button', style: css.btn(false), onClick: () => setHiddenParts(new Set()), children: 'All' }),
              jsx('button', { type: 'button', style: css.btn(false), 'data-testid': 'only-frame', title: 'Hide everything except the frame (what gets analysed)',
                onClick: () => setHiddenParts(new Set(scene.mesh.parts.filter(pt => partColour(pt.name) !== 0).map(pt => pt.id))), children: 'Frame only' }),
            ] }),
            ...scene.mesh.parts.map(pt => jsxs('label', { key: pt.id, style: { display: 'flex', gap: 5, alignItems: 'center', whiteSpace: 'nowrap' }, children: [
              jsx('input', { type: 'checkbox', checked: !hiddenParts.has(pt.id), onChange: () => { const h = new Set(hiddenParts); h.has(pt.id) ? h.delete(pt.id) : h.add(pt.id); setHiddenParts(h) } }),
              jsx('span', { style: { width: 9, height: 9, borderRadius: 2, background: `rgb(${PART_COLOURS[partColour(pt.name)].join(',')})` } }),
              pt.name] })),
          ] }),
          jsx('svg', { ref: glyphRef, 'data-testid': 'bc-glyphs', style: { position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none', overflow: 'visible' } }),
          !empty && jsx('div', { style: css.hint, children: lasso ? 'draw round faces to select them · ctrl adds' : 'drag: orbit · shift-drag: pan · wheel: zoom · click: @face into chat · ctrl-click: select several' }),
          showSheet && sheet && jsxs('div', {
            style: { position: 'absolute', inset: 0, overflow: 'auto', background: '#fff', display: 'flex', flexDirection: 'column' },
            children: [
              jsx('img', { src: sheet.data_url, alt: 'drawing sheet', style: { width: '100%', height: 'auto', display: 'block' } }),
              jsx('div', { style: { ...css.hint, position: 'static', padding: '4px 8px', color: sheet.issues ? 'var(--ui-warning, #b45309)' : '#555' },
                children: scene && scene.drawing && scene.drawing.kind === 'result' ? `${sheet.title || 'result'} · Autodesk Inventor Stress Analysis` : `${sheet.title || 'sheet'} · QA: ${sheet.issues == null ? 'not checked' : sheet.issues === 0 ? 'clean — export allowed' : `${sheet.issues} issue(s) — export blocked`}` }),
            ],
          }),
        ] }),
        jsx('div', { style: css.below, 'data-testid': 'below', children: [
        face && jsxs('div', { style: css.section, children: [
          jsx('div', { style: css.label, children: 'Selected face' }),
          jsx('div', { style: css.mono, children: describe(face, sel.point) }),
          probe && jsx('div', { style: { ...css.mono, fontWeight: 600, marginTop: 2 }, 'data-testid': 'probe', children: `${probe.label}: ${fmt(probe.value)}` }),
          jsx('div', { style: { ...css.mono, color: 'var(--ui-text-tertiary)', marginTop: 2 }, children: `selector ${JSON.stringify(selectorFor(face, sel.point))}` }),
          jsxs('div', { style: css.row, children: [
            jsx('button', { type: 'button', style: css.btn(false), onClick: () => insert('support'), children: 'Insert as support' }),
            jsx('button', { type: 'button', style: css.btn(false), onClick: () => insert('load'), children: 'Insert as load face' }),
            jsx('button', { type: 'button', style: css.btn(false), onClick: () => insert('ref'), children: 'Insert reference' }),
            probe && jsx('button', { type: 'button', style: css.btn(true), onClick: () => insert('ask'), children: 'Ask Hermes about this spot' }),
          ] }),
        ] }),
        picked.size > 0 && jsxs('div', { style: css.section, 'data-testid': 'selection', children: [
          jsx('div', { style: css.label, children: `${picked.size} face(s) selected` }),
          jsxs('div', { style: css.row, children: [
            jsx('button', { type: 'button', style: css.btn(true), onClick: insertPicked, children: 'Insert @faces' }),
            jsx('button', { type: 'button', style: css.btn(false), onClick: () => sendText(`Support ${[...picked].slice(0, 24).map(f => refToken(geo.faces.get(f) || { id: f }, pointOfFace(f))).join(' ')} `), children: 'As supports' }),
            jsx('button', { type: 'button', style: css.btn(false), onClick: () => sendText(`Load on ${[...picked].slice(0, 24).map(f => refToken(geo.faces.get(f) || { id: f }, pointOfFace(f))).join(' ')} `), children: 'As load faces' }),
            jsx('button', { type: 'button', style: css.btn(false), onClick: () => setPicked(new Set()), children: 'Clear' }),
          ] }),
        ] }),
        mode === 'stress' && !showSheet && geo && geo.fields.length > 0 && jsxs('div', { style: css.section, 'data-testid': 'zones', children: [
          jsxs('div', { style: { ...css.row, alignItems: 'center', marginTop: 0 }, children: [
            jsx('span', { style: css.label, children: 'Zones above' }),
            jsx('input', { type: 'number', 'aria-label': 'Zone level', step: 'any', style: { width: 70, fontSize: 11 },
              value: zoneLevel != null ? zoneLevel : '', placeholder: fmt((rv.cut || fieldRange(geo, rv).fd.vmax * 0.7)),
              onChange: e => setZoneLevel(e.target.value === '' ? null : +e.target.value) }),
            jsx('button', { type: 'button', style: css.btn(false), onClick: () => setZoneLevel(rv.cut || +fmt(fieldRange(geo, rv).fd.vmax * 0.7)), children: 'Find' }),
          ] }),
          ...zones.map(z => jsxs('div', { key: z.name, style: { ...css.ev, display: 'flex', alignItems: 'center', gap: 6 }, children: [
            jsx('span', { style: { ...css.mono, flex: 1 }, children: `${z.name.replace('HighStress_', '')} · peak ${fmt(z.peak)} · ${z.faces.size} face(s)` }),
            jsx('button', { type: 'button', style: css.btn(false), onClick: () => setPicked(new Set(z.faces)), children: 'Select' }),
            jsx('button', { type: 'button', style: css.btn(true), onClick: () => sendText(`@${z.name}{peak:${fmt(z.peak)}, near:[${z.at.map(round1).join(',')}]} `), children: '@ chat' }),
          ] })),
        ] }),
        mode === 'stress' && !showSheet && jsx(ResultControls, { geo, rv, setRv, fea: scene && scene.fea, animate, setAnimate }),
        scene && jsx(PlanCards, { plans: scene.plans, edits: planEdits, setEdits: setPlanEdits, send: sendText, submit: submitText }),
        scene && jsx(ModelInfo, { info: scene.model_info }),
        scene && jsx(History, { versions: scene.history }),
        scene && jsx(Artifacts, { items: scene.artifacts }),
        scene && scene.kind === 'fea' && jsx(FeaCard, { fea: scene.fea }),
        jsx(Evidence, { items: scene && scene.evidence }),
        ] }),
      ],
    })
  }
}

export default {
  id: 'hermes-shaperotator',
  name: 'ShapeRotator',
  register(ctx) {
    const Pane = makePane(ctx)
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'ShapeRotator',
      data: { placement: 'right', width: '420px' },
      render: () => jsx(Pane, {}),
    })
  },
}
