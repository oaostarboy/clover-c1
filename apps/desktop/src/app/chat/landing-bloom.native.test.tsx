import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { LandingBloom } from './landing-bloom'
afterEach(()=>{cleanup();vi.restoreAllMocks();vi.unstubAllGlobals();Reflect.deleteProperty(window,'cloverDesktop');Reflect.deleteProperty(HTMLCanvasElement.prototype,'transferControlToOffscreen')})
it('pauses native-hidden botanical workers even when document.hidden stays false',()=>{
  const postMessage=vi.fn(),off=vi.fn()
  let notify: ((state:{isVisible:boolean})=>void)|undefined
  Object.defineProperty(window,'cloverDesktop',{configurable:true,value:{onWindowStateChanged:(fn:typeof notify)=>{notify=fn;return off}}})
  vi.stubGlobal('Worker',class {postMessage=postMessage;terminate=vi.fn()})
  vi.stubGlobal('matchMedia',()=>({matches:false,addEventListener:vi.fn(),removeEventListener:vi.fn()}))
  Object.defineProperty(HTMLCanvasElement.prototype,'transferControlToOffscreen',{configurable:true,value:()=>({})})
  const {unmount}=render(<LandingBloom />)
  notify?.({isVisible:false})
  expect(postMessage).toHaveBeenLastCalledWith({type:'active',active:false,reduced:false})
  notify?.({isVisible:true})
  expect(postMessage).toHaveBeenLastCalledWith({type:'active',active:true,reduced:false})
  unmount();expect(off).toHaveBeenCalledOnce()
})
