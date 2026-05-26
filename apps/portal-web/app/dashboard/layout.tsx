'use client'

import { useRouter } from 'next/navigation'
import { useEffect } from 'react'
import Nav from '@/components/Nav'
import { getToken } from '@/lib/auth'

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter()

  useEffect(() => {
    if (!getToken()) {
      router.replace('/login')
    }
  }, [router])

  return (
    <div className="min-h-screen">
      <Nav />
      <main className="mx-auto max-w-5xl px-4 py-8">{children}</main>
    </div>
  )
}
