export interface WidgetGridItem {
  id: string
  span?: number
}

export interface WidgetGridCell {
  col: number
  id: string
  span: number
  width: number
}

export interface WidgetGridLayout {
  columnCount: number
  columns: number[]
  rows: WidgetGridCell[][]
}

export interface WidgetGridLayoutOptions {
  gap?: number
  items: WidgetGridItem[]
  maxColumns?: number
  minColumnWidth?: number
  width: number
}

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value))

const toInt = (value: number, fallback: number) => {
  if (!Number.isFinite(value)) {
    return fallback
  }

  return Math.trunc(value)
}

const columnCountForWidth = (width: number, minColumnWidth: number, gap: number, maxColumns: number) => {
  const safeWidth = Math.max(1, toInt(width, 1))
  const safeMinWidth = Math.max(1, toInt(minColumnWidth, 1))
  const safeGap = Math.max(0, toInt(gap, 0))
  const safeMaxColumns = Math.max(1, toInt(maxColumns, 1))
  const count = Math.floor((safeWidth + safeGap) / (safeMinWidth + safeGap))

  return clamp(count || 1, 1, safeMaxColumns)
}

const buildColumnWidths = (width: number, columnCount: number, gap: number) => {
  const safeWidth = Math.max(1, toInt(width, 1))
  const safeGap = Math.max(0, toInt(gap, 0))
  const slots = Math.max(1, toInt(columnCount, 1))
  const usable = Math.max(1, safeWidth - safeGap * Math.max(0, slots - 1))
  const base = Math.floor(usable / slots)
  const remainder = usable % slots

  return Array.from({ length: slots }, (_, idx) => base + (idx < remainder ? 1 : 0))
}

const spanWidth = (columns: number[], colStart: number, span: number, gap: number) => {
  const end = Math.min(columns.length, colStart + span)
  const width = columns.slice(colStart, end).reduce((acc, value) => acc + value, 0)
  const safeGap = Math.max(0, toInt(gap, 0))

  return width + safeGap * Math.max(0, end - colStart - 1)
}

export function layoutWidgetGrid({
  gap = 1,
  items,
  maxColumns = 3,
  minColumnWidth = 28,
  width
}: WidgetGridLayoutOptions): WidgetGridLayout {
  const safeGap = Math.max(0, toInt(gap, 1))
  const columnCount = columnCountForWidth(width, minColumnWidth, safeGap, maxColumns)
  const columns = buildColumnWidths(width, columnCount, safeGap)
  const rows: WidgetGridCell[][] = []
  let row: WidgetGridCell[] = []
  let usedCols = 0

  for (const item of items) {
    const wantedSpan = clamp(toInt(item.span ?? 1, 1), 1, columnCount)

    if (row.length > 0 && usedCols + wantedSpan > columnCount) {
      rows.push(row)
      row = []
      usedCols = 0
    }

    row.push({
      col: usedCols,
      id: item.id,
      span: wantedSpan,
      width: spanWidth(columns, usedCols, wantedSpan, safeGap)
    })

    usedCols += wantedSpan
  }

  if (row.length > 0) {
    rows.push(row)
  }

  return { columnCount, columns, rows }
}
