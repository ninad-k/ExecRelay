'use client'

import { useParams } from 'next/navigation'
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'

interface BacktestResult {
  job_id: string
  total_signals: number
  total_fills: number
  fill_rate_pct: number
  gross_pnl: number
  net_pnl: number
  sharpe_ratio: number
  max_drawdown_pct: number
  win_count: number
  loss_count: number
  win_pct: number
  status: string
  error?: string
}

export default function BacktestPage() {
  const { id } = useParams<{ id: string }>()
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [error, setError] = useState('')

  const handleBacktest = async () => {
    if (!startDate || !endDate) {
      setError('Please select both start and end dates')
      return
    }

    setLoading(true)
    setError('')

    try {
      const response = await fetch('/api/backtest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          license_id: id,
          date_start: startDate,
          date_end: endDate
        })
      })

      const data = await response.json()
      setResult(data)
    } catch (err: any) {
      setError(`Backtest failed: ${err.message}`)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold mb-4">Strategy Backtester</h1>
        <p className="text-gray-600">Simulate historical strategy performance</p>
      </div>

      {error && <div className="bg-red-50 border border-red-200 text-red-700 p-3 rounded">{error}</div>}

      <div className="bg-white rounded-lg border p-6">
        <h2 className="font-semibold mb-4">Backtest Parameters</h2>
        <div className="grid grid-cols-2 gap-4 mb-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Start Date</label>
            <input
              type="date"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">End Date</label>
            <input
              type="date"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded"
            />
          </div>
        </div>
        <button
          onClick={handleBacktest}
          disabled={loading}
          className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-400"
        >
          {loading ? 'Running backtest...' : 'Run Backtest'}
        </button>
      </div>

      {result && (
        <div className="space-y-4">
          {result.status === 'failed' ? (
            <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded">
              <div className="font-semibold">Backtest Failed</div>
              <div>{result.error}</div>
            </div>
          ) : (
            <>
              <div className="grid grid-cols-4 gap-4">
                <div className="bg-white rounded-lg border p-4">
                  <div className="text-sm text-gray-600">Total Signals</div>
                  <div className="text-3xl font-bold mt-2">{result.total_signals}</div>
                </div>
                <div className="bg-white rounded-lg border p-4">
                  <div className="text-sm text-gray-600">Fill Rate</div>
                  <div className="text-3xl font-bold mt-2">{result.fill_rate_pct.toFixed(1)}%</div>
                </div>
                <div className="bg-white rounded-lg border p-4">
                  <div className="text-sm text-gray-600">Net P&L</div>
                  <div className={`text-3xl font-bold mt-2 ${result.net_pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                    ${result.net_pnl.toFixed(2)}
                  </div>
                </div>
                <div className="bg-white rounded-lg border p-4">
                  <div className="text-sm text-gray-600">Win Rate</div>
                  <div className="text-3xl font-bold mt-2">{result.win_pct.toFixed(1)}%</div>
                </div>
              </div>

              <div className="grid grid-cols-3 gap-4">
                <div className="bg-white rounded-lg border p-4">
                  <div className="text-sm text-gray-600">Sharpe Ratio</div>
                  <div className="text-2xl font-bold mt-2">{result.sharpe_ratio.toFixed(2)}</div>
                </div>
                <div className="bg-white rounded-lg border p-4">
                  <div className="text-sm text-gray-600">Max Drawdown</div>
                  <div className="text-2xl font-bold mt-2 text-red-600">{result.max_drawdown_pct.toFixed(2)}%</div>
                </div>
                <div className="bg-white rounded-lg border p-4">
                  <div className="text-sm text-gray-600">Win/Loss Count</div>
                  <div className="text-2xl font-bold mt-2">{result.win_count}/{result.loss_count}</div>
                </div>
              </div>

              <div className="bg-white rounded-lg border p-4">
                <h3 className="font-semibold mb-3">Trade Summary</h3>
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div>
                    <div className="text-gray-600">Total Fills</div>
                    <div className="text-lg font-semibold">{result.total_fills}</div>
                  </div>
                  <div>
                    <div className="text-gray-600">Gross P&L</div>
                    <div className={`text-lg font-semibold ${result.gross_pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                      ${result.gross_pnl.toFixed(2)}
                    </div>
                  </div>
                </div>
              </div>
            </>
          )}
        </div>
      )}

      {!result && !loading && (
        <div className="bg-white rounded-lg border p-8 text-center">
          <div className="text-gray-500">Select date range and run backtest to see results</div>
        </div>
      )}
    </div>
  )
}
