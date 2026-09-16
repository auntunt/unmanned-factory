/** Per-tab, per-actor recovery state for the single entry submission.
 *
 *  Kept in sessionStorage so two tabs never share a conversation/op key and a
 *  refresh in the same tab can resume the exact submission. Keyed by actor id so a
 *  different account never inherits another user's in-flight material. Only small
 *  metadata is stored — never file bytes. File content is identified by a SHA-256
 *  hash so a same-named but edited file is treated as different material and a
 *  partial upload lost to a refresh can be detected and re-requested. */
export type OpAttachment = { name: string; hash: string; size: number }
export type OpState = {
  op: string
  kind: 'dev' | 'chat'
  goal: string
  mode?: 'workspace' | 'import-files' | 'import-zip' // dev input mode, fixed at start
  agentId?: string
  cid?: string
  projectId?: string
  manifest?: OpAttachment[] // materials the op started with (dev: name+size; chat: +content hash)
  uploaded?: string[]
}

const keyFor = (uid?: string | number | null) => `webuddy:start:op:${uid ?? 'anon'}`
const draftKeyFor = (uid?: string | number | null) => `webuddy:start:draft:${uid ?? 'anon'}`

export function readOp(uid?: string | number | null): OpState | null {
  try { const raw = sessionStorage.getItem(keyFor(uid)); return raw ? (JSON.parse(raw) as OpState) : null } catch { return null }
}
export function writeOp(uid: string | number | null | undefined, state: OpState): void {
  try { sessionStorage.setItem(keyFor(uid), JSON.stringify(state)) } catch { /* storage is optional */ }
}
export function clearOp(uid?: string | number | null): void {
  try { sessionStorage.removeItem(keyFor(uid)) } catch { /* storage is optional */ }
}

// The entry draft (what the user typed) is per actor and per tab, like the op — so
// one account never backfills or overwrites another's text, and two tabs are isolated.
export function readDraft(uid?: string | number | null): string {
  try { return sessionStorage.getItem(draftKeyFor(uid)) || '' } catch { return '' }
}
export function writeDraft(uid: string | number | null | undefined, goal: string): void {
  try { sessionStorage.setItem(draftKeyFor(uid), goal) } catch { /* storage is optional */ }
}
export function clearDraft(uid?: string | number | null): void {
  try { sessionStorage.removeItem(draftKeyFor(uid)) } catch { /* storage is optional */ }
}

/** SHA-256 hex of a file's bytes, for content-addressed, retry-safe attachments.
 *  Uses the native WebCrypto in browsers; falls back to a pure-JS SHA-256 where
 *  crypto.subtle is unavailable (e.g. the jsdom test environment). */
export async function hashFile(file: File): Promise<string> {
  const bytes = await fileBytes(file)
  const subtle = globalThis.crypto?.subtle
  if (subtle) return hex(new Uint8Array(await subtle.digest('SHA-256', bytes as BufferSource)))
  return sha256Hex(bytes)
}

async function fileBytes(file: File): Promise<Uint8Array> {
  if (typeof file.arrayBuffer === 'function') return new Uint8Array(await file.arrayBuffer())
  if (typeof FileReader === 'function') {
    return await new Promise<Uint8Array>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer))
      reader.onerror = () => reject(reader.error)
      reader.readAsArrayBuffer(file)
    })
  }
  if (typeof file.text === 'function') return new TextEncoder().encode(await file.text())
  throw new Error('无法读取文件内容以计算校验值')
}

function hex(bytes: Uint8Array): string {
  return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('')
}

// Compact, standard SHA-256 over bytes (fallback only). Correct, not performance-tuned.
function sha256Hex(msg: Uint8Array): string {
  const K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]
  const h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]
  const l = msg.length
  const withOne = new Uint8Array((((l + 8) >> 6) + 1) * 64)
  withOne.set(msg); withOne[l] = 0x80
  const bits = l * 8
  const dv = new DataView(withOne.buffer)
  dv.setUint32(withOne.length - 4, bits >>> 0); dv.setUint32(withOne.length - 8, Math.floor(bits / 0x100000000))
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n))
  const w = new Uint32Array(64)
  for (let i = 0; i < withOne.length; i += 64) {
    for (let t = 0; t < 16; t++) w[t] = dv.getUint32(i + t * 4)
    for (let t = 16; t < 64; t++) {
      const s0 = rotr(w[t - 15], 7) ^ rotr(w[t - 15], 18) ^ (w[t - 15] >>> 3)
      const s1 = rotr(w[t - 2], 17) ^ rotr(w[t - 2], 19) ^ (w[t - 2] >>> 10)
      w[t] = (w[t - 16] + s0 + w[t - 7] + s1) >>> 0
    }
    let [a, b, c, d, e, f, g, hh] = h
    for (let t = 0; t < 64; t++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)
      const ch = (e & f) ^ (~e & g)
      const t1 = (hh + S1 + ch + K[t] + w[t]) >>> 0
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)
      const maj = (a & b) ^ (a & c) ^ (b & c)
      const t2 = (S0 + maj) >>> 0
      hh = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0
    }
    h[0] = (h[0] + a) >>> 0; h[1] = (h[1] + b) >>> 0; h[2] = (h[2] + c) >>> 0; h[3] = (h[3] + d) >>> 0
    h[4] = (h[4] + e) >>> 0; h[5] = (h[5] + f) >>> 0; h[6] = (h[6] + g) >>> 0; h[7] = (h[7] + hh) >>> 0
  }
  return h.map(x => (x >>> 0).toString(16).padStart(8, '0')).join('')
}
