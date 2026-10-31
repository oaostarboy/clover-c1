import {createContext} from 'react'

import type {CloverGateway} from '@/clover'
export const ThreadBackendContext=createContext<{gateway:CloverGateway|null;sessionId:string|null}>({gateway:null,sessionId:null})
