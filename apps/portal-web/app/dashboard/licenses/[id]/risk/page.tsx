'use client'

import { useParams } from 'next/navigation'
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'

interface RiskMetrics {
  license_id: string
  accounts: Array<{
    account_id: string
    notional_exposure: number
    exposure_limit: number
    exposure_ratio: number
    peak_equity: number
    current_equity: number
    drawdown_pct: number
    largest_position: {
      symbol: string
      size: number
      value: number
    } | null
  }>
  total_exposure: number
  total_limit: number
  breaches: Array<{
    account_id: string
    breach_type: string
    current_value: number
    limit_value: number
    created_at: string
  }>
}

export default function RiskDashboard() {
  const { id } = useParams<{ id: string }>()
  const [metrics, setMetrics] = useState<RiskMetrics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!id) return
    api
      .getRiskMetrics(id)
      .then((data) => setMetrics(data))
      .catch(() => setError('Failed to load risk metrics'))
      .finally(() => setLoading(false))
  }, [id])

  if (loading)
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-gray-500">Loading risk metrics...</div>
      </div>
    )

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold mb-4">Risk Management Dashboard</h1>
        <p className="text-gray-600">Monitor account exposure, drawdown, and risk limit breaches</p>
      </div>

      {error && <div className="bg-red-50 border border-red-200 text-red-700 p-3 rounded">{error}</div>}

      {metrics && (
        <>
          <div className="grid grid-cols-3 gap-4">
            <div className="bg-white rounded-lg border p-4">
              <div className="text-sm text-gray-600">Total Exposure</div>
              <div className="text-3xl font-bold mt-2">${metrics.total_exposure.toLocaleString(undefined, { maximumFractionDigits: 0 })}</div>
              <div className="text-xs text-gray-500 mt-1">
                {metrics.total_limit > 0 ? `${(metrics.total_exposure / metrics.total_limit * 100).toFixed(1)}% of limit` : 'No limits set'}
              </div>
            </div>

            <div className="bg-white rounded-lg border p-4">
              <div className="text-sm text-gray-600">Avg Drawdown</div>
              <div className={`text-3xl font-bold mt-2 ${metrics.accounts.length > 0 && metrics.accounts.some(a => a.drawdown_pct > 10) ? 'text-red-600' : 'text-gray-900'}`}>
                {metrics.accounts.length > 0 ? (metrics.accounts.reduce((sum, a) => sum + a.drawdown_pct, 0) / metrics.accounts.length).toFixed(1) : '0.0'}%
              </div>
              <div className="text-xs text-gray-500 mt-1">Across {metrics.accounts.length} account{metrics.accounts.length !== 1 ? 's' : ''}</div>
            </div>

            <div className="bg-white rounded-lg border p-4">
              <div className="text-sm text-gray-600">Active Breaches</div>
              <div className="text-3xl font-bold mt-2">{metrics.breaches.length}</div>
              <div className="text-xs text-gray-500 mt-1">Limit violations</div>
            </div>
          </div>

          <div className="bg-white rounded-lg border p-4">
            <h2 className="font-semibold mb-4">Account Risk Heatmap</h2>
            <div className="space-y-3">
              {metrics.accounts.length === 0 ? (
                <div className="text-sm text-gray-600 text-center py-8">No accounts with tracked positions</div>
              ) : (
                metrics.accounts.map((account) => {
                  const expRatio = account.exposure_ratio
                  const drawStatus =
                    account.drawdown_pct > 20 ? 'critical' :
                    account.drawdown_pct > 10 ? 'warning' :
                    'healthy'
                  const expStatus =
                    expRatio > 90 ? 'critical' :
                    expRatio > 75 ? 'warning' :
                    'healthy'

                  return (
                    <div key={account.account_id} className="border rounded p-3">
                      <div className="flex justify-between items-start mb-2">
                        <div className="font-semibold">{account.account_id}</div>
                        <div className="text-right text-sm">
                          <span className={`inline-block px-2 py-1 rounded text-xs font-semibold mr-2 ${
                            expStatus === 'critical' ? 'bg-red-100 text-red-700' :
                            expStatus === 'warning' ? 'bg-yellow-100 text-yellow-700' :
                            'bg-green-100 text-green-700'
                          }`}>
                            Exposure: {expRatio.toFixed(1)}%
                          </span>
                          <span className={`inline-block px-2 py-1 rounded text-xs font-semibold ${
                            drawStatus === 'critical' ? 'bg-red-100 text-red-700' :
                            drawStatus === 'warning' ? 'bg-yellow-100 text-yellow-700' :
                            'bg-green-100 text-green-700'
                          }`}>
                            Drawdown: {account.drawdown_pct.toFixed(1)}%
                          </span>
                        </div>
                      </div>
                      <div className="grid grid-cols-3 gap-2 text-xs text-gray-600">
                        <div>
                          <div className="font-semibold">${account.notional_exposure.toLocaleString(undefined, { maximumFractionDigits: 0 })}</div>
                          <div>Exposure</div>
                        </div>
                        <div>
                          <div className="font-semibold">${account.current_equity.toLocaleString(undefined, { maximumFractionDigits: 0 })}</div>
                          <div>Equity</div>
                        </div>
                        <div>
                          {account.largest_position && (
                            <>
                              <div className="font-semibold">{account.largest_position.symbol}</div>
                              <div>Largest</div>
                            </>
                          )}
                        </div>
                      </div>
                    </div>
                  )
                })
              )}
            </div>
          </div>

          {metrics.breaches.length > 0 && (
            <div className="bg-red-50 rounded-lg border border-red-200 p-4">
              <h2 className="font-semibold text-red-900 mb-3">Recent Breach Alerts</h2>
              <div className="space-y-2">
                {metrics.breaches.map((breach, idx) => (
                  <div key={idx} className="text-sm text-red-800 p-2 bg-white rounded border border-red-100">
                    <div className="font-semibold">{breach.account_id}</div>
                    <div>{breach.breach_type}: ${breach.current_value.toFixed(2)} exceeds limit of ${breach.limit_value.toFixed(2)}</div>
                    <div className="text-xs text-gray-500 mt-1">{new Date(breach.created_at).toLocaleString()}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      <div className="bg-white rounded-lg border p-4">
        <h2 className="font-semibold mb-4">Account Risk Heatmap</h2>
        <div className="space-y-3">
          <div className="text-sm text-gray-600 text-center py-8">
            Risk metrics will appear here once positions are tracked
          </div>
        </div>
      </div>

      <div className="bg-white rounded-lg border p-4">
        <h2 className="font-semibold mb-4">Recent Breach Alerts</h2>
        <div className="text-sm text-gray-600 text-center py-8">
          No limit breaches detected
        </div>
      </div>

      <div className="bg-white rounded-lg border p-4">
        <h2 className="font-semibold mb-4">Exposure Limits</h2>
        <div className="space-y-3">
          <div className="text-sm text-gray-600">
            Configure exposure limits per account to enable risk monitoring and alerts
          </div>
          <button className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700">
            Configure Limits
          </button>
        </div>
      </div>
    </div>
  )
}
