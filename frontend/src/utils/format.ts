export function fmt(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function priorityType(p: string): 'danger' | 'warning' | 'primary' | 'info' {
  return ({ P1: 'danger', P2: 'warning', P3: 'primary' } as const)[p as 'P1' | 'P2' | 'P3'] ?? 'info'
}

export function pct(v: number | null | undefined): string {
  return v == null ? '—' : `${(v * 100).toFixed(1)}%`
}

export function seconds(ms: number | null | undefined): string {
  return ms == null ? '—' : `${(ms / 1000).toFixed(1)}s`
}

export function toolArgs(args: Record<string, unknown> | null | undefined): string {
  if (!args) return ''
  return Object.entries(args).map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`).join('，')
}
