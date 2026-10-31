import {expect,it} from 'vitest'

import {isCardTool} from '@/lib/tool-render-class'

import {splitRunItems} from './fallback'
it('does not collapse the registered comparison deliverable into a tool summary',()=>{
 expect(isCardTool('comparison_card')).toBe(true)
 expect(splitRunItems(['read_file','comparison_card','terminal'])).toEqual([{kind:'run',start:0,end:0},{kind:'card',index:1},{kind:'run',start:2,end:2}])
})
it('prices only the supported comparison bridge as a deliverable',()=>{
 expect(isCardTool('tool_call',{name:'comparison_card',arguments:{}})).toBe(true)
 expect(isCardTool('tool_call',{name:'terminal',arguments:{}})).toBe(false)
})
