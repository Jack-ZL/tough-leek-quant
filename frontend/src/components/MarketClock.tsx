import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Clock } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'

// 北京时间 (UTC+8, 无夏令时) — 与服务端 market_time.CN_TZ 口径一致
const BEIJING_TZ = 'Asia/Shanghai'

const dateTimeFmt = new Intl.DateTimeFormat('zh-CN', {
  timeZone: BEIJING_TZ,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
})

/** 北京当前时间 → `YYYY-MM-DD HH:mm:ss` */
export function formatBeijingDateTime(d: Date): string {
  const parts = dateTimeFmt.formatToParts(d)
  const get = (type: string) => parts.find(p => p.type === type)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}:${get('second')}`
}

/** `YYYY-MM-DD` → `M月D日` (下一交易日提示用) */
export function formatMonthDay(iso: string): string {
  const [m, d] = (iso || '').split('-').slice(1)
  const month = Number(m)
  const day = Number(d)
  if (!Number.isFinite(month) || !Number.isFinite(day)) return iso
  return `${month}月${day}日`
}

/**
 * 侧栏日期/时间 + 交易日状态。
 * 时间每秒本地刷新 (北京时区); 交易日状态由后端探针判定, 每分钟复探
 * (跨日/开盘缓冲窗变化), 后端内部按 TTL 缓存, 轮询成本可忽略。
 */
export function MarketClock() {
  const [now, setNow] = useState(() => new Date())

  useEffect(() => {
    let timer: number
    // 每次对齐到下一个整秒边界 (而非固定 1000ms 间隔), 避免计时器漂移
    // 导致显示比系统时间慢一秒; +10ms 保证落在边界之后、秒数已进位。
    const tick = () => {
      setNow(new Date())
      timer = window.setTimeout(tick, 1000 - (Date.now() % 1000) + 10)
    }
    timer = window.setTimeout(tick, 1000 - (Date.now() % 1000) + 10)
    return () => window.clearTimeout(timer)
  }, [])

  const { data } = useQuery({
    queryKey: QK.marketTradingStatus,
    queryFn: api.marketTradingStatus,
    staleTime: 60_000,
    refetchInterval: 60_000,
  })

  const verdict = data?.is_trading_day
  // A 股语义色 (bull/bear) 仅用于价格/K 线; 状态色用 accent/muted/warning。
  const dotCls =
    verdict === true ? 'bg-accent'
    : verdict === false ? 'bg-muted'
    : 'bg-warning'
  const textCls =
    verdict === true ? 'text-accent'
    : verdict === false ? 'text-muted'
    : 'text-warning'
  const pillCls =
    verdict === true ? 'bg-accent/10'
    : verdict === false ? 'bg-muted/10'
    : 'bg-warning/10'

  let statusText = '交易日状态未知'
  if (verdict === true) statusText = '交易日'
  else if (verdict === false) {
    statusText = data?.next_trading_day
      ? `休市 · 下一交易日 ${formatMonthDay(data.next_trading_day)}`
      : '休市'
  }

  return (
    <div className="rounded-md border border-border/60 bg-elevated/40 px-2.5 py-2">
      <div className="flex items-center justify-center gap-1">
        <Clock className="h-3.5 w-3.5 shrink-0 text-muted" />
        <span className="font-mono text-[13px] font-medium leading-none tabular-nums whitespace-nowrap text-foreground">
          {formatBeijingDateTime(now)}
        </span>
      </div>
      <div className="mt-1.5 flex items-center justify-center">
        <span className={cn('inline-flex items-center gap-1.5 rounded-full px-2 py-0.5', pillCls)}>
          <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', dotCls)} />
          <span className={cn('text-[10px] font-medium leading-none', textCls)}>
            {statusText}
          </span>
        </span>
      </div>
    </div>
  )
}
