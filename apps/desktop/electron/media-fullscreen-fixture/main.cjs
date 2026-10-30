const { app, BrowserWindow, session } = require('electron')
const fs = require('node:fs')
const { isDesktopPermissionRequestAllowed } = require(process.env.CLOVER_TEST_PERMISSION_MODULE)

app.whenReady().then(async () => {
  session.defaultSession.setPermissionRequestHandler((_wc, permission, callback, details) => {
    callback(isDesktopPermissionRequestAllowed(permission, details))
  })
  session.defaultSession.setPermissionCheckHandler((_wc, permission) => permission === 'media')
  const win = new BrowserWindow({ width: 900, height: 700, webPreferences: { contextIsolation: true } })
  const video = fs.readFileSync(process.env.CLOVER_TEST_VIDEO).toString('base64')
  await win.loadURL(
    'data:text/html,' +
      encodeURIComponent(
        `<video controls loop style="width:640px;height:360px" src="data:video/mp4;base64,${video}"></video>`
      )
  )
})
app.on('window-all-closed', () => app.quit())
