import { expect, test } from '@playwright/test'
import { startMockServer, restartMockServer } from './mock-server'
test('auxiliary requests cannot consume the agent interim script', async () => {
  restartMockServer()
  const server=await startMockServer()
  const messages=[{role:'user',content:'E2E_INTERIM_TRIGGER'}]
  try {
    for(let i=0;i<2;i++) await fetch(server.url+'/v1/chat/completions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:'mock-model',messages})})
    const response=await fetch(server.url+'/v1/chat/completions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:'mock-model',messages,tools:[{type:'function',function:{name:'todo'}}]})})
    const body=await response.json()
    expect(body.choices[0].message.content).toBe('Let me start by planning the approach.')
  } finally {await server.close()}
})
