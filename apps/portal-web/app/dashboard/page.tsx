'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { api, ApiError } from '@/lib/api'
import type { License } from '@/lib/types'

const PLATFORMS = ['mt5', 'mt4', 'dxtrade']

export default function DashboardPage() {
  const [licenses, setLicenses] = useState<License[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [platform, setPlatform] = useState('mt5')
  const [error, setError] = useState('')

  async function load() {
    try {
      setLicenses(await api.getLicenses())
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        window.location.href = '/login'
      }
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setCreating(true)
    try {
      const license = await api.createLicense(platform)
      setLicenses((prev) => [license, ...prev])
    } catch {
      setError('Failed to create license.')
    } finally {
      setCreating(false)
    }
  }

  async function toggleActive(license: License) {
    try {
      const updated = await api.patchLicense(license.id, !license.active)
      setLicenses((prev) => prev.map((l) => (l.id === license.id ? updated : l)))
    } catch {
      setError('Failed to update license.')
    }
  }

  if (loading) {
    return <p className="text-sm text-gray-500">Loading…</p>
  }

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold">Licenses</h1>
        <form onSubmit={handleCreate} className="flex gap-2">
          <select
            value={platform}
            onChange={(e) => setPlatform(e.target.value)}
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
            {creating ? 'Creating…' : '+ New license'}
          </button>
        </form>
      </div>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      {licenses.length === 0 ? (
        <p className="text-sm text-gray-500">No licenses yet. Create one above.</p>
      ) : (
        <div className="space-y-3">
          {licenses.map((license) => (
            <div
              key={license.id}
              className="flex items-center justify-between rounded-xl border border-gray-200 bg-white px-5 py-4 shadow-sm"
            >
              <div>
                <Link
                  href={`/dashboard/licenses/${license.id}`}
                  className="font-mono text-sm font-medium text-blue-600 hover:underline"
                >
                  {license.license_key}
                </Link>
                <p className="mt-0.5 text-xs text-gray-500">
                  {license.platform} &middot; created {new Date(license.created_at).toLocaleDateString()}
                </p>
              </div>
              <div className="flex items-center gap-3">
                <span
                  className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${
                    license.active
                      ? 'bg-green-100 text-green-800'
                      : 'bg-gray-100 text-gray-600'
                  }`}
                >
                  {license.active ? 'active' : 'inactive'}
                </span>
                <button
                  onClick={() => toggleActive(license)}
                  className="text-xs text-gray-400 hover:text-gray-700"
                >
                  {license.active ? 'Disable' : 'Enable'}
                </button>
                <Link
                  href={`/dashboard/licenses/${license.id}`}
                  className="text-xs text-gray-400 hover:text-gray-700"
                >
                  Details →
                </Link>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
