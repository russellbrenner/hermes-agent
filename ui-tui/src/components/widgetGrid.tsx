import { Box } from '@hermes/ink'
import { Fragment, memo, useMemo, type ReactNode } from 'react'

import { layoutWidgetGrid, type WidgetGridCell, type WidgetGridItem } from '../lib/widgetGrid.js'

export interface WidgetGridWidget extends WidgetGridItem {
  render: (width: number) => ReactNode
}

interface WidgetGridProps {
  cols: number
  gap?: number
  maxColumns?: number
  minColumnWidth?: number
  rowGap?: number
  widgets: WidgetGridWidget[]
}

export const WidgetGrid = memo(function WidgetGrid({
  cols,
  gap = 2,
  maxColumns = 2,
  minColumnWidth = 46,
  rowGap = 1,
  widgets
}: WidgetGridProps) {
  const layout = useMemo(
    () =>
      layoutWidgetGrid({
        gap,
        items: widgets.map(({ id, span }) => ({ id, span })),
        maxColumns,
        minColumnWidth,
        width: cols
      }),
    [cols, gap, maxColumns, minColumnWidth, widgets]
  )

  const widgetById = useMemo(() => new Map(widgets.map(widget => [widget.id, widget])), [widgets])

  if (!layout.rows.length) {
    return null
  }

  return (
    <Box flexDirection="column" width={Math.max(1, cols)}>
      {layout.rows.map((row, rowIdx) => (
        <Box flexDirection="column" key={`row-${rowIdx}`}>
          <Box flexDirection="row">
            {row.map((cell, cellIdx) => (
              <WidgetCell
                cell={cell}
                gap={gap}
                isLast={cellIdx === row.length - 1}
                key={cell.id}
                widget={widgetById.get(cell.id)}
              />
            ))}
          </Box>

          {rowGap > 0 && rowIdx < layout.rows.length - 1 ? <Box height={rowGap} /> : null}
        </Box>
      ))}
    </Box>
  )
})

const WidgetCell = memo(function WidgetCell({
  cell,
  gap,
  isLast,
  widget
}: {
  cell: WidgetGridCell
  gap: number
  isLast: boolean
  widget?: WidgetGridWidget
}) {
  const node = widget?.render(cell.width) ?? null

  return (
    <Fragment>
      <Box flexShrink={0} width={cell.width}>
        {node}
      </Box>

      {!isLast && gap > 0 ? <Box flexShrink={0} width={gap} /> : null}
    </Fragment>
  )
})
