// Keep capture checks consistent across the desktop permission hooks.
export function isDesktopPermissionRequestAllowed(permission: string, details?: unknown): boolean {
  // Chromium only sends this after a fullscreen request; do not grant
  // `automatic-fullscreen`, which would bypass the user-gesture requirement.
  if (permission === 'fullscreen') {
    return true
  }

  if (permission === 'audioCapture' || permission === 'videoCapture') {
    return true
  }

  if (permission !== 'media') {
    return false
  }

  const mediaTypes = (details as { mediaTypes?: string[] } | undefined)?.mediaTypes

  // Windows may omit capture metadata.
  if (!Array.isArray(mediaTypes) || mediaTypes.length === 0) {
    return true
  }

  return mediaTypes.includes('audio') || mediaTypes.includes('video')
}
