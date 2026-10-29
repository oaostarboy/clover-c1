import { parse as upstreamParse } from './upstream-lang-core/dist/index.mjs'
import type { OpenUIComparisonPayload } from './openui-contract'

export const OPENUI_COMPARISON_PARAM_MAP = new Map([
  [
    'Comparison',
    {
      params: [
        { name: 'title', required: true },
        { name: 'columns', required: true },
        { name: 'rows', required: true },
      ],
    },
  ],
  [
    'Row',
    {
      params: [
        { name: 'label', required: true },
        { name: 'values', required: true },
      ],
    },
  ],
])

export interface OpenUIParseResult {
  success: boolean
  payload?: OpenUIComparisonPayload
  rawRoot?: unknown
  errors?: string[]
}

/**
 * Reusable OpenUI Lang parser adapter.
 * Reuses @openuidev/lang-core parser (MIT licensed) under an isolated,
 * strictly bounded parameter map with closed validation.
 */
export function parseOpenUILangComparison(source: string): OpenUIParseResult {
  if (typeof source !== 'string') {
    return { success: false, errors: ['Source must be a string'] }
  }
  const bytes = new TextEncoder().encode(source)
  if (bytes.length > 32768) {
    return { success: false, errors: ['Source exceeds 32 KiB limit'] }
  }

  try {
    const res = upstreamParse(source, OPENUI_COMPARISON_PARAM_MAP)
    if (!res || !res.root) {
      return { success: false, errors: ['Failed to parse root element or unrecognized root component'] }
    }
    const root = res.root as any
    if (root.type !== 'element' || root.typeName !== 'Comparison') {
      return { success: false, errors: [`Expected root component Comparison, got ${root.typeName || 'unknown'}`] }
    }

    const { title, columns, rows } = root.props || {}
    if (typeof title !== 'string' || title.length < 1 || title.length > 160) {
      return { success: false, errors: ['Invalid or missing title in Comparison'] }
    }
    if (!Array.isArray(columns) || columns.length < 2 || columns.length > 4 || !columns.every((c: any) => typeof c === 'string' && c.length >= 1 && c.length <= 80)) {
      return { success: false, errors: ['Invalid columns in Comparison (expected 2-4 strings, 1-80 chars each)'] }
    }
    if (!Array.isArray(rows) || rows.length < 1 || rows.length > 20) {
      return { success: false, errors: ['Invalid rows in Comparison (expected 1-20 rows)'] }
    }

    const colWidth = columns.length
    const normalizedRows: { label: string; values: string[] }[] = []

    for (const r of rows) {
      let label: string
      let values: string[]

      if (r && typeof r === 'object' && r.type === 'element' && r.typeName === 'Row') {
        label = r.props?.label
        values = r.props?.values
      } else if (r && typeof r === 'object' && 'label' in r && 'values' in r) {
        label = r.label
        values = r.values
      } else {
        return { success: false, errors: ['Invalid row element'] }
      }

      if (typeof label !== 'string' || label.length < 1 || label.length > 120) {
        return { success: false, errors: ['Invalid row label'] }
      }
      if (!Array.isArray(values) || values.length !== colWidth || !values.every((v: any) => typeof v === 'string' && v.length <= 500)) {
        return { success: false, errors: [`Row values must match column count (${colWidth}) and be strings <= 500 chars`] }
      }
      normalizedRows.push({ label, values })
    }

    const payload: OpenUIComparisonPayload = {
      version: 1,
      title,
      columns,
      rows: normalizedRows,
      openui_lang: source,
    }

    return {
      success: true,
      payload,
      rawRoot: res.root,
    }
  } catch (err: any) {
    return { success: false, errors: [err?.message || String(err)] }
  }
}
