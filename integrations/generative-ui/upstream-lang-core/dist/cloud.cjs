Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
//#region src/cloud.ts
/**
* Wire pins for OpenUI Cloud's managed artifact libraries. Cloud rejects a
* non-numeric or too-old version.
*/
const SLIDES_LIBRARY_VERSION = "0.1.0";
const REPORT_LIBRARY_VERSION = "0.1.0";
const DEFAULT_LIBRARY_VERSION = {
	slides: SLIDES_LIBRARY_VERSION,
	report: REPORT_LIBRARY_VERSION
};
/**
* Build the Responses `tools[]` entry that enables Cloud's managed artifact tool.
*
*   tools: [artifactTool()]                          // all artifact types
*   tools: [artifactTool({ artifacts: ["report"] })] // report only
*   tools: [artifactTool({
*     artifacts: [
*       { type: "slides", instruction: "Use the corporate template." },
*       "report",
*     ],
*   })]
*
* Pass at most one artifactTool() entry per request — Cloud keys the
* artifact config by tool type, so a second entry silently replaces the first.
*/
function artifactTool(options = {}) {
	const { artifacts } = options;
	if (artifacts === void 0) return {
		type: "artifact",
		artifacts: Object.keys(DEFAULT_LIBRARY_VERSION).map((kind) => ({
			artifact_type: kind,
			library_version: DEFAULT_LIBRARY_VERSION[kind]
		}))
	};
	if (artifacts.length === 0) throw new Error("artifactTool: `artifacts` must not be empty — omit it to enable all artifact types.");
	const seen = /* @__PURE__ */ new Set();
	return {
		type: "artifact",
		artifacts: artifacts.map((artifact) => {
			const opt = typeof artifact === "string" ? { type: artifact } : artifact;
			if (!(opt.type in DEFAULT_LIBRARY_VERSION)) throw new Error(`artifactTool: unknown artifact type '${opt.type}'. Supported: ${Object.keys(DEFAULT_LIBRARY_VERSION).join(", ")}.`);
			if (seen.has(opt.type)) throw new Error(`artifactTool: duplicate artifact '${opt.type}'.`);
			seen.add(opt.type);
			return {
				artifact_type: opt.type,
				...opt.instruction && { instruction: opt.instruction },
				library_version: opt.libraryVersion ?? DEFAULT_LIBRARY_VERSION[opt.type]
			};
		})
	};
}
//#endregion
exports.REPORT_LIBRARY_VERSION = REPORT_LIBRARY_VERSION;
exports.SLIDES_LIBRARY_VERSION = SLIDES_LIBRARY_VERSION;
exports.artifactTool = artifactTool;

//# sourceMappingURL=cloud.cjs.map