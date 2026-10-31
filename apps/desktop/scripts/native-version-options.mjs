export function nativeVersionOptions(version) {
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.exec(version)
  return match ? { buildNumber: match[4], buildVersion: version } : {}
}
