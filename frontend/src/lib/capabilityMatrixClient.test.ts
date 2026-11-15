// @vitest-environment jsdom
/**
 * 能力矩阵的客户端契约 (方案 §6/§7)。
 *
 * 钉住两件容易悄悄坏掉的事:
 *  1. 🔴 **缓存 key 带 market** —— CN 与 HK 的矩阵响应形状不同 (HK 是声明式:
 *     field/candidates 恒空)。key 不带市场段就会互相污染, 而且污染是静默的:
 *     先访问的页面把响应写进缓存, 后访问的页面拿到错市场的 usable 判定。
 *  2. 🔴 **非法 market 不做客户端兜底** —— 原样发给后端吃 400, 与后端
 *     「非法 market 不静默回退 CN」同款教条。客户端悄悄改成 CN 会让
 *     「用户以为在看港股能力」这个 bug 无从暴露。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

function stubFetch() {
  const fetchMock = vi.fn(async (..._args: unknown[]) =>
    new Response(JSON.stringify({ market: 'CN', routed: true, capabilities: [] }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const urlOf = (fetchMock: ReturnType<typeof stubFetch>) =>
  String(fetchMock.mock.calls[0]?.[0] ?? '')

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('QK.capabilityMatrix', () => {
  it('不传 market → 裸前缀 (既是 CN 取数 key, 也是 invalidate 的前缀 key)', () => {
    expect(QK.capabilityMatrix()).toEqual(['capability-matrix'])
  })

  it('传 market → key 追加市场段', () => {
    expect(QK.capabilityMatrix('CN')).toEqual(['capability-matrix', 'CN'])
    expect(QK.capabilityMatrix('HK')).toEqual(['capability-matrix', 'HK'])
  })

  it('CN 与 HK 的 key 不碰撞 (缓存不串市场)', () => {
    expect(QK.capabilityMatrix('CN')).not.toEqual(QK.capabilityMatrix('HK'))
  })

  it('裸前缀能覆盖所有市场 → 设置页一次 invalidate 不会漏掉港股矩阵', () => {
    const prefix = QK.capabilityMatrix()
    for (const market of ['CN', 'HK'] as const) {
      const key = QK.capabilityMatrix(market)
      expect(key.slice(0, prefix.length)).toEqual(prefix)
    }
  })
})

describe('api.capabilityMatrix', () => {
  it('不传 market → 不带 query, 走后端缺省 CN (既有调用点零改动)', async () => {
    const fetchMock = stubFetch()
    await api.capabilityMatrix()
    expect(urlOf(fetchMock)).toContain('/api/settings/capability-matrix')
    expect(urlOf(fetchMock)).not.toContain('market=')
  })

  it('传 HK → ?market=HK', async () => {
    const fetchMock = stubFetch()
    await api.capabilityMatrix('HK')
    expect(urlOf(fetchMock)).toContain('/api/settings/capability-matrix?market=HK')
  })

  it('🔴 非法 market 原样透传给后端 (不在客户端静默改成 CN)', async () => {
    const fetchMock = stubFetch()
    // 绕过类型检查模拟「URL 参数/旧缓存里混进脏值」
    await api.capabilityMatrix('XX' as never)
    expect(urlOf(fetchMock)).toContain('market=XX')
  })
})
