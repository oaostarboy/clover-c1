//#region \0rolldown/runtime.js
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __commonJSMin = (cb, mod) => () => (mod || (cb((mod = { exports: {} }).exports, mod), cb = null), mod.exports);
var __copyProps = (to, from, except, desc) => {
	if (from && typeof from === "object" || typeof from === "function") for (var keys = __getOwnPropNames(from), i = 0, n = keys.length, key; i < n; i++) {
		key = keys[i];
		if (!__hasOwnProp.call(to, key) && key !== except) __defProp(to, key, {
			get: ((k) => from[k]).bind(null, key),
			enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable
		});
	}
	return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", {
	value: mod,
	enumerable: true
}) : target, mod));
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
Object.defineProperty(exports, "PROJECT_ID_SALT", {
	enumerable: true,
	get: function() {
		return PROJECT_ID_SALT;
	}
});
Object.defineProperty(exports, "TELEMETRY_REQUEST_TIMEOUT_MS", {
	enumerable: true,
	get: function() {
		return TELEMETRY_REQUEST_TIMEOUT_MS;
	}
});
Object.defineProperty(exports, "__commonJSMin", {
	enumerable: true,
	get: function() {
		return __commonJSMin;
	}
});
Object.defineProperty(exports, "__toESM", {
	enumerable: true,
	get: function() {
		return __toESM;
	}
});
Object.defineProperty(exports, "getPostHogConfig", {
	enumerable: true,
	get: function() {
		return getPostHogConfig;
	}
});
Object.defineProperty(exports, "isRuntimeTelemetryEnabled", {
	enumerable: true,
	get: function() {
		return isRuntimeTelemetryEnabled;
	}
});
Object.defineProperty(exports, "isTelemetryDisabled", {
	enumerable: true,
	get: function() {
		return isTelemetryDisabled;
	}
});
Object.defineProperty(exports, "isTruthyEnv", {
	enumerable: true,
	get: function() {
		return isTruthyEnv;
	}
});
Object.defineProperty(exports, "normalizeProjectIdentity", {
	enumerable: true,
	get: function() {
		return normalizeProjectIdentity;
	}
});

//# sourceMappingURL=shared-CBGuna5j.cjs.map