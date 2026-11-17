import { expect, test } from 'claude-code/testing'
import type { AgentInfo } from 'claude-code'

function agent(id: string, over: Partial<AgentInfo> = {}): AgentInfo {
  return {
    id, description: `task ${id}`, type: 'Explore', status: 'running', kind: 'local_agent',
    taskStatus: 'running', label: `task ${id}`, startTime: 1787000000000, cwd: '/repo',
    model: 'claude-opus-5', contextWindowSize: 200000, tokenCount: 48000,
    ...over,
  } as AgentInfo
}

const AGENTS = [agent('a'), agent('b', { status: 'completed' })]

function exited(stdout: string) {
  return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
}

function row(id: string) {
  return {
    surface: 'terminal', component: 'AgentRow', requestId: id,
    props: { ...AGENTS.find((a) => a.id === id)!, rowIds: AGENTS.map((a) => a.id), columns: 100, isHidden: false },
  } as const
}

test('one script run draws every row of a snapshot', async ($, on) => {
  const runs: { argv: readonly string[]; stdin?: string }[] = []
  const bodies: Record<string, string | undefined> = {}
  on('agent.list', () => ({ value: AGENTS }))
  on('process.hookEnv', () => ({ value: { CLAUDE_PLUGIN_ROOT: '/plugin' } }))
  on('process.run', (_, e) => {
    runs.push({ argv: e.argv, stdin: e.init?.stdin })
    const out = JSON.parse(e.init?.stdin ?? '{}').tasks
      .map((t: AgentInfo) => JSON.stringify({ id: t.id, content: `row ${t.id}` })).join('\n')
    return exited(out + '\n')
  })
  on('ui.render', { component: 'AgentRow' }, (_, e, next) => {
    bodies[e.requestId] = e.props.body
    return next(e)
  })

  await Promise.all([$.ui.render(row('a')), $.ui.render(row('b'))])

  expect(bodies).toEqual({ a: 'row a', b: 'row b' })
  expect(runs.length).toBe(1)
  expect(runs[0]?.argv.slice(1)).toEqual(['-X', 'utf8', '/plugin/subagent_statusline.py'])
  expect(JSON.parse(runs[0]?.stdin ?? '{}').columns).toBe(100)
})

test('the row is named by the agent name, else its type, never the description', async ($, on) => {
  let labels: string[] = []
  on('agent.list', () => ({ value: [agent('a', { name: 'red-team' }), agent('b')] }))
  on('process.hookEnv', () => ({ value: { CLAUDE_PLUGIN_ROOT: '/plugin' } }))
  on('process.run', (_, e) => {
    labels = JSON.parse(e.init?.stdin ?? '{}').tasks.map((t: AgentInfo) => t.label)
    return exited('')
  })
  on('ui.render', { component: 'AgentRow' }, (_, e, next) => next(e))

  await $.ui.render(row('a'))

  expect(labels).toEqual(['red-team', 'Explore'])
})

test('without a working Python the engine keeps its own row', async ($, on) => {
  const tried: string[] = []
  let body: string | undefined = 'unset'
  on('agent.list', () => ({ value: AGENTS }))
  on('process.hookEnv', () => ({ value: { CLAUDE_PLUGIN_ROOT: '/plugin' } }))
  on('process.run', (_, e) => {
    tried.push(e.argv[0] ?? '')
    throw new Error('not found')
  })
  on('ui.render', { component: 'AgentRow' }, (_, e, next) => {
    body = e.props.body
    return next(e)
  })

  await $.ui.render(row('a'))

  expect(tried).toEqual(['python', 'python3', 'py'])
  expect(body).toBeUndefined()
})
