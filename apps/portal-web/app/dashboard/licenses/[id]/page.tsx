'use client'

import { useParams } from 'next/navigation'
import { useEffect, useRef, useState } from 'react'
import { api } from '@/lib/api'
import type { Fill, Instance, Signal, TraceTimeline } from '@/lib/types'

const PLATFORMS = ['mt5', 'mt4', 'dxtrade']

export default function LicensePage() {
  const { id } = useParams<{ id: string }>()
  const [instances, setInstances] = useState<Instance[]>([])
  const [signals, setSignals] = useState<Signal[]>([])
  const [config, setConfig] = useState('')
  const [fills, setFills] = useState<Record<string, Fill[]>>({})
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [newKey, setNewKey] = useState('')
  const [newPlatform, setNewPlatform] = useState('mt5')
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const [testingSignal, setTestingSignal] = useState(false)
  const [testResult, setTestResult] = useState('')
  const [replayingId, setReplayingId] = useState<string | null>(null)
  const [activeTrace, setActiveTrace] = useState<TraceTimeline | null>(null)
  const [traceLoading, setTraceLoading] = useState(false)
  const fetchedFills = useRef<Set<string>>(new Set())

  useEffect(() => {
    if (!id) return
    void Promise.all([
      api.getInstances(id).then(setInstances),
      api.getSignals(id).then(setSignals),
      api.getConfig(id).then((r) => setConfig(r.config)),
    ])
      .catch(() => setLoadError('Failed to load license data. Please refresh.'))
      .finally(() => setLoading(false))
  }, [id])

  useEffect(() => {
    for (const inst of instances) {
      if (fetchedFills.current.has(inst.id)) continue
      fetchedFills.current.add(inst.id)
      api
        .getFills(id, inst.id)
        .then((data) => setFills((prev) => ({ ...prev, [inst.id]: data })))
        .catch(() => {})
    }
  }, [instances, id])

  async function handleCreateInstance(e: React.FormEvent) {
    e.preventDefault()
    if (!newKey.trim()) return
    setCreating(true)
    setError('')
    try {
      const inst = await api.createInstance(id, newKey.trim(), newPlatform)
      setInstances((prev) => [...prev, inst])
      setNewKey('')
    } catch {
      setError('Failed to create instance.')
    } finally {
      setCreating(false)
    }
  }

  function copyConfig() {
    navigator.clipboard.writeText(config).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  async function handleTestSignal() {
    setTestingSignal(true)
    setTestResult('')
    try {
      const r = await api.testSignal(id)
      setTestResult(`Accepted — trace: ${r.trace_id}`)
    } catch {
      setTestResult('Test failed — check ingress is reachable and you have an active instance.')
    } finally {
      setTestingSignal(false)
    }
  }

  async function handleReplay(signalId: string) {
    setReplayingId(signalId)
    try {
      const r = await api.replaySignal(signalId)
      setTestResult(`Replayed — new trace: ${r.trace_id}`)
    } catch {
      setTestResult('Replay failed — signal may predate replay support.')
    } finally {
      setReplayingId(null)
    }
  }

  async function handleViewTrace(traceId: string) {
    if (activeTrace?.trace_id === traceId) {
      setActiveTrace(null)
      return
    }
    setTraceLoading(true)
    try {
      const t = await api.getTrace(traceId)
      setActiveTrace(t)
    } catch {
      setActiveTrace(null)
    } finally {
      setTraceLoading(false)
    }
  }

  if (loading) {
    return <p className="text-sm text-gray-500">Loading…</p>
  }

  if (loadError) {
    return <p className="text-sm text-red-600">{loadError}</p>
  }

  return (
    <div className="space-y-8">
      {/* Config export */}
      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Ingress config</h2>
          <button
            onClick={handleTestSignal}
            disabled={testingSignal}
            className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-sm font-medium text-blue-700 hover:bg-blue-100 disabled:opacity-50"
          >
            {testingSignal ? 'Sending…' : 'Send test signal'}
          </button>
        </div>
        <div className="relative rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
          <pre className="overflow-x-auto whitespace-pre-wrap break-all font-mono text-xs text-gray-700">
            {config || 'No instances yet — add one below to generate config.'}
          </pre>
          {config && (
            <button
              onClick={copyConfig}
              className="absolute right-3 top-3 rounded-md border border-gray-200 bg-white px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50"
            >
              {copied ? 'Copied!' : 'Copy'}
            </button>
          )}
        </div>
        {testResult && (
          <p className={`mt-2 text-sm ${testResult.startsWith('Accepted') || testResult.startsWith('Replayed') ? 'text-green-700' : 'text-red-600'}`}>
            {testResult}
          </p>
        )}
        <p className="mt-2 text-xs text-gray-400">
          Set this as <code className="font-mono">EXECRELAY_LICENSES</code> on the ingress service.
        </p>
      </section>

      {/* Instances */}
      <section>
        <h2 className="mb-3 text-lg font-semibold">Instances</h2>

        {instances.length > 0 ? (
          <div className="mb-4 space-y-2">
            {instances.map((inst) => (
              <div
                key={inst.id}
                className="rounded-xl border border-gray-200 bg-white px-5 py-3 shadow-sm"
              >
                <div className="flex items-center justify-between">
                  <div>
                    <span className="font-mono text-sm font-medium">{inst.instance_key}</span>
                    <span className="ml-2 rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600">
                      {inst.platform}
                    </span>
                  </div>
                  <span
                    className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                      inst.active ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-600'
                    }`}
                  >
                    {inst.active ? 'active' : 'inactive'}
                  </span>
                </div>
                {fills[inst.id] && fills[inst.id].length > 0 && (
                  <div className="mt-3">
                    <p className="mb-1 text-xs font-medium text-gray-500">Recent fills</p>
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-left text-gray-400">
                          <th className="pb-1 pr-4 font-normal">Trace</th>
                          <th className="pb-1 pr-4 font-normal">Status</th>
                          <th className="pb-1 pr-4 font-normal">Order ID</th>
                          <th className="pb-1 font-normal">Time</th>
                        </tr>
                      </thead>
                      <tbody>
                        {fills[inst.id].map((f) => (
                          <tr key={f.id} className="text-gray-700">
                            <td className="pr-4 font-mono">{f.trace_id.slice(0, 8)}…</td>
                            <td className="pr-4">
                              <span
                                className={`font-medium ${
                                  f.status === 'filled'
                                    ? 'text-green-700'
                                    : f.status === 'timeout'
                                      ? 'text-amber-600'
                                      : 'text-red-600'
                                }`}
                              >
                                {f.status}
                              </span>
                            </td>
                            <td className="pr-4 font-mono">{f.broker_order_id || '—'}</td>
                            <td className="text-gray-400">
                              {new Date(f.filled_at).toLocaleTimeString()}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            ))}
          </div>
        ) : (
          <p className="mb-4 text-sm text-gray-500">No instances yet.</p>
        )}

        <form onSubmit={handleCreateInstance} className="flex gap-2">
          <input
            type="text"
            required
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
            placeholder="instance-key (e.g. mt5-a)"
            className="flex-1 rounded-lg border border-gray-300 px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          <select
            value={newPlatform}
            onChange={(e) => setNewPlatform(e.target.value)}
            className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            {PLATFORMS.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
          <button
            type="submit"
            disabled={creating}
            className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {creating ? 'Adding…' : 'Add'}
          </button>
        </form>
        {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      </section>

      {/* Signal history */}
      <section>
        <h2 className="mb-3 text-lg font-semibold">Recent signals</h2>
        {signals.length === 0 ? (
          <p className="text-sm text-gray-500">No signals received yet.</p>
        ) : (
          <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
            <table className="w-full text-sm">
              <thead className="border-b border-gray-100 bg-gray-50">
                <tr className="text-left text-xs font-medium text-gray-500">
                  <th className="px-4 py-3">Trace ID</th>
                  <th className="px-4 py-3">Command</th>
                  <th className="px-4 py-3">Symbol</th>
                  <th className="px-4 py-3">Region</th>
                  <th className="px-4 py-3">Received</th>
                  <th className="px-4 py-3"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {signals.map((s) => (
                  <>
                    <tr key={s.id} className="text-gray-700">
                      <td className="px-4 py-2.5 font-mono text-xs">{s.trace_id.slice(0, 12)}…</td>
                      <td className="px-4 py-2.5 font-medium">{s.command}</td>
                      <td className="px-4 py-2.5">{s.symbol}</td>
                      <td className="px-4 py-2.5 text-xs text-gray-400">{s.ingress_region}</td>
                      <td className="px-4 py-2.5 text-xs text-gray-400">
                        {new Date(s.received_at).toLocaleString()}
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex gap-2">
                          <button
                            onClick={() => handleViewTrace(s.trace_id)}
                            className="text-xs text-blue-600 hover:underline"
                          >
                            {traceLoading && activeTrace?.trace_id !== s.trace_id ? '…' :
                              activeTrace?.trace_id === s.trace_id ? 'hide' : 'trace'}
                          </button>
                          <button
                            onClick={() => handleReplay(s.id)}
                            disabled={replayingId === s.id}
                            className="text-xs text-gray-500 hover:text-gray-800 disabled:opacity-40"
                          >
                            {replayingId === s.id ? '…' : 'replay'}
                          </button>
                        </div>
                      </td>
                    </tr>
                    {activeTrace?.trace_id === s.trace_id && (
                      <tr key={`${s.id}-trace`}>
                        <td colSpan={6} className="bg-gray-50 px-4 py-3">
                          <TraceView trace={activeTrace} />
                        </td>
                      </tr>
                    )}
                  </>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}

function TraceView({ trace }: { trace: TraceTimeline }) {
  const steps = [
    { label: 'Ingress received', time: trace.signal?.received_at, detail: `${trace.signal?.command} ${trace.signal?.symbol} via ${trace.signal?.ingress_region}` },
    ...trace.fills.map((f) => ({
      label: `Fill: ${f.status}`,
      time: f.created_at,
      detail: f.broker_order_id ? `order ${f.broker_order_id}` : f.error_message ?? '',
    })),
    ...trace.events.map((e) => ({
      label: e.event_type,
      time: e.created_at,
      detail: e.severity,
    })),
  ]
    .filter((s) => s.time)
    .sort((a, b) => new Date(a.time!).getTime() - new Date(b.time!).getTime())

  return (
    <div className="space-y-1">
      <p className="mb-2 text-xs font-medium text-gray-500">
        Trace: <span className="font-mono">{trace.trace_id}</span>
      </p>
      {steps.map((step, i) => (
        <div key={i} className="flex items-start gap-3 text-xs">
          <span className="mt-0.5 h-2 w-2 shrink-0 rounded-full bg-blue-400" />
          <div>
            <span className="font-medium text-gray-700">{step.label}</span>
            {step.detail && <span className="ml-2 text-gray-400">{step.detail}</span>}
            <span className="ml-2 text-gray-300">
              {new Date(step.time!).toLocaleTimeString()}
            </span>
          </div>
        </div>
      ))}
      {steps.length === 0 && (
        <p className="text-xs text-gray-400">No timeline events found for this trace.</p>
      )}
    </div>
  )
}
