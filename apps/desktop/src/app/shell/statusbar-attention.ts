/** Quiet normal state; never suppress status that needs attention. */
export function showGatewayHealth(botsShowing: boolean, inferenceReady: boolean, restarting: boolean): boolean {
  return !botsShowing && (!inferenceReady || restarting)
}

/** Version details remain in About; updates and their progress stay visible. */
export function showUpdateStatus(hasUpdate: boolean, applying: boolean): boolean {
  return hasUpdate || applying
}
