'use client'

import { useParams } from 'next/navigation'
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'

interface Position {
  symbol: string
  size: number
}

interface Account {
  account_id: string
  notional_usd: number
  limit_usd: number
  utilization_pct: number
  positions: Position[]
}

interface PortfolioExposure {
  license_id: string
  accounts: Account[]
}

export default function PortfolioPage() {
  const { id } = useParams<{ id: string }>()
  const [portfolio, setPortfolio] = useState<PortfolioExposure | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!id) return
    api
      .getPortfolioExposure(id)
      .then((data) => setPortfolio(data))
      .catch(() => setError('Failed to load portfolio data'))
      .finally(() => setLoading(false))
  }, [id])

  if (loading)
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-gray-500">Loading portfolio...</div>
      </div>
    )

  if (error)
    return <div className="bg-red-50 border border-red-200 text-red-700 p-3 rounded">{error}</div>

  if (!portfolio || portfolio.accounts.length === 0)
    return <div className="text-gray-500">No accounts with positions</div>

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold mb-4">Portfolio Exposure</h1>
        <p className="text-gray-600">Monitor account exposure and limits across all trading accounts</p>
      </div>

      <div className="space-y-4">
        {portfolio.accounts.map((account) => {
          const utilPct = account.utilization_pct
          const status =
            utilPct > 90 ? 'critical' : utilPct > 75 ? 'warning' : utilPct > 50 ? 'caution' : 'healthy'
          const statusColor = {
            critical: 'bg-red-50 border-red-200',
            warning: 'bg-yellow-50 border-yellow-200',
            caution: 'bg-blue-50 border-blue-200',
            healthy: 'bg-green-50 border-green-200',
          }[status]

          return (
            <div key={account.account_id} className={`border rounded-lg p-4 ${statusColor}`}>
              <div className="flex justify-between items-start mb-3">
                <h2 className="font-semibold text-lg">{account.account_id}</h2>
                <div className="text-right">
                  <div className="text-sm text-gray-600">Exposure</div>
                  <div className="text-2xl font-bold">${account.notional_usd.toLocaleString()}</div>
                  <div className="text-xs text-gray-600">of ${account.limit_usd.toLocaleString()} limit</div>
                </div>
              </div>

              <div className="mb-3">
                <div className="bg-gray-200 rounded-full h-2 overflow-hidden">
                  <div
                    className={`h-full transition ${
                      status === 'critical'
                        ? 'bg-red-600'
                        : status === 'warning'
                          ? 'bg-yellow-500'
                          : status === 'caution'
                            ? 'bg-blue-500'
                            : 'bg-green-500'
                    }`}
                    style={{ width: `${Math.min(utilPct, 100)}%` }}
                  />
                </div>
                <div className="text-xs text-gray-600 mt-1">
                  {utilPct.toFixed(1)}% utilization
                  {status !== 'healthy' && (
                    <span className="ml-2 font-semibold">
                      {status === 'critical'
                        ? '🔴 CRITICAL'
                        : status === 'warning'
                          ? '🟡 WARNING'
                          : '🔵 CAUTION'}
                    </span>
                  )}
                </div>
              </div>

              {account.positions.length > 0 && (
                <div className="mt-3">
                  <div className="text-xs font-semibold text-gray-600 mb-2">Positions ({account.positions.length})</div>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                    {account.positions.map((pos) => (
                      <div key={pos.symbol} className="bg-white bg-opacity-50 rounded p-2 text-center text-xs">
                        <div className="font-bold">{pos.symbol}</div>
                        <div className="text-gray-600">{pos.size.toFixed(2)}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
