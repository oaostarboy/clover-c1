const TELEMETRY_REQUEST_TIMEOUT_MS = 2e3;
const PROJECT_ID_SALT = "openui-project-id";
function getPostHogConfig(env) {
	const host = env?.["OPENUI_POSTHOG_HOST"] ?? "https://dgoeivjus9jfp.cloudfront.net";
	return {
		apiKey: env?.["OPENUI_POSTHOG_KEY"] ?? "phc_3OLW53x09ZTVZSV6BEpj5uycj3ooqR6KOemOjx04e3D",
		captureUrl: new URL("/capture/", host).toString()
	};
}
function isTruthyEnv(value) {
	return value === "1" || value?.toLowerCase() === "true";
}
function isTelemetryDisabled(env) {
	return isTruthyEnv(env?.["OPENUI_TELEMETRY_DISABLED"]) || isTruthyEnv(env?.["DO_NOT_TRACK"]);
}
/** Runtime telemetry is opt-in: it requires explicit consent and honors the disable flags. */
function isRuntimeTelemetryEnabled(env) {
	return isTruthyEnv(env?.["OPENUI_RUNTIME_TELEMETRY_ENABLED"]) && !isTelemetryDisabled(env);
}
function normalizeProjectIdentity(rawValue) {
	const value = rawValue.trim();
	if (!value) return "";
	try {
		const url = new URL(value);
		const repositoryPath = normalizeRepositoryPath(decodeURIComponent(url.pathname));
		if (url.protocol === "file:") return `file:${repositoryPath}`;
		if (url.host) return joinHostAndPath(url.host, repositoryPath);
	} catch {}
	if (/^[A-Za-z]:[\\/]/.test(value)) return normalizeRepositoryPath(value);
	const scpMatch = /^(?:[^@/\s]+@)?([^:/\s]+):(.+)$/.exec(value);
	if (scpMatch) return joinHostAndPath(scpMatch[1], scpMatch[2]);
	return normalizeRepositoryPath(value);
}
function joinHostAndPath(host, repositoryPath) {
	const normalizedHost = host.trim().toLowerCase();
	const normalizedPath = normalizeRepositoryPath(repositoryPath);
	return normalizedPath ? `${normalizedHost}/${normalizedPath}` : normalizedHost;
}
function normalizeRepositoryPath(repositoryPath) {
	return repositoryPath.trim().replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").replace(/\.git$/i, "");
}
//#endregion
export { isTelemetryDisabled as a, isRuntimeTelemetryEnabled as i, TELEMETRY_REQUEST_TIMEOUT_MS as n, isTruthyEnv as o, getPostHogConfig as r, normalizeProjectIdentity as s, PROJECT_ID_SALT as t };

//# sourceMappingURL=shared-tgSjXCMf.mjs.map