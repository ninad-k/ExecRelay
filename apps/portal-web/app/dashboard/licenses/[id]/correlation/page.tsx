'use client'

import { useParams } from 'next/navigation'
import { useEffect, useState } from 'react'
import { Signal } from '@/lib/types'
import { api } from '@/lib/api'

interface CorrelationResult {
  signals: Signal[]
  correlation_matrix: Record<string, number>
  symbol_groups: Record<string, number>
  conflicts: string[]
}

export default function CorrelationPage() {
  const { id } = useParams<{ id: string }>()
  const [signals, setSignals] = useState<Signal[]>([])
  const [selectedSignals, setSelectedSignals] = useState<Set<string>>(new Set())
  const [result, setResult] = useState<CorrelationResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!id) return
    api
      .getSignals(id)
      .then((sigs) => setSignals(sigs as Signal[]))
      .catch(() => setError('Failed to load signals'))
  }, [id])

  async function analyzeCorrelation() {
    if (selectedSignals.size < 2) {
      setError('Select at least 2 signals')
      return
    }

    setLoading(true)
    setError('')
    try {
      const data = await api.correlateSignals(id, Array.from(selectedSignals))
      setResult(data)
    } catch (err) {
      setError('Failed to analyze correlation')
    } finally {
      setLoading(false)
    }
  }

  function toggleSignal(sigId: string) {
    const next = new Set(selectedSignals)
    if (next.has(sigId)) next.delete(sigId)
    else next.add(sigId)
    setSelectedSignals(next)
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold mb-4">Signal Correlation Analysis</h1>
        <p className="text-gray-600 mb-4">Analyze relationships between signals and detect conflicts</p>
      </div>

      <div className="bg-white rounded-lg border p-4">
        <h2 className="font-semibold mb-3">Select Signals</h2>
        <div className="space-y-2 max-h-96 overflow-y-auto">
          {signals.map((sig) => (
            <label key={sig.id} className="flex items-center gap-3 p-2 hover:bg-gray-50 rounded">
              <input
                type="checkbox"
                checked={selectedSignals.has(sig.id)}
                onChange={() => toggleSignal(sig.id)}
                className="rounded"
              />
              <span className="text-sm">
                {sig.symbol} {sig.command} (ID: {sig.id})
              </span>
            </label>
          ))}
        </div>

        <button
          onClick={analyzeCorrelation}
          disabled={loading || selectedSignals.size < 2}
          className="mt-4 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-400"
        >
          {loading ? 'Analyzing...' : 'Analyze Correlation'}
        </button>
      </div>

      {error && <div className="bg-red-50 border border-red-200 text-red-700 p-3 rounded">{error}</div>}

      {result && (
        <div className="space-y-4">
          <div className="bg-white rounded-lg border p-4">
            <h2 className="font-semibold mb-3">Symbol Groups</h2>
            <div className="grid grid-cols-3 gap-4">
              {Object.entries(result.symbol_groups).map(([symbol, count]) => (
                <div key={symbol} className="border rounded p-3 text-center">
                  <div className="text-lg font-bold">{symbol}</div>
                  <div className="text-sm text-gray-600">{count} signal(s)</div>
                </div>
              ))}
            </div>
          </div>

          {Object.keys(result.correlation_matrix).length > 0 && (
            <div className="bg-white rounded-lg border p-4">
              <h2 className="font-semibold mb-3">Correlations</h2>
              <div className="space-y-2">
                {Object.entries(result.correlation_matrix).map(([pair, corr]) => (
                  <div key={pair} className="flex justify-between items-center p-2 bg-gray-50 rounded">
                    <span className="font-mono text-sm">{pair}</span>
                    <span className={`text-sm font-bold ${corr > 0.7 ? 'text-red-600' : 'text-green-600'}`}>
                      {corr.toFixed(3)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {result.conflicts.length > 0 && (
            <div className="bg-yellow-50 border border-yellow-200 rounded p-4">
              <h2 className="font-semibold text-yellow-900 mb-2">⚠️ Conflicts Detected</h2>
              <ul className="space-y-1">
                {result.conflicts.map((conflict, i) => (
                  <li key={i} className="text-sm text-yellow-800">
                    • {conflict}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
