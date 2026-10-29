//#region src/cloud.d.ts
type ArtifactKind = "slides" | "report";
interface ArtifactOption {
  type: ArtifactKind;
  /** Per-artifact instruction, folded into the Cloud tool description. */
  instruction?: string;
  /** Override this package's pinned library version for this artifact. */
  libraryVersion?: string;
}
interface ArtifactToolOptions {
  /** Which artifacts to enable. Omit to enable all artifact types. */
  artifacts?: Array<ArtifactKind | ArtifactOption>;
}
interface ArtifactWireEntry {
  artifact_type: ArtifactKind;
  instruction?: string;
  library_version?: string;
}
/** The Responses `tools[]` entry enabling OpenUI Cloud's managed artifact tool. */
interface ResponsesArtifactToolEntry {
  type: "artifact";
  /** Absent → all supported artifact types enabled. */
  artifacts?: ArtifactWireEntry[];
}
/**
 * Wire pins for OpenUI Cloud's managed artifact libraries. Cloud rejects a
 * non-numeric or too-old version.
 */
declare const SLIDES_LIBRARY_VERSION = "0.1.0";
declare const REPORT_LIBRARY_VERSION = "0.1.0";
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
declare function artifactTool(options?: ArtifactToolOptions): ResponsesArtifactToolEntry;
//#endregion
export { ArtifactKind, ArtifactOption, ArtifactToolOptions, ArtifactWireEntry, REPORT_LIBRARY_VERSION, ResponsesArtifactToolEntry, SLIDES_LIBRARY_VERSION, artifactTool };
//# sourceMappingURL=cloud.d.mts.map