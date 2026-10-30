import { beforeEach, describe, expect, it } from 'vitest'

import {
  $sidebarCompactNavigation,
  $sidebarGrouping,
  $sidebarOpen,
  $sidebarOrdering,
  $sidebarRowMeta,
  $sidebarViewCustomized,
  resetSidebarView,
  setSidebarCompactNavigation,
  setSidebarGrouping,
  setSidebarOpen,
  setSidebarOrdering,
  toggleSidebarOpen,
  toggleSidebarRowMeta,
  toggleSidebarStatusFilter
} from './layout'
import { $showAllProfiles } from './profile'

beforeEach(() => {
  $showAllProfiles.set(false)
  resetSidebarView()
})

describe('the sidebar as it ships', () => {
  it('groups by date, sorts by recency, and pins the timestamp and preview', () => {
    expect($sidebarGrouping.get()).toBe('date')
    expect($sidebarOrdering.get()).toBe('updated')
    expect($sidebarRowMeta.get()).toEqual(['preview', 'updated'])
  })

  it('offers no reset until something actually moves off the defaults', () => {
    expect($sidebarViewCustomized.get()).toBe(false)

    toggleSidebarRowMeta('tokens')

    expect($sidebarViewCustomized.get()).toBe(true)
  })

  it('is what reset puts back — every knob, not just the filters', () => {
    setSidebarGrouping('project')
    setSidebarOrdering('cost')
    toggleSidebarRowMeta('updated')
    toggleSidebarRowMeta('cost')
    toggleSidebarStatusFilter('working')

    resetSidebarView()

    expect($sidebarGrouping.get()).toBe('date')
    expect($sidebarOrdering.get()).toBe('updated')
    expect($sidebarRowMeta.get()).toEqual(['preview', 'updated'])
    expect($sidebarViewCustomized.get()).toBe(false)
  })

  it('ships by date in the all-profiles scope too, and resets back to it', () => {
    $showAllProfiles.set(true)
    setSidebarGrouping('profile')

    resetSidebarView()

    expect($sidebarGrouping.get()).toBe('date')
    expect($sidebarViewCustomized.get()).toBe(false)
  })

  it('resets the scope the user is not looking at, so flipping the rail cannot restore it', () => {
    setSidebarGrouping('status')
    $showAllProfiles.set(true)
    setSidebarGrouping('profile')

    resetSidebarView()
    $showAllProfiles.set(false)

    expect($sidebarGrouping.get()).toBe('date')
  })

  it('turns all-profiles on when the user groups by profile, since that is the ask', () => {
    setSidebarGrouping('profile')

    expect($showAllProfiles.get()).toBe(true)
    expect($sidebarGrouping.get()).toBe('profile')
  })
})

describe('sidebar compact navigation & open toggle', () => {
  it('expands compact navigation when sidebar is open and toggled', () => {
    setSidebarOpen(true)
    setSidebarCompactNavigation(true)
    expect($sidebarCompactNavigation.get()).toBe(true)
    expect($sidebarOpen.get()).toBe(true)

    toggleSidebarOpen()
    expect($sidebarCompactNavigation.get()).toBe(false)
    expect($sidebarOpen.get()).toBe(true)
  })

  it('expands compact navigation when setSidebarOpen(true) is called', () => {
    setSidebarCompactNavigation(true)
    expect($sidebarCompactNavigation.get()).toBe(true)

    setSidebarOpen(true)
    expect($sidebarCompactNavigation.get()).toBe(false)
  })
})
