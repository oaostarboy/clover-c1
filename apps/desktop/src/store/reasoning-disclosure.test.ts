import { beforeEach, expect, it, vi } from 'vitest'
const pref=vi.hoisted(()=>({value:undefined as boolean|undefined,persist:vi.fn()}))
vi.mock('@/lib/storage',()=>({storedBoolean:(_key:string,fallback:boolean)=>pref.value??fallback,persistBoolean:pref.persist}))
beforeEach(()=>{vi.resetModules();pref.value=undefined;pref.persist.mockClear()})
it('starts collapsed when no explicit preference exists',async()=>{const m=await import('./reasoning-disclosure');expect(m.$reasoningCollapsedByDefault.get()).toBe(true)})
it('preserves explicit expanded preference',async()=>{pref.value=false;const m=await import('./reasoning-disclosure');expect(m.$reasoningCollapsedByDefault.get()).toBe(false)})
it('allows changing the default without losing the explicit choice',async()=>{const m=await import('./reasoning-disclosure');m.setReasoningCollapsedByDefault(false);expect(m.$reasoningCollapsedByDefault.get()).toBe(false);expect(pref.persist).toHaveBeenLastCalledWith('clover.desktop.reasoning.collapsedByDefault',false)})
