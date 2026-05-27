'use client'

import Link from 'next/link'
import { useParams, usePathname } from 'next/navigation'

export default function LicenseLayout({ children }: { children: React.ReactNode }) {
  const { id } = useParams<{ id: string }>()
  const pathname = usePathname()

  const tabs = [
    { name: 'Overview', href: `/dashboard/licenses/${id}` },
    { name: 'Correlation', href: `/dashboard/licenses/${id}/correlation` },
    { name: 'Portfolio', href: `/dashboard/licenses/${id}/portfolio` },
    { name: 'Risk', href: `/dashboard/licenses/${id}/risk` },
    { name: 'Backtest', href: `/dashboard/licenses/${id}/backtest` },
  ]

  return (
    <div>
      <div className="border-b">
        <div className="flex gap-4 px-4">
          {tabs.map((tab) => (
            <Link
              key={tab.href}
              href={tab.href}
              className={`py-3 px-2 border-b-2 transition ${
                pathname === tab.href
                  ? 'border-blue-500 text-blue-600'
                  : 'border-transparent text-gray-600 hover:text-gray-900'
              }`}
            >
              {tab.name}
            </Link>
          ))}
        </div>
      </div>
      <div className="p-6">{children}</div>
    </div>
  )
}
