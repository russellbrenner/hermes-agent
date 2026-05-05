import { describe, expect, it } from 'vitest'

import { layoutWidgetGrid } from '../lib/widgetGrid.js'

describe('layoutWidgetGrid', () => {
  it('falls back to a single column on narrow widths', () => {
    const layout = layoutWidgetGrid({
      items: [{ id: 'a' }, { id: 'b' }],
      maxColumns: 3,
      minColumnWidth: 40,
      width: 35
    })

    expect(layout.columnCount).toBe(1)
    expect(layout.columns).toEqual([35])
    expect(layout.rows).toEqual([[{ col: 0, id: 'a', span: 1, width: 35 }], [{ col: 0, id: 'b', span: 1, width: 35 }]])
  })

  it('packs spans left-to-right and wraps to the next row', () => {
    const layout = layoutWidgetGrid({
      gap: 2,
      items: [
        { id: 'a', span: 1 },
        { id: 'b', span: 2 },
        { id: 'c', span: 1 }
      ],
      maxColumns: 3,
      minColumnWidth: 30,
      width: 100
    })

    expect(layout.columnCount).toBe(3)
    expect(layout.columns).toEqual([32, 32, 32])
    expect(layout.rows).toEqual([
      [
        { col: 0, id: 'a', span: 1, width: 32 },
        { col: 1, id: 'b', span: 2, width: 66 }
      ],
      [{ col: 0, id: 'c', span: 1, width: 32 }]
    ])
  })

  it('clamps spans to available columns', () => {
    const layout = layoutWidgetGrid({
      gap: 1,
      items: [{ id: 'huge', span: 9 }],
      maxColumns: 2,
      minColumnWidth: 20,
      width: 50
    })

    expect(layout.columnCount).toBe(2)
    expect(layout.rows[0]?.[0]).toEqual({
      col: 0,
      id: 'huge',
      span: 2,
      width: 50
    })
  })
})
