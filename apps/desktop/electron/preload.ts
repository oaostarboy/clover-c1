import { contextBridge, ipcRenderer, webFrame, webUtils } from 'electron'

// Which translucency the OS can back. Asked synchronously because the renderer
// needs it before its first paint, and answered by main because deciding it
// needs `os.release()` — a sandboxed preload may only require electron, events,
// timers and url, so importing node:os here throws before contextBridge runs
// and takes the ENTIRE bridge down with it (window.cloverDesktop undefined =>
// "Desktop IPC bridge is unavailable"). No reply means no glass, which degrades
// to an ordinary opaque window rather than a page thinned over nothing.
const translucencySupport = ipcRenderer.sendSync('clover:translucency:support')

contextBridge.exposeInMainWorld('cloverDesktop', {
  glassSupported: translucencySupport?.glass === true,
  translucencySupported: translucencySupport?.translucency === true,
  getConnection: profile => ipcRenderer.invoke('clover:connection', profile),
  // Registry-scoped backend resolution: { connectionId, profile } → descriptor.
  getConnectionFor: payload => ipcRenderer.invoke('clover:connection:for', payload),
  getProfileRoutes: profiles => ipcRenderer.invoke('clover:plugin-profile-routes', profiles),
  revalidateConnection: () => ipcRenderer.invoke('clover:connection:revalidate'),
  touchBackend: profile => ipcRenderer.invoke('clover:backend:touch', profile),
  getGatewayWsUrl: profile => ipcRenderer.invoke('clover:gateway:ws-url', profile),
  // Registry-scoped fresh WS URL: { connectionId, profile } → result shape of
  // getGatewayWsUrl, minted against that connection's backend.
  getGatewayWsUrlFor: payload => ipcRenderer.invoke('clover:gateway:ws-url-for', payload),
  // Union agent roster across every registered connection.
  getAgentRoster: () => ipcRenderer.invoke('clover:agents:roster'),
  openSessionWindow: (sessionId, opts) => ipcRenderer.invoke('clover:window:openSession', sessionId, opts),
  openSessionInTerminal: (sessionId, opts) => ipcRenderer.invoke('clover:window:openInTerminal', sessionId, opts),
  openWindow: () => ipcRenderer.invoke('clover:window:openInstance'),
  claimAmbientCue: key => ipcRenderer.invoke('clover:ambient:claim', key),
  wakeIndicator: {
    getState: () => ipcRenderer.invoke('clover:wake-indicator:get'),
    setState: state => ipcRenderer.send('clover:wake-indicator:set', state),
    onState: callback => {
      const listener = (_event, state) => callback(state)
      ipcRenderer.on('clover:wake-indicator:state', listener)

      return () => ipcRenderer.removeListener('clover:wake-indicator:state', listener)
    }
  },
  petOverlay: {
    // Main renderer → main process: window lifecycle + drag. `request` is
    // `{ bounds, screen }`; resolves with the screen bounds it actually used.
    open: request => ipcRenderer.invoke('clover:pet-overlay:open', request),
    close: () => ipcRenderer.invoke('clover:pet-overlay:close'),
    setBounds: bounds => ipcRenderer.send('clover:pet-overlay:set-bounds', bounds),
    setIgnoreMouse: ignore => ipcRenderer.send('clover:pet-overlay:ignore-mouse', ignore),
    // Flip the overlay focusable (and focus it) while the composer needs keys.
    setFocusable: focusable => ipcRenderer.send('clover:pet-overlay:set-focusable', focusable),
    // Main renderer → overlay (forwarded by main): push the latest pet state.
    pushState: payload => ipcRenderer.send('clover:pet-overlay:state', payload),
    // Overlay → main renderer (forwarded by main): pop back in / composer submit.
    control: payload => ipcRenderer.send('clover:pet-overlay:control', payload),
    // Overlay subscribes to state pushes.
    onState: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('clover:pet-overlay:state', listener)

      return () => ipcRenderer.removeListener('clover:pet-overlay:state', listener)
    },
    // Main renderer subscribes to overlay control messages.
    onControl: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('clover:pet-overlay:control', listener)

      return () => ipcRenderer.removeListener('clover:pet-overlay:control', listener)
    }
  },
  // HUD mode: the chrome-free floating chat. A full app renderer (own gateway)
  // sized as a floating bar, so it mounts the real composer. Main owns the
  // window; `onChanged` keeps every window's toggle truthful.
  hud: {
    open: request => ipcRenderer.invoke('clover:hud:open', request),
    close: () => ipcRenderer.invoke('clover:hud:close'),
    setIgnoreMouse: ignore => ipcRenderer.send('clover:hud:ignore-mouse', ignore),
    moveBy: delta => ipcRenderer.send('clover:hud:move-by', delta),
    setBounds: bounds => ipcRenderer.send('clover:hud:set-bounds', bounds),
    // Whether the band covers the window below the bar. Main pairs it with the
    // user's translucency setting to decide the native frost (macOS vibrancy /
    // Windows 11 DWM backdrop) — see hudFrostFor.
    setFrost: showing => ipcRenderer.invoke('clover:hud:frost', showing),
    // The HUD tells main which session it is on; main hands that back to the
    // app window when the HUD closes, so the app can re-home onto it.
    setSession: sessionId => ipcRenderer.send('clover:hud:session', sessionId),
    onGoto: callback => {
      const listener = (_event, sessionId) => callback(sessionId)
      ipcRenderer.on('clover:hud:goto', listener)

      return () => ipcRenderer.removeListener('clover:hud:goto', listener)
    },
    onChanged: callback => {
      const listener = (_event, state) => callback(state)
      ipcRenderer.on('clover:hud:changed', listener)

      return () => ipcRenderer.removeListener('clover:hud:changed', listener)
    },
    // Linux only, and silent elsewhere: where the cursor is, in page
    // coordinates, or null when it has left the window. Stands in for the
    // mousemove that `setIgnoreMouseEvents(true, { forward: true })` delivers on
    // macOS and Windows but not here.
    onCursor: callback => {
      const listener = (_event, point) => callback(point)
      ipcRenderer.on('clover:hud:cursor', listener)

      return () => ipcRenderer.removeListener('clover:hud:cursor', listener)
    }
  },
  // Quick Entry: the global-hotkey mini composer window. Main owns the OS
  // shortcut + the persisted preference; the quick window only captures text
  // and hands it back, and the primary renderer submits it through the normal
  // prompt path.
  quickEntry: {
    getSettings: () => ipcRenderer.invoke('clover:quick-entry:settings:get'),
    setSettings: patch => ipcRenderer.invoke('clover:quick-entry:settings:set', patch),
    submit: payload => ipcRenderer.send('clover:quick-entry:submit', payload),
    dismiss: () => ipcRenderer.send('clover:quick-entry:dismiss'),
    // Primary renderer → main → quick window: gateway connection state + the
    // recent-session options the target picker offers. Main caches the latest
    // payload so a freshly spawned quick window starts from truth.
    pushState: payload => ipcRenderer.send('clover:quick-entry:state', payload),
    // Quick window subscribes to those pushes.
    onState: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('clover:quick-entry:state', listener)

      return () => ipcRenderer.removeListener('clover:quick-entry:state', listener)
    },
    // Main → primary renderer: a submit captured by the quick window.
    onSubmit: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('clover:quick-entry:submit', listener)

      return () => ipcRenderer.removeListener('clover:quick-entry:submit', listener)
    },
    // Main → quick window: you were just summoned (reset draft + refocus).
    onShown: callback => {
      const listener = () => callback()
      ipcRenderer.on('clover:quick-entry:shown', listener)

      return () => ipcRenderer.removeListener('clover:quick-entry:shown', listener)
    }
  },
  getBootProgress: () => ipcRenderer.invoke('clover:boot-progress:get'),
  getConnectionConfig: profile => ipcRenderer.invoke('clover:connection-config:get', profile),
  saveConnectionConfig: payload => ipcRenderer.invoke('clover:connection-config:save', payload),
  applyConnectionConfig: payload => ipcRenderer.invoke('clover:connection-config:apply', payload),
  testConnectionConfig: payload => ipcRenderer.invoke('clover:connection-config:test', payload),
  // v2 multi-connection registry: named agent sources (local / remote / cloud / ssh).
  connections: {
    list: () => ipcRenderer.invoke('clover:connections:list'),
    save: payload => ipcRenderer.invoke('clover:connections:save', payload),
    remove: id => ipcRenderer.invoke('clover:connections:remove', id),
    setPrimary: id => ipcRenderer.invoke('clover:connections:set-primary', id),
    setLaunchMode: mode => ipcRenderer.invoke('clover:connections:set-launch-mode', mode),
    setLastUsed: id => ipcRenderer.invoke('clover:connections:set-last-used', id),
    test: id => ipcRenderer.invoke('clover:connections:test', id),
    // Fan out `clover update` to every eligible registered connection.
    // Optional excludeIds skips rows the caller updates through another path.
    updateAll: options => ipcRenderer.invoke('clover:connections:update-all', options),
    // Registry lifecycle push (main → renderer): a connection was removed or
    // materially edited, so secondaries scoped to it must be disposed (and,
    // for edits, re-dialed at the new target).
    onChanged: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('clover:connections:changed', listener)

      return () => ipcRenderer.removeListener('clover:connections:changed', listener)
    }
  },
  sshConfigHosts: () => ipcRenderer.invoke('clover:ssh-config:hosts'),
  sshResolveHost: host => ipcRenderer.invoke('clover:ssh-config:resolve', host),
  probeConnectionConfig: remoteUrl => ipcRenderer.invoke('clover:connection-config:probe', remoteUrl),
  oauthLoginConnectionConfig: remoteUrl => ipcRenderer.invoke('clover:connection-config:oauth-login', remoteUrl),
  oauthLogoutConnectionConfig: remoteUrl => ipcRenderer.invoke('clover:connection-config:oauth-logout', remoteUrl),
  // Clover Cloud: one portal login powers discovery + silent per-agent sign-in
  // (cloud-auto-discovery Phase 3).
  cloud: {
    status: () => ipcRenderer.invoke('clover:cloud:status'),
    login: () => ipcRenderer.invoke('clover:cloud:login'),
    logout: () => ipcRenderer.invoke('clover:cloud:logout'),
    discover: org => ipcRenderer.invoke('clover:cloud:discover', org),
    agentSignIn: dashboardUrl => ipcRenderer.invoke('clover:cloud:agent-sign-in', dashboardUrl)
  },
  profile: {
    get: () => ipcRenderer.invoke('clover:profile:get'),
    set: name => ipcRenderer.invoke('clover:profile:set', name)
  },
  api: request => ipcRenderer.invoke('clover:api', request),
  notify: payload => ipcRenderer.invoke('clover:notify', payload),
  requestMicrophoneAccess: () => ipcRenderer.invoke('clover:requestMicrophoneAccess'),
  readWindowBelow: () => ipcRenderer.invoke('clover:window:readBelow'),
  readFileDataUrl: filePath => ipcRenderer.invoke('clover:readFileDataUrl', filePath),
  readFileDataUrlForAttach: filePath => ipcRenderer.invoke('clover:readFileDataUrlForAttach', filePath),
  dataUrlReadMax: {
    get: () => ipcRenderer.invoke('clover:data-url-read-max:get'),
    set: maxMb => ipcRenderer.invoke('clover:data-url-read-max:set', maxMb)
  },
  readFileText: filePath => ipcRenderer.invoke('clover:readFileText', filePath),
  readPluginSource: (filePath: string) => ipcRenderer.invoke('clover:readPluginSource', filePath),
  selectPaths: options => ipcRenderer.invoke('clover:selectPaths', options),
  selectSavePath: options => ipcRenderer.invoke('clover:selectSavePath', options),
  writeClipboard: text => ipcRenderer.invoke('clover:writeClipboard', text),
  readClipboard: () => ipcRenderer.invoke('clover:readClipboard'),
  saveGatewayFile: payload => ipcRenderer.invoke('clover:saveGatewayFile', payload),
  saveImageFromUrl: url => ipcRenderer.invoke('clover:saveImageFromUrl', url),
  contextMenuEdit: command => ipcRenderer.invoke('clover:context-menu:edit', command),
  contextMenuCopyImage: () => ipcRenderer.invoke('clover:context-menu:copy-image'),
  contextMenuSpellcheck: action => ipcRenderer.invoke('clover:context-menu:spellcheck', action),
  contextMenuGuestAddWord: payload => ipcRenderer.invoke('clover:context-menu:guest-add-word', payload),
  onContextMenuSpellcheck: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('clover:context-menu-spellcheck', listener)

    return () => ipcRenderer.removeListener('clover:context-menu-spellcheck', listener)
  },
  saveImageBuffer: (data, ext) => ipcRenderer.invoke('clover:saveImageBuffer', { data, ext }),
  saveClipboardImage: () => ipcRenderer.invoke('clover:saveClipboardImage'),
  getPathForFile: file => {
    try {
      return webUtils.getPathForFile(file) || ''
    } catch {
      return ''
    }
  },
  normalizePreviewTarget: (target, baseDir) => ipcRenderer.invoke('clover:normalizePreviewTarget', target, baseDir),
  watchPreviewFile: url => ipcRenderer.invoke('clover:watchPreviewFile', url),
  watchDirectory: dir => ipcRenderer.invoke('clover:watchDirectory', dir),
  stopPreviewFileWatch: id => ipcRenderer.invoke('clover:stopPreviewFileWatch', id),
  setActiveWork: payload => ipcRenderer.send('clover:active-work', payload),
  setTitleBarTheme: payload => ipcRenderer.send('clover:titlebar-theme', payload),
  setNativeTheme: mode => ipcRenderer.send('clover:native-theme', mode),
  setTranslucency: payload => ipcRenderer.send('clover:translucency', payload),
  setKeepAwake: on => ipcRenderer.send('clover:keep-awake', on),
  setDisableF12: blocked => ipcRenderer.send('clover:devtools:disable-f12', blocked),
  setPreviewShortcutActive: active => ipcRenderer.send('clover:previewShortcutActive', Boolean(active)),
  openExternal: url => ipcRenderer.invoke('clover:openExternal', url),
  openPreviewInBrowser: url => ipcRenderer.invoke('clover:openPreviewInBrowser', url),
  reachPreviewUrl: url => ipcRenderer.invoke('clover:preview:reach', url),
  fetchLinkTitle: url => ipcRenderer.invoke('clover:fetchLinkTitle', url),
  resolveFavicon: url => ipcRenderer.invoke('clover:resolveFavicon', url),
  sanitizeWorkspaceCwd: cwd => ipcRenderer.invoke('clover:workspace:sanitize', cwd),
  settings: {
    getDefaultProjectDir: () => ipcRenderer.invoke('clover:setting:defaultProjectDir:get'),
    setDefaultProjectDir: dir => ipcRenderer.invoke('clover:setting:defaultProjectDir:set', dir),
    pickDefaultProjectDir: () => ipcRenderer.invoke('clover:setting:defaultProjectDir:pick')
  },
  zoom: {
    // Current zoom of this window, as { level, percent }.
    get: () => ipcRenderer.invoke('clover:zoom:get'),
    // Synchronous zoom factor (1 = 100%). Coordinate math needs it in the
    // same tick as the event it converts, so no IPC round-trip here.
    factor: () => webFrame.getZoomFactor(),
    setPercent: percent => ipcRenderer.send('clover:zoom:set-percent', percent),
    // Fires on every zoom change, including the Ctrl/Cmd +/-/0 shortcuts,
    // so the settings UI can stay in sync with the keyboard.
    onChanged: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('clover:zoom:changed', listener)

      return () => ipcRenderer.removeListener('clover:zoom:changed', listener)
    }
  },
  revealLogs: () => ipcRenderer.invoke('clover:logs:reveal'),
  getRecentLogs: () => ipcRenderer.invoke('clover:logs:recent'),
  // Fire-and-forget: persists a renderer error-boundary catch (with component
  // stack) to desktop.log so crashes survive the window (#79428).
  reportRendererError: report => ipcRenderer.send('clover:logs:renderer-error', report),
  readDir: dirPath => ipcRenderer.invoke('clover:fs:readDir', dirPath),
  gitRoot: startPath => ipcRenderer.invoke('clover:fs:gitRoot', startPath),
  revealPath: targetPath => ipcRenderer.invoke('clover:fs:reveal', targetPath),
  openDir: dirPath => ipcRenderer.invoke('clover:fs:openDir', dirPath),
  desktopPluginsRoot: () => ipcRenderer.invoke('clover:fs:desktopPluginsRoot'),
  logsRoot: () => ipcRenderer.invoke('clover:fs:logsRoot'),
  agentPluginsRoot: () => ipcRenderer.invoke('clover:fs:agentPluginsRoot'),
  renamePath: (targetPath, newName) => ipcRenderer.invoke('clover:fs:rename', targetPath, newName),
  writeTextFile: (filePath, content) => ipcRenderer.invoke('clover:fs:writeText', filePath, content),
  trashPath: targetPath => ipcRenderer.invoke('clover:fs:trash', targetPath),
  git: {
    worktreeList: repoPath => ipcRenderer.invoke('clover:git:worktreeList', repoPath),
    worktreeAdd: (repoPath, options) => ipcRenderer.invoke('clover:git:worktreeAdd', repoPath, options),
    worktreeRemove: (repoPath, worktreePath, options) =>
      ipcRenderer.invoke('clover:git:worktreeRemove', repoPath, worktreePath, options),
    branchSwitch: (repoPath, branch) => ipcRenderer.invoke('clover:git:branchSwitch', repoPath, branch),
    branchList: repoPath => ipcRenderer.invoke('clover:git:branchList', repoPath),
    baseBranchList: repoPath => ipcRenderer.invoke('clover:git:baseBranchList', repoPath),
    repoStatus: repoPath => ipcRenderer.invoke('clover:git:repoStatus', repoPath),
    fileDiff: (repoPath, filePath) => ipcRenderer.invoke('clover:git:fileDiff', repoPath, filePath),
    scanRepos: (roots, options) => ipcRenderer.invoke('clover:git:scanRepos', roots, options),
    review: {
      list: (repoPath, scope, baseRef) => ipcRenderer.invoke('clover:git:review:list', repoPath, scope, baseRef),
      diff: (repoPath, filePath, scope, baseRef, staged) =>
        ipcRenderer.invoke('clover:git:review:diff', repoPath, filePath, scope, baseRef, staged),
      stage: (repoPath, filePath) => ipcRenderer.invoke('clover:git:review:stage', repoPath, filePath),
      unstage: (repoPath, filePath) => ipcRenderer.invoke('clover:git:review:unstage', repoPath, filePath),
      revert: (repoPath, filePath) => ipcRenderer.invoke('clover:git:review:revert', repoPath, filePath),
      revParse: (repoPath, ref) => ipcRenderer.invoke('clover:git:review:revParse', repoPath, ref),
      commit: (repoPath, message, push) => ipcRenderer.invoke('clover:git:review:commit', repoPath, message, push),
      commitContext: repoPath => ipcRenderer.invoke('clover:git:review:commitContext', repoPath),
      push: repoPath => ipcRenderer.invoke('clover:git:review:push', repoPath),
      shipInfo: repoPath => ipcRenderer.invoke('clover:git:review:shipInfo', repoPath),
      prList: (repoPath, branches, numbers) =>
        ipcRenderer.invoke('clover:git:review:prList', repoPath, branches, numbers),
      fetchPrComment: (repoPath, url) => ipcRenderer.invoke('clover:git:review:fetchPrComment', repoPath, url),
      createPr: repoPath => ipcRenderer.invoke('clover:git:review:createPr', repoPath)
    }
  },
  terminal: {
    cwd: id => ipcRenderer.invoke('clover:terminal:cwd', id),
    dispose: id => ipcRenderer.invoke('clover:terminal:dispose', id),
    resize: (id, size) => ipcRenderer.invoke('clover:terminal:resize', id, size),
    start: options => ipcRenderer.invoke('clover:terminal:start', options),
    write: (id, data) => ipcRenderer.invoke('clover:terminal:write', id, data),
    onData: (id, callback) => {
      const channel = `clover:terminal:${id}:data`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    },
    onExit: (id, callback) => {
      const channel = `clover:terminal:${id}:exit`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    }
  },
  onClosePreviewRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('clover:close-preview-requested', listener)

    return () => ipcRenderer.removeListener('clover:close-preview-requested', listener)
  },
  onPreviewNav: callback => {
    const listener = (_event, command) => callback(command)
    ipcRenderer.on('clover:preview-nav', listener)

    return () => ipcRenderer.removeListener('clover:preview-nav', listener)
  },
  onOpenFolderRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('clover:open-folder-requested', listener)

    return () => ipcRenderer.removeListener('clover:open-folder-requested', listener)
  },
  onOpenUpdatesRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('clover:open-updates', listener)

    return () => ipcRenderer.removeListener('clover:open-updates', listener)
  },
  onDeepLink: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('clover:deep-link', listener)

    return () => ipcRenderer.removeListener('clover:deep-link', listener)
  },
  signalDeepLinkReady: () => ipcRenderer.invoke('clover:deep-link-ready'),
  probePluginRepo: payload => ipcRenderer.invoke('clover:plugin:probe', payload),
  installDesktopPlugin: payload => ipcRenderer.invoke('clover:plugin:installDesktop', payload),
  onWindowStateChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('clover:window-state-changed', listener)

    return () => ipcRenderer.removeListener('clover:window-state-changed', listener)
  },
  onFocusSession: callback => {
    const listener = (_event, sessionId) => callback(sessionId)
    ipcRenderer.on('clover:focus-session', listener)

    return () => ipcRenderer.removeListener('clover:focus-session', listener)
  },
  onNotificationAction: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('clover:notification-action', listener)

    return () => ipcRenderer.removeListener('clover:notification-action', listener)
  },
  onNotificationActivate: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('clover:notification-activate', listener)

    return () => ipcRenderer.removeListener('clover:notification-activate', listener)
  },
  onPreviewFileChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('clover:preview-file-changed', listener)

    return () => ipcRenderer.removeListener('clover:preview-file-changed', listener)
  },
  onBackendExit: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('clover:backend-exit', listener)

    return () => ipcRenderer.removeListener('clover:backend-exit', listener)
  },
  // Soft gateway-mode apply finished tearing down the primary backend. Renderer
  // should wipe session lists + re-dial without a window reload.
  onConnectionApplied: callback => {
    const listener = () => callback()
    ipcRenderer.on('clover:connection:applied', listener)

    return () => ipcRenderer.removeListener('clover:connection:applied', listener)
  },
  onPowerResume: callback => {
    const listener = () => callback()
    ipcRenderer.on('clover:power-resume', listener)

    return () => ipcRenderer.removeListener('clover:power-resume', listener)
  },
  // AC ↔ battery transitions; renderers slow their backstop polls on battery.
  getOnBattery: () => ipcRenderer.invoke('clover:power-battery:get'),
  onBatteryChanged: callback => {
    const listener = (_event, onBattery) => callback(Boolean(onBattery))
    ipcRenderer.on('clover:power-battery', listener)

    return () => ipcRenderer.removeListener('clover:power-battery', listener)
  },
  onBootProgress: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('clover:boot-progress', listener)

    return () => ipcRenderer.removeListener('clover:boot-progress', listener)
  },
  // First-launch bootstrap progress -- emitted by the install.ps1 stage
  // runner in main.ts (apps/desktop/electron/bootstrap-runner.ts).
  // Renderer's install overlay subscribes to live events and queries the
  // current snapshot via getBootstrapState() to recover after a devtools
  // reload mid-bootstrap.
  getBootstrapState: () => ipcRenderer.invoke('clover:bootstrap:get'),
  continueBootstrapLocal: () => ipcRenderer.invoke('clover:bootstrap:continue-local'),
  resetBootstrap: () => ipcRenderer.invoke('clover:bootstrap:reset'),
  repairBootstrap: () => ipcRenderer.invoke('clover:bootstrap:repair'),
  cancelBootstrap: () => ipcRenderer.invoke('clover:bootstrap:cancel'),
  onBootstrapEvent: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('clover:bootstrap:event', listener)

    return () => ipcRenderer.removeListener('clover:bootstrap:event', listener)
  },
  getVersion: () => ipcRenderer.invoke('clover:version'),
  getRemoteDisplayReason: () => ipcRenderer.invoke('clover:get-remote-display-reason'),
  uninstall: {
    summary: () => ipcRenderer.invoke('clover:uninstall:summary'),
    run: mode => ipcRenderer.invoke('clover:uninstall:run', { mode })
  },
  updates: {
    check: () => ipcRenderer.invoke('clover:updates:check'),
    apply: opts => ipcRenderer.invoke('clover:updates:apply', opts),
    getBranch: () => ipcRenderer.invoke('clover:updates:branch:get'),
    setBranch: name => ipcRenderer.invoke('clover:updates:branch:set', name),
    onProgress: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('clover:updates:progress', listener)

      return () => ipcRenderer.removeListener('clover:updates:progress', listener)
    }
  },
  themes: {
    fetchMarketplace: id => ipcRenderer.invoke('clover:vscode-theme:fetch', id),
    searchMarketplace: query => ipcRenderer.invoke('clover:vscode-theme:search', query)
  },
  // Find-in-page (Ctrl/Cmd+F): delegates to Electron's
  // webContents.findInPage on the IPC sender's window so a Cmd+F pressed
  // in a secondary session window searches THAT window, not the primary.
  // `onFoundInPage` returns the unsubscribe fn; the renderer wires it via
  // `initFindInPageListener` in store/find-in-page.ts and tears it down
  // when the FindBar unmounts.
  findInPage: (query, options) => ipcRenderer.invoke('clover:find-in-page', query, options),
  stopFindInPage: () => ipcRenderer.invoke('clover:stop-find-in-page'),
  onFoundInPage: callback => {
    const listener = (_event, result) => callback(result)
    ipcRenderer.on('clover:found-in-page', listener)

    return () => ipcRenderer.removeListener('clover:found-in-page', listener)
  },
  // Main-process `before-input-event` forwards Ctrl/Cmd+F here so renderer
  // can open the FindBar even when the GTK compositor has already grabbed
  // the chord at the windowing layer (#81727).
  onOpenFindBarRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('clover:open-find-bar', listener)

    return () => ipcRenderer.removeListener('clover:open-find-bar', listener)
  }
})
