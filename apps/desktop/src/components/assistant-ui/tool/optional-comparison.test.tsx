import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'

import type {CloverGateway} from '@/clover'

import {OptionalComparison} from './optional-comparison'
import {ThreadBackendContext} from './thread-backend-context'
afterEach(cleanup)
it('hydrates only canonical semantic data and renders read-only text with local view switching',async()=>{
 const record={contract:'clover.structured-tool/1',identity:{stored_session_id:'stored',assistant_row_id:3,tool_call_id:'call-1',turn_row_id:2},component:'clover.comparison',version:1,tool_name:'comparison_card',revision:2,status:'success',payload:{version:1,title:'Authored comparison',columns:['A','B'],rows:[{label:'Markup',values:['<img src=x onerror=alert(1)>','Plain']}]},terminal_row_id:4}
 const request=vi.fn(async(_method:string,_params:unknown)=>({structured_tools:[record]})),onEvent=vi.fn(()=>()=>{})
 render(<ThreadBackendContext.Provider value={{gateway:{request,onEvent} as unknown as CloverGateway,sessionId:'runtime'}}><OptionalComparison toolCallId="call-1" /></ThreadBackendContext.Provider>)
 await screen.findByText('Authored comparison')
 expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeTruthy()
 expect(document.querySelector('img')).toBeNull()
 expect(screen.getByRole('button',{name:'List view'}).className).toContain('min-h-11')
 fireEvent.click(screen.getByRole('button',{name:'List view'}))
 await waitFor(()=>expect(screen.getByRole('button',{name:'List view'}).getAttribute('aria-pressed')).toBe('true'))
 expect(request.mock.calls.map(call=>call[0])).toEqual(['session.history'])
})
it('keeps normal fallback empty when the backend lacks the optional capability',async()=>{
 const request=vi.fn(async()=>({messages:[]})),onEvent=vi.fn(()=>()=>{})
 const {container}=render(<ThreadBackendContext.Provider value={{gateway:{request,onEvent} as unknown as CloverGateway,sessionId:'runtime'}}><OptionalComparison toolCallId="call-1" /></ThreadBackendContext.Provider>)
 await waitFor(()=>expect(request).toHaveBeenCalledOnce())
 expect(container.textContent).toBe('')
})
it('keeps the durable card and exposes offline state until supported history reconnects',async()=>{
 const record={contract:'clover.structured-tool/1',identity:{stored_session_id:'stored',assistant_row_id:3,tool_call_id:'call-1',turn_row_id:2},component:'clover.comparison',version:1,tool_name:'comparison_card',revision:1,status:'running',payload:{version:1,title:'Offline comparison',columns:['A','B'],rows:[{label:'Data',values:['Cached','Text']}]}}
 let changed:((state:string)=>void)|undefined
 const request=vi.fn(async()=>({structured_tools:[record]})),onEvent=vi.fn(()=>()=>{}),onState=vi.fn((cb:(state:string)=>void)=>{changed=cb;return()=>{}})
 render(<ThreadBackendContext.Provider value={{gateway:{request,onEvent,onState} as unknown as CloverGateway,sessionId:'runtime'}}><OptionalComparison toolCallId="call-1" /></ThreadBackendContext.Provider>)
 await screen.findByText('Offline comparison')
 act(()=>changed?.('closed'))
 await screen.findByText(/Offline\. Showing the last saved state/)
 expect(screen.getByText('Cached')).toBeTruthy()
 act(()=>changed?.('open'))
 await waitFor(()=>expect(request).toHaveBeenCalledTimes(2))
 await waitFor(()=>expect(screen.queryByText(/Offline\. Showing the last saved state/)).toBeNull())
})
