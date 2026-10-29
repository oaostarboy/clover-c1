import { a as isTelemetryDisabled, n as TELEMETRY_REQUEST_TIMEOUT_MS, o as isTruthyEnv, r as getPostHogConfig, s as normalizeProjectIdentity, t as PROJECT_ID_SALT } from "./shared-tgSjXCMf.mjs";
import { createRequire } from "node:module";
import { id, isCI } from "ci-info";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { release } from "node:os";
import path from "node:path";
//#region \0rolldown/runtime.js
var __commonJSMin = (cb, mod) => () => (mod || (cb((mod = { exports: {} }).exports, mod), cb = null), mod.exports);
var __require = /* @__PURE__ */ createRequire(import.meta.url);
//#endregion
//#region src/telemetry/install.ts
/**
* Installation telemetry disclosure
*
* Sent during package postinstall unless telemetry is disabled:
* - A random installation ID and a salted SHA-256 project ID
* - Lang Core, Node.js, OS, architecture, and package-manager versions
* - CI provider category, Docker status, project-ID source category, and schema version
*
* The JSON payload contains no direct PII or application-user data. It never includes
* names, email addresses, usernames, raw Git origins, repository or project names,
* filesystem paths, branches, commits, credentials, environment values, source code,
* prompts, messages, generated output, component or tool definitions, errors, logs,
* stack traces, routes, or UI interactions. As with any HTTP request, the telemetry
* proxy and PostHog can observe the source IP as transport metadata; the script does
* not read it or add it to the payload.
*/
var import_telemetry_state = (/* @__PURE__ */ __commonJSMin(((exports, module) => {
	const crypto = __require("node:crypto");
	const fs = __require("node:fs");
	const os = __require("node:os");
	const path$1 = __require("node:path");
	function createTelemetryStateIO(overrides = {}) {
		return {
			homedir: os.homedir,
			readFile: (file) => fs.readFileSync(file, "utf8"),
			writeFile: (file, contents, options) => fs.writeFileSync(file, contents, options),
			mkdir: (directory) => fs.mkdirSync(directory, {
				recursive: true,
				mode: 448
			}),
			rename: fs.renameSync,
			link: fs.linkSync,
			unlink: fs.unlinkSync,
			randomUUID: crypto.randomUUID,
			...overrides
		};
	}
	function getTelemetryStateFile(env, io) {
		const configRoot = env["XDG_CONFIG_HOME"] || path$1.join(io.homedir(), ".config");
		return path$1.join(configRoot, "openui", "telemetry.json");
	}
	function readStoredState(file, io) {
		let contents;
		try {
			contents = io.readFile(file);
		} catch (error) {
			return {
				stored: {},
				status: hasErrorCode(error, "ENOENT") ? "missing" : "unreadable"
			};
		}
		try {
			const parsed = JSON.parse(contents);
			return {
				stored: isRecord(parsed) ? parsed : {},
				status: "readable"
			};
		} catch {
			return {
				stored: {},
				status: "readable"
			};
		}
	}
	function getStoredDistinctId(stored) {
		const distinctId = stored["distinctId"];
		return typeof distinctId === "string" && distinctId ? distinctId : void 0;
	}
	function loadTelemetryState(options = {}) {
		const env = options.env ?? process.env;
		const io = createTelemetryStateIO(options.io);
		const file = getTelemetryStateFile(env, io);
		const { stored, status } = readStoredState(file, io);
		const storedDistinctId = getStoredDistinctId(stored);
		return {
			file,
			status,
			state: {
				...stored,
				distinctId: storedDistinctId ?? io.randomUUID()
			},
			changed: !storedDistinctId
		};
	}
	/**
	* Writes the state without exposing partial files to concurrent processes. New state is published
	* only if no other process created it first; otherwise that process's identity is reused.
	*/
	function saveTelemetryState(context, state, overrides) {
		if (context.status === "unreadable") return {
			...context,
			state,
			changed: false
		};
		const io = createTelemetryStateIO(overrides);
		const contents = JSON.stringify(state);
		const temporaryFile = `${context.file}.${io.randomUUID()}.tmp`;
		const saved = (savedState) => ({
			...context,
			status: "readable",
			state: savedState,
			changed: false
		});
		try {
			io.mkdir(path$1.dirname(context.file));
			io.writeFile(temporaryFile, contents, {
				mode: 384,
				flag: "wx"
			});
			if (context.status === "readable") {
				io.rename(temporaryFile, context.file);
				return saved(state);
			}
			try {
				io.link(temporaryFile, context.file);
				return saved(state);
			} catch (error) {
				if (!hasErrorCode(error, "EEXIST")) try {
					io.writeFile(context.file, contents, {
						mode: 384,
						flag: "wx"
					});
					return saved(state);
				} catch (fallbackError) {
					if (!hasErrorCode(fallbackError, "EEXIST")) throw fallbackError;
				}
			}
			const winner = readStoredState(context.file, io).stored;
			const winnerDistinctId = getStoredDistinctId(winner);
			return winnerDistinctId ? saved({
				...winner,
				distinctId: winnerDistinctId
			}) : saved(state);
		} catch {
			return {
				...context,
				state,
				changed: false
			};
		} finally {
			try {
				io.unlink(temporaryFile);
			} catch {}
		}
	}
	function hasErrorCode(error, code) {
		return isRecord(error) && error["code"] === code;
	}
	function isRecord(value) {
		return typeof value === "object" && value !== null && !Array.isArray(value);
	}
	module.exports = {
		createTelemetryStateIO,
		loadTelemetryState,
		saveTelemetryState
	};
})))();
const INSTALL_EVENT = "openui_lang_core_installed";
const INSTALL_NOTICE = "\n◆ OpenUI Lang Core sends pseudonymous installation telemetry to PostHog.\n  It includes a salted project ID, OpenUI/Node/OS/package-manager metadata, and the network IP observed by PostHog.\n  It does not send repository URLs, paths, source code, prompts, errors, or application data.\n  Disable it with OPENUI_TELEMETRY_DISABLED=1 or DO_NOT_TRACK=1.\n\n";
function hashProjectIdentity(salt, projectIdentity) {
	return createHash("sha256").update(salt).update("\0").update(projectIdentity).digest("hex");
}
function detectPackageManager(env) {
	const match = (env["npm_config_user_agent"]?.trim() ?? "").match(/^(npm|pnpm|yarn|bun)\/([^\s]+)/i);
	if (!match) return { name: "unknown" };
	const name = match[1].toLowerCase();
	const version = match[2].slice(0, 64);
	return version ? {
		name,
		version
	} : { name };
}
async function runInstallTelemetry(options = {}) {
	const env = options.env ?? process.env;
	if (isTelemetryDisabled(env)) return { status: "disabled" };
	let payload;
	try {
		const io = createInstallTelemetryIO(options.io);
		let telemetryState = (0, import_telemetry_state.loadTelemetryState)({
			env,
			io
		});
		let { state, changed } = telemetryState;
		if (state["langCorePostinstallNoticeShown"] !== true) {
			io.stderr(INSTALL_NOTICE);
			state = {
				...state,
				langCorePostinstallNoticeShown: true
			};
			changed = true;
		}
		if (changed) {
			telemetryState = (0, import_telemetry_state.saveTelemetryState)(telemetryState, state, io);
			state = telemetryState.state;
		}
		const projectIdentity = resolveProjectIdentity(path.resolve(env["INIT_CWD"] || io.cwd()), env, io);
		const packageManager = detectPackageManager(env);
		payload = {
			distinctId: state.distinctId,
			event: INSTALL_EVENT,
			properties: {
				telemetry_schema_version: 2,
				project_id: hashProjectIdentity(PROJECT_ID_SALT, projectIdentity.value),
				project_id_source: projectIdentity.source,
				lang_core_version: env["OPENUI_LANG_CORE_PACKAGE_VERSION"] || env["npm_package_version"] || "unknown",
				node_version: process.version,
				system_platform: io.platform,
				system_release: io.release(),
				system_architecture: io.architecture,
				package_manager: packageManager.name,
				...packageManager.version ? { package_manager_version: packageManager.version } : {},
				ci: isCI,
				...id ? { ci_name: id } : {},
				is_docker: io.isDocker()
			}
		};
		if (isTruthyEnv(env["OPENUI_TELEMETRY_DEBUG"])) {
			io.stdout(`[OpenUI telemetry debug]\n${JSON.stringify(payload, null, 2)}\n`);
			return {
				status: "debug",
				payload
			};
		}
		await (options.send ?? sendToPostHog)(payload, env);
		return {
			status: "sent",
			payload
		};
	} catch {
		return {
			status: "failed",
			...payload ? { payload } : {}
		};
	}
}
function createInstallTelemetryIO(overrides = {}) {
	return {
		...(0, import_telemetry_state.createTelemetryStateIO)(),
		cwd: () => process.cwd(),
		getGitOrigin: defaultGetGitOrigin,
		platform: process.platform,
		architecture: process.arch,
		release,
		isDocker: defaultIsDocker,
		stdout: (message) => process.stdout.write(message),
		stderr: (message) => process.stderr.write(message),
		...overrides
	};
}
function resolveProjectIdentity(projectRoot, env, io) {
	const gitOrigin = io.getGitOrigin(projectRoot)?.trim();
	if (gitOrigin) return {
		value: normalizeProjectIdentity(gitOrigin),
		source: "git_origin"
	};
	const repositoryUrl = env["REPOSITORY_URL"]?.trim();
	if (repositoryUrl) return {
		value: normalizeProjectIdentity(repositoryUrl),
		source: "repository_url"
	};
	return {
		value: projectRoot,
		source: "install_root"
	};
}
function defaultGetGitOrigin(projectRoot) {
	try {
		return execFileSync("git", [
			"-C",
			projectRoot,
			"config",
			"--get",
			"remote.origin.url"
		], {
			encoding: "utf8",
			stdio: [
				"ignore",
				"pipe",
				"ignore"
			],
			timeout: 1e3
		}).trim();
	} catch {
		return;
	}
}
function defaultIsDocker() {
	if (existsSync("/.dockerenv")) return true;
	try {
		return /docker|containerd|kubepods/i.test(readFileSync("/proc/self/cgroup", "utf8"));
	} catch {
		return false;
	}
}
async function sendToPostHog(payload, env) {
	const postHog = getPostHogConfig(env);
	const response = await fetch(postHog.captureUrl, {
		method: "POST",
		headers: { "content-type": "application/json" },
		body: JSON.stringify({
			api_key: postHog.apiKey,
			event: payload.event,
			properties: {
				distinct_id: payload.distinctId,
				...payload.properties
			}
		}),
		signal: AbortSignal.timeout(TELEMETRY_REQUEST_TIMEOUT_MS)
	});
	if (!response.ok) throw new Error(`PostHog capture failed: ${response.status}`);
}
//#endregion
//#region src/postinstall.ts
runInstallTelemetry().catch(() => {});
//#endregion
export {};

//# sourceMappingURL=postinstall.mjs.map