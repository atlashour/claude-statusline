// Subagent rows: an adapter that hands the agent list to subagent_statusline.py.
//
// The Python script stays the one renderer, so the rows share the main line's gauge,
// colours and drop order. It needs every row at once to align columns, while the engine
// asks for AgentRow one row at a time, so one run per snapshot is shared by every row.
// Any failure answers next(e): the engine's own row, never a hidden one.
import type { EngineInterface, Register } from 'claude-code'

const SCRIPT = 'subagent_statusline.py'
const PYTHONS = ['python', 'python3', 'py']
const TIMEOUT_MS = 5_000

type Rows = Map<string, string>

// Module state; a reload starts it over, which only costs one interpreter probe.
const cache: {
  python?: string                      // first interpreter that ran the script
  env?: Record<string, string>
  payload: string                      // the snapshot `run` renders
  run?: Promise<Rows>
} = { payload: '' }

function parseRows(stdout: string): Rows {
  const rows: Rows = new Map()
  for (const line of stdout.split('\n')) {
    if (!line.trim()) continue
    try {
      const { id, content } = JSON.parse(line)
      if (typeof id === 'string' && typeof content === 'string' && content) rows.set(id, content)
    } catch { /* one bad line drops its row only */ }
  }
  return rows
}

async function render($: EngineInterface, payload: string): Promise<Rows> {
  if (!cache.env) cache.env = { ...(await $.process.hookEnv()), PYTHONUTF8: '1' }
  const env = cache.env
  const script = `${env.CLAUDE_PLUGIN_ROOT}/${SCRIPT}`
  for (const exe of cache.python ? [cache.python] : PYTHONS) {
    let out
    try {
      out = await $.process.run([exe, '-X', 'utf8', script], { env, stdin: payload, timeoutMs: TIMEOUT_MS })
    } catch {
      continue                                        // not on PATH: try the next name
    }
    if (out.exitCode !== 0) continue
    cache.python = exe
    return parseRows(out.stdout)
  }
  throw new Error(`no Python interpreter could run ${SCRIPT}`)
}

export const register: Register = (on) => {
  on('ui.render', { component: 'AgentRow' }, async ($, e, next) => {
    let body: string | undefined
    try {
      const ids = new Set(e.props.rowIds)
      // The script's `label` is the row's short name. AgentInfo's label is the progress
      // summary, else the description, which the row already shows on the right.
      const tasks = (await $.agent.list())
        .filter((a) => ids.has(a.id))
        .map((a) => ({ ...a, label: a.name || a.type }))
      const payload = JSON.stringify({ columns: e.props.columns, tasks })
      if (payload !== cache.payload || !cache.run) {
        const run = render($, payload)
        cache.payload = payload
        cache.run = run
        run.catch(() => { if (cache.run === run) cache.run = undefined })   // retry on the next ask
      }
      body = (await cache.run).get(e.props.id)
    } catch { /* the engine's own row */ }
    return next(body ? { ...e, props: { ...e.props, body } } : e)
  })
}
