import { z } from "zod/v4";
import * as z$1 from "zod/v4/core";

//#region src/parser/ast.d.ts
/**
 * Discriminated union representing every value that can appear in an
 * openui-lang expression. The `k` field is the discriminant.
 *
 * Literal & structural nodes:
 * - `Comp`       — a component call: `Header("Hello", "Subtitle")`
 * - `Str`        — a string literal: `"hello"`
 * - `Num`        — a number literal: `42` or `3.14`
 * - `Bool`       — a boolean literal: `true` or `false`
 * - `Null`       — the null literal
 * - `Arr`        — an array: `[a, b, c]`
 * - `Obj`        — an object: `{ key: value }`
 * - `Ref`        — a reference to another statement: `myTable`
 * - `Ph`         — a placeholder for an unresolvable reference
 *
 * Reactive & expression nodes:
 * - `StateRef`   — a reactive state variable reference: `$count`
 * - `RuntimeRef` — a reference resolved at runtime (e.g. Query results)
 * - `BinOp`      — a binary operation: `a + b`, `x == y`
 * - `UnaryOp`    — a unary operation: `!flag`
 * - `Ternary`    — a conditional expression: `cond ? a : b`
 * - `Member`     — dot member access: `obj.field`
 * - `Index`      — bracket index access: `arr[0]`
 * - `Assign`     — state assignment: `$count = $count + 1`
 */
type ASTNode = {
  k: "Comp";
  name: string;
  args: ASTNode[];
  mappedProps?: Record<string, ASTNode>; /** Set on an `@Name(...)` call whose name is not a built-in or action step. */
  fn?: true;
} | {
  k: "Str";
  v: string;
} | {
  k: "Num";
  v: number;
} | {
  k: "Bool";
  v: boolean;
} | {
  k: "Null";
} | {
  k: "Arr";
  els: ASTNode[];
} | {
  k: "Obj";
  entries: [string, ASTNode][];
} | {
  k: "Ref";
  n: string;
} | {
  k: "Ph";
  n: string;
} | {
  k: "StateRef";
  n: string;
} | {
  k: "RuntimeRef";
  n: string;
  refType: "query" | "mutation";
} | {
  k: "BinOp";
  op: string;
  left: ASTNode;
  right: ASTNode;
} | {
  k: "UnaryOp";
  op: string;
  operand: ASTNode;
} | {
  k: "Ternary";
  cond: ASTNode;
  then: ASTNode;
  else: ASTNode;
} | {
  k: "Member";
  obj: ASTNode;
  field: string;
} | {
  k: "Index";
  obj: ASTNode;
  index: ASTNode;
} | {
  k: "Assign";
  target: string;
  value: ASTNode;
};
declare function walkAST(node: ASTNode, visit: (node: ASTNode) => void): void;
//#endregion
//#region src/parser/types.d.ts
type JSONSchemaProperty = Record<string, unknown>;
/** A JSON Schema object; keywords are added as libraries use them (anyOf: the ActionExpression union). */
type JSONSchemaDef = {
  properties?: JSONSchemaProperty;
  required?: string[];
  description?: string;
  type?: string;
  anyOf?: JSONSchemaProperty[];
};
/** A function's params and return value as JSON Schema. */
interface FunctionSchema {
  description?: string;
  /** JSON Schema object of the params. Property order is the positional order in programs. */
  params: JSONSchemaDef;
  /** JSON Schema of the return value. */
  returns?: JSONSchemaProperty;
}
/**
 * The JSON Schema document produced by `library.toJSONSchema()`.
 * All component schemas live in `$defs`, keyed by component name.
 * Library functions and actions live in `functions` and `actions`.
 */
interface LibraryJSONSchema {
  $defs?: Record<string, JSONSchemaDef>;
  /** Component names as keys. Other `$defs` are data shapes. */
  properties?: Record<string, unknown>;
  functions?: Record<string, FunctionSchema>;
  /** Custom actions, when the library has any. They have no `returns`. */
  actions?: Record<string, FunctionSchema>;
}
interface ParamDef {
  /** Parameter name, e.g. "title", "columns". */
  name: string;
  /** Whether the parameter is required by the component. */
  required: boolean;
  /** Default value from JSON Schema — used when the required field is missing/null. */
  defaultValue?: unknown;
  /** The raw JSON Schema fragment for this param. */
  schema?: unknown;
}
/** `@Percent(part, total)` -> params [{name: "part"}, {name: "total"}], kept as AST */
interface CallDef {
  kind: "function" | "action";
  params: ParamDef[];
  /** Built-ins coerce their args: the runtime maps them by name, unvalidated. */
  builtin?: true;
  /** Lazy built-ins (@Each, Action, @Run, @Set, @Reset) get their args unevaluated. */
  lazy?: true;
}
/** "Card" -> its params; callDefs: "Sum" -> built-in CallDef, "Percent" -> library CallDef */
type ParamMap = Map<string, {
  params: ParamDef[];
}> & {
  callDefs?: Map<string, CallDef>;
};
/**
 * A fully resolved component node from the parser.
 *
 * The parser converts openui-lang text into a tree of these nodes.
 * Each node represents one component invocation with its positional
 * arguments mapped into named `props` via the library's Zod key order.
 */
interface ElementNode {
  type: "element";
  /** Source variable name (e.g. "header" from `header = TextContent(...)`). Undefined for inline components. */
  statementId?: string;
  /** Component name as defined in the library (e.g. "Table", "BarChart"). */
  typeName: string;
  /** Named props produced by positional-to-named mapping in the parser. */
  props: Record<string, unknown>;
  /**
   * True when the parser hasn't received all tokens for this node yet
   * (streaming in progress).
   */
  partial: boolean;
  /**
   * False when all props are static literals — evaluation can be skipped.
   * Undefined is treated as true (dynamic) for backward compatibility.
   */
  hasDynamicProps?: boolean;
}
/**
 * Validation error codes for schema-related issues.
 */
type ValidationErrorCode = "missing-required" | "null-required" | "unknown-component" | "inline-reserved" | "excess-args" | "type-mismatch" | "unknown-function" | "no-root";
/**
 * A prop validation error. Components with missing required props are
 * dropped from the output tree and errors are recorded here.
 */
interface ValidationError {
  /** Machine-readable error code. */
  code: ValidationErrorCode;
  /** Component type name, e.g. "Header", "BarChart". */
  component: string;
  /** JSON Pointer path within the props object, e.g. "/title", "". */
  path: string;
  /** Human-readable error message. */
  message: string;
  /** Statement name that triggered the error (e.g. "header", "chart"). */
  statementId?: string;
  /** "warning" when the program still renders (e.g. the `no-root` fallback); absent means error. */
  severity?: "warning";
}
/**
 * Error sources across the openui-lang pipeline.
 */
type OpenUIErrorSource = "parser" | "runtime" | "query" | "mutation";
/**
 * Machine-readable error codes for the openui-lang pipeline.
 *
 * - Parser: "unknown-component", "missing-required", "null-required", "inline-reserved",
 *   "excess-args", "type-mismatch", "unknown-function", "no-root", "parse-exception", "parse-failed"
 * - Runtime: "runtime-error" (prop evaluation), "render-error" (React render)
 * - Query/Mutation: "tool-not-found", "tool-error", "mcp-error"
 */
type OpenUIErrorCode = ValidationErrorCode | "runtime-error" | "render-error" | "parse-exception" | "parse-failed" | "tool-not-found" | "tool-error" | "mcp-error";
/**
 * Structured, LLM-friendly error from the openui-lang pipeline.
 *
 * Designed for an automated correction loop: send these to the LLM so it can
 * generate patches. Only includes errors that are fixable by changing the
 * openui-lang code — transient streaming errors, network failures, and tool
 * execution errors are excluded.
 */
interface OpenUIError {
  /** Where the error originated. */
  source: OpenUIErrorSource;
  /** Machine-readable error code. */
  code: OpenUIErrorCode;
  /** Human/LLM-readable description of what went wrong. */
  message: string;
  /** Statement name (e.g. "header", "metrics") — tells the LLM which statement to patch. */
  statementId?: string;
  /** Component type name (e.g. "BarChart", "Query"). */
  component?: string;
  /** Prop path (e.g. "/title"). */
  path?: string;
  /** Tool name for query/mutation errors (e.g. "get_users", "create_item"). */
  toolName?: string;
  /** Actionable fix context for the LLM (e.g. available components, correct signature). */
  hint?: string;
  /** "warning" when the program still renders; absent means error. */
  severity?: "warning";
}
/**
 * Built-in action types for host app events.
 */
declare enum BuiltinActionType {
  ContinueConversation = "continue_conversation",
  OpenUrl = "open_url"
}
/**
 * A single step in an ActionPlan.
 * Step type values match ACTION_STEPS in builtins.ts (single source of truth).
 */
type ActionStep = {
  type: "run";
  statementId: string;
  refType: "query" | "mutation";
} | {
  type: "continue_conversation";
  message: string;
  context?: unknown;
} | {
  type: "open_url";
  url: string;
} | {
  type: "set";
  target: string;
  valueAST: ASTNode;
} | {
  type: "reset";
  targets: string[];
} /** A library action from `defineAction`; delivered to `onAction` as `{ type: name, params }`. */ | {
  type: "custom_action";
  name: string;
  params: Record<string, unknown>;
};
/**
 * An ordered sequence of steps to execute when a button is clicked.
 * Produced by evaluating an Action() expression at runtime.
 */
interface ActionPlan {
  steps: ActionStep[];
}
/**
 * Structured action event fired by interactive components.
 */
interface ActionEvent {
  /** Action type. See `BuiltinActionType` for built-in types. */
  type: BuiltinActionType | (string & {});
  /** Action-specific params (e.g. { url } for OpenUrl, custom params for Custom). */
  params: Record<string, unknown>;
  /** Human-readable label for the action (displayed as user message in chat). */
  humanFriendlyMessage: string;
  /** Raw form state at the time of the action — all field values. */
  formState?: Record<string, unknown>;
  /** The form name that triggered this action, if any. */
  formName?: string;
}
/**
 * Extracted info about a Query() call from the parsed program.
 */
interface QueryStatementInfo {
  /** Statement name that holds this query (e.g. "metrics"). */
  statementId: string;
  /** First arg AST — the tool name (should evaluate to a string). */
  toolAST: ASTNode | null;
  /** Second arg AST — the arguments object (may contain $var refs). */
  argsAST: ASTNode | null;
  /** Third arg AST — default data returned before fetch resolves. */
  defaultsAST: ASTNode | null;
  /** Fourth arg AST — refresh interval in seconds. */
  refreshAST: ASTNode | null;
  /** Pre-computed $variable deps from argsAST (extracted at parse time). */
  deps?: string[];
  /** False while the Query() call is still being streamed. */
  complete: boolean;
}
/**
 * Extracted info about a Mutation() call from the parsed program.
 */
interface MutationStatementInfo {
  /** Statement name that holds this mutation (e.g. "createResult"). */
  statementId: string;
  /** First arg AST — the tool name (should evaluate to a string). */
  toolAST: ASTNode | null;
  /** Second arg AST — the arguments object (may contain $var refs). */
  argsAST: ASTNode | null;
}
/**
 * The output of a single `parser.parse(text)` call.
 *
 * During streaming, each chunk produces a new ParseResult as the
 * accumulated text is re-parsed. The `root` progressively resolves
 * from null → partial tree → complete tree.
 */
interface ParseResult {
  /** The root ElementNode (typically a Root component), or null if parsing hasn't produced one yet. */
  root: ElementNode | null;
  /** Set when the entry picks a component at runtime (`root = $t ? A : B`); see `evaluateRoot`. */
  rootExpression?: ASTNode;
  meta: {
    /** True if the parser detected truncated/incomplete input. */incomplete: boolean; /** Names of references used but not yet defined (dropped as null in output). */
    unresolved: string[]; /** Names of value statements defined but not reachable from root. Excludes $state, Query, and Mutation declarations. */
    orphaned: string[]; /** Total number of `identifier = Expression` statements parsed. */
    statementCount: number;
    /**
     * Prop validation errors. Components with missing required props are
     * redacted (dropped as null) and listed here.
     */
    errors: ValidationError[];
  };
  /** $variable declarations — maps "$varName" to its materialized default value. */
  stateDeclarations: Record<string, unknown>;
  /** Extracted Query() calls with their positional args as AST nodes. */
  queryStatements: QueryStatementInfo[];
  /** Extracted Mutation() calls with their positional args as AST nodes. */
  mutationStatements: MutationStatementInfo[];
}
//#endregion
//#region src/parser/builtins.d.ts
/** Maps parser-level action step names → runtime step type values. Single source of truth. */
declare const ACTION_STEPS: {
  readonly Run: "run";
  readonly ToAssistant: "continue_conversation";
  readonly OpenUrl: "open_url";
  readonly Set: "set";
  readonly Reset: "reset";
};
declare const actionSchema: z.ZodUnion<readonly [z.ZodObject<{
  type: z.ZodLiteral<"open_url">;
  url: z.ZodString;
}, z.core.$strip>, z.ZodObject<{
  type: z.ZodLiteral<"continue_conversation">;
  context: z.ZodOptional<z.ZodString>;
}, z.core.$strip>, z.ZodObject<{
  type: z.ZodString;
  params: z.ZodOptional<z.ZodRecord<z.ZodString, z.ZodAny>>;
}, z.core.$strip>]>;
/**
 * Schema for a component's action prop: `action: action().optional()`. Tagged
 * `ActionExpression`, so the prompt teaches the @step syntax (one @step, or
 * Action([...]) for several). Its JSON schema describes the legacy `{ type, ... }`
 * action objects, which also still work. Returns the same schema on every call.
 */
declare function action(): z.ZodUnion<readonly [z.ZodObject<{
  type: z.ZodLiteral<"open_url">;
  url: z.ZodString;
}, z.core.$strip>, z.ZodObject<{
  type: z.ZodLiteral<"continue_conversation">;
  context: z.ZodOptional<z.ZodString>;
}, z.core.$strip>, z.ZodObject<{
  type: z.ZodString;
  params: z.ZodOptional<z.ZodRecord<z.ZodString, z.ZodAny>>;
}, z.core.$strip>]>;
/** A slot schema naming one action, typed like an action prop: `z.union([steps.OpenUrl.ref])`. */
type ActionRef = z.ZodType<z.infer<typeof actionSchema>>;
/** Refs of the built-in steps that have a legacy JSON shape, for restricted action slots. */
declare const steps: {
  OpenUrl: {
    ref: ActionRef;
  };
  ToAssistant: {
    ref: ActionRef;
  };
};
//#endregion
//#region src/functions.d.ts
/** defineFunction({name: "Percent", params, fn}) -> called as @Percent(done, total) */
interface DefinedFunction<T extends z$1.$ZodObject = z$1.$ZodObject, R extends z$1.$ZodType | undefined = z$1.$ZodType | undefined> {
  name: string;
  description: string;
  /** z.object({part, total}) -> @Percent(part, total), in key order */
  params: T;
  /** z.string() -> the return type in the prompt signature, checked at runtime */
  returns?: R;
  /** `@Percent(3, 4)` -> fn({part: 3, total: 4}); bad args or result, or a throw -> null */
  fn: (args: z$1.infer<T>) => R extends z$1.$ZodType ? z$1.infer<R> : unknown;
}
/** createLibrary({ functions: [defineFunction({name: "Percent", ...})] }) */
declare function defineFunction<T extends z$1.$ZodObject, R extends z$1.$ZodType | undefined = undefined>(config: DefinedFunction<T, R>): DefinedFunction<T, R>;
/** A library action step, delivered to `onAction` as `{ type: name, params }` on click. */
interface DefinedAction<T extends z$1.$ZodObject = z$1.$ZodObject, N extends string = string> {
  name: N;
  description: string;
  /** Parameters as a Zod object. Key order is the positional order in programs. */
  params: T;
  /** For a slot that takes only some actions: `z.union([copy.ref, steps.OpenUrl.ref])` */
  ref: ActionRef;
}
/** Define a library action for `createLibrary({ actions })`. */
declare function defineAction<const N extends string, T extends z$1.$ZodObject>(config: Omit<DefinedAction<T, N>, "ref">): DefinedAction<T, N>;
//#endregion
//#region src/parser/prompt.d.ts
/**
 * Tool schema for prompt generation — describes a tool the LLM can use via Query()/Mutation().
 * Shape inspired by MCP's tool schema (name, description, inputSchema, annotations).
 */
interface ToolSpec {
  name: string;
  description?: string;
  inputSchema: Record<string, unknown>;
  outputSchema: Record<string, unknown>;
  annotations?: {
    readOnlyHint?: boolean;
    destructiveHint?: boolean;
  };
}
interface ComponentPromptSpec {
  signature: string;
  description?: string;
}
interface ComponentGroup$1 {
  name: string;
  components: string[];
  notes?: string[];
}
interface BaseSpec {
  id?: string;
  root?: string;
  components: Record<string, ComponentPromptSpec>;
  componentGroups?: ComponentGroup$1[];
  /** Library functions, listed next to the built-ins. Signatures are pre-built. */
  functions?: Record<string, ComponentPromptSpec>;
  /** Custom library actions, listed with the built-in action steps. Signatures are pre-built. */
  actions?: Record<string, ComponentPromptSpec>;
}
interface PromptSpec extends BaseSpec {
  tools?: (string | ToolSpec)[];
  editMode?: boolean;
  inlineMode?: boolean;
  /** Enable Query(), Mutation(), @Run, tool workflow. Default: true if tools provided. */
  toolCalls?: boolean;
  /** Enable $variables, @Set, @Reset, interactive filters. Default: true if toolCalls. */
  bindings?: boolean;
  /** List the built-in functions (@Count, @Sum, ...). Default: true if toolCalls or bindings. */
  builtinFunctions?: boolean;
  preamble?: string;
  /** General examples (static/layout patterns). Both `examples` and `toolExamples` are included when present. */
  examples?: string[];
  /** Tool-specific examples (Query/Mutation patterns). Both `examples` and `toolExamples` are included when present. */
  toolExamples?: string[];
  additionalRules?: string[];
}
interface LibrarySpec extends BaseSpec {
  schema?: LibraryJSONSchema;
}
/** @deprecated Use {@link generateSystemPrompt}. */
declare function generatePrompt(spec: PromptSpec): string;
/** Prompt options for {@link generateSystemPrompt} */
type SystemPromptOptions = Omit<PromptSpec, keyof BaseSpec>;
/**
 * Prompt options allowed on the OpenUI Cloud wire. Extra flags (`tools`,
 * `editMode`, …) are stripped — Cloud's built-in prompt assembler ignores them.
 */
type CloudPromptOptions = Pick<SystemPromptOptions, "examples" | "preamble" | "additionalRules">;
/**
 * Object input for {@link generateSystemPrompt}.
 *
 * Pass `cloud: true` to emit OpenUI Cloud's managed `]]>openui:config` block
 * instead of a locally generated prompt. In that mode `library` is optional —
 * omit it to use Cloud's built-in chat library. `instructions` is Cloud-only
 * extra prose appended after the config block.
 */
type SystemPromptSpec = {
  library: LibrarySpec;
  promptOptions?: SystemPromptOptions;
  cloud?: false;
} | {
  cloud: true;
  library?: LibrarySpec;
  promptOptions?: CloudPromptOptions;
  instructions?: string;
};
/** Render the full system prompt for a library, or Cloud's managed config block when `cloud: true`. */
declare function generateSystemPrompt(spec: SystemPromptSpec): string;
/** @deprecated Pass `{ library, promptOptions }` instead. Removed at the next major. */
declare function generateSystemPrompt(spec: PromptSpec): string;
//#endregion
//#region src/signature.d.ts
/**
 * Tag a schema with an ID for prompt signatures.
 * Use for non-component schemas that need friendly type names (e.g. ActionExpression).
 * This affects prompt output only, not JSON Schema $defs.
 */
declare function tagSchemaId(schema: object, id: string): void;
interface FieldInfo {
  name: string;
  isOptional: boolean;
  isArray: boolean;
  typeAnnotation?: string;
}
declare function buildSignature(componentName: string, fields: FieldInfo[]): string;
//#endregion
//#region src/library.d.ts
/**
 * Runtime shape of a parsed sub-component element as seen by parent renderers.
 */
type SubComponentOf<P> = {
  type: "element";
  typeName: string;
  props: P;
  partial: boolean;
};
/**
 * The props passed to every component renderer.
 *
 * Framework adapters narrow `RenderNode`:
 * - React:  RenderNode = ReactNode
 * - Svelte: RenderNode = Snippet<[unknown]>
 * - Vue:    RenderNode = VNode
 */
interface ComponentRenderProps<P = Record<string, unknown>, RenderNode = unknown> {
  props: P;
  renderNode: (value: unknown) => RenderNode;
  /** The statement ID from the parsed program (e.g., "header", "prose1"). Undefined for inline components. */
  statementId?: string;
}
/**
 * A fully defined component. The `C` parameter represents the
 * framework-specific component/renderer type. lang-core never
 * inspects this value — it is stored opaquely and consumed
 * by the framework adapter's Renderer.
 */
interface DefinedComponent<T extends z$1.$ZodObject = z$1.$ZodObject, C = unknown> {
  name: string;
  props: T;
  description: string;
  component: C;
  /** Use in parent schemas: `z.array(ChildComponent.ref)` */
  ref: z$1.$ZodType<SubComponentOf<T extends z$1.$ZodType<infer O> ? O : any>>;
}
/**
 * Define a component with name, schema, description, and renderer.
 * Tags the schema with the component name so it resolves in prompt
 * signatures even if the component isn't in every library.
 */
declare function defineComponent<T extends z$1.$ZodObject, C>(config: {
  name: string;
  props: T;
  description: string;
  component: C;
}): DefinedComponent<T, C>;
interface ComponentGroup {
  name: string;
  components: string[];
  notes?: string[];
}
/** Tool descriptor for prompt generation — simple string or rich ToolSpec. */
type ToolDescriptor = string | ToolSpec;
interface PromptOptions {
  preamble?: string;
  additionalRules?: string[];
  /** Examples shown when no tools are present (static/layout patterns). */
  examples?: string[];
  /** Examples shown when tools ARE present (Query/Mutation patterns). Takes priority over `examples`. */
  toolExamples?: string[];
  /** Available tools for Query() — string names or rich ToolSpec descriptors injected into the prompt. */
  tools?: ToolDescriptor[];
  /** Enable edit-mode instructions in the prompt. */
  editMode?: boolean;
  /** Enable inline mode — LLM can respond with text + optional openui-lang fenced code. */
  inlineMode?: boolean;
  /** Enable Query(), Mutation(), @Run, tool workflow. Default: true if tools provided. */
  toolCalls?: boolean;
  /** Enable $variables, @Set, @Reset, interactive filters. Default: true if toolCalls. */
  bindings?: boolean;
  /** List the built-in functions (@Count, @Sum, ...). Default: true if toolCalls or bindings. */
  builtinFunctions?: boolean;
}
type AnyAction = DefinedAction<any, string>;
interface Library<C = unknown, A extends AnyAction = AnyAction> {
  readonly components: Record<string, DefinedComponent<any, C>>;
  readonly componentGroups: ComponentGroup[] | undefined;
  /** Library functions keyed by name; optional so hand-built libraries without them still type-check. */
  readonly functions?: Record<string, DefinedFunction<any, any>>;
  /** Custom actions keyed by name; optional like `functions`. */
  readonly actions?: { readonly [K in A as K["name"]]: K };
  readonly root: string | undefined;
  readonly id: string | undefined;
  /** Instance id minted by `createLibrary()`. Distinct from the optional public `id`. */
  readonly __libraryId: string;
  prompt(options?: PromptOptions): string;
  toSpec(): PromptSpec;
  toJSONSchema(): LibraryJSONSchema;
  /** Derive a new library (the base stays untouched); create it at module scope, parsers memoize by identity. */
  extend?<A2 extends AnyAction = never>(extension: LibraryExtension<C, A2>): Required<Library<C, A | A2>>;
}
interface LibraryExtension<C = unknown, A extends AnyAction = AnyAction> {
  components?: {
    /** New components; with `slots` ("Parent.prop", or "Parent" with one slot) they join those unions. */add?: (DefinedComponent<any, C> | {
      component: DefinedComponent<any, C>;
      slots: string[];
    })[]; /** Replacements (schema and renderer) by name. Every parent points at the new one. */
    override?: DefinedComponent<any, C>[];
    /**
     * Names to drop from the library, every parent union and componentGroups.
     * Throws when a prop would be left with no type, even an optional one
     * (`image?: Image` with Image removed): override that parent instead.
     */
    remove?: string[];
  };
  functions?: ExtensionVerbs<DefinedFunction<any, any>>;
  actions?: ExtensionVerbs<A>;
}
interface ExtensionVerbs<T> {
  /** Joins the library and is listed in the prompt. */
  add?: T[];
}
type LibraryAction<L> = L extends {
  readonly actions?: infer R;
} ? NonNullable<R>[keyof NonNullable<R>] : never;
type CustomActionEvent<A> = A extends DefinedAction<infer T, infer N> ? Omit<ActionEvent, "type" | "params"> & {
  type: N;
  params: z$1.infer<T>;
} : never;
/** The `onAction` event for library `L`: a union discriminated on `type` when it has custom actions. */
type LibraryActionEvent<L> = [LibraryAction<L>] extends [never] ? ActionEvent : string extends LibraryAction<L>["name"] ? ActionEvent : (Omit<ActionEvent, "type"> & {
  type: `${BuiltinActionType}` | "custom";
}) | CustomActionEvent<LibraryAction<L>>;
interface LibraryDefinition<C = unknown, A extends AnyAction = AnyAction> {
  components: DefinedComponent<any, C>[];
  componentGroups?: ComponentGroup[];
  /** Library functions, called as `@Name(...)` in programs. */
  functions?: DefinedFunction<any, any>[];
  /** Custom actions, used as `@Name(...)` steps in action props. */
  actions?: A[];
  root?: string;
  id?: string;
}
/**
 * Create a component library from an array of defined components.
 */
declare function createLibrary<C = unknown, A extends AnyAction = never>(input: LibraryDefinition<C, A>): Required<Library<C, A>>;
//#endregion
//#region src/message.d.ts
interface ParsedMessage {
  /** Body of the last `content` section, or the whole text when there is none. */
  content: string;
  /** Body of the `context` section: parsed JSON, else raw text. null when absent. */
  context: unknown;
  /** String attributes of the winning `content` marker. */
  attributes: Record<string, string>;
  /** An `end` line follows the winning `content` section. */
  end: boolean;
}
interface ParseMessageOptions {
  /** Hold back a last line that may be a marker cut mid-stream. */
  streaming?: boolean;
}
interface BuildMessageInput {
  content: string;
  /** Written as is when a string, else as JSON. Omitted when null or undefined. */
  context?: unknown;
  attributes?: Record<string, string>;
  end?: boolean;
}
/** Splits a stored message into its content, context, attributes and end flag. */
declare function parseMessage(raw: string, {
  streaming
}?: ParseMessageOptions): ParsedMessage;
/** Writes a stored message with its marker lines. */
declare function buildMessage({
  content,
  context,
  attributes,
  end
}: BuildMessageInput): string;
//#endregion
//#region src/parser/parser.d.ts
/**
 * Parse a complete openui-lang string in one pass.
 *
 * @param input  - Full openui-lang source text (may be partial/streaming)
 * @param cat    - Param map for positional-arg → named-prop mapping
 * @returns      ParseResult with root ElementNode (or null) and metadata
 */
declare function parse(input: string, cat: ParamMap, rootName?: string): ParseResult;
interface StreamParser {
  /** Feed the next SSE/stream chunk and get the latest ParseResult.
   *  Pass `complete` once the stream has ended. */
  push(chunk: string, complete?: boolean): ParseResult;
  /** Set the full text — diffs against internal buffer, pushes only the delta.
   *  Resets automatically if the text was replaced (not appended).
   *  Pass `complete` once the stream has ended. */
  set(fullText: string, complete?: boolean): ParseResult;
  /** Get the latest ParseResult without consuming new data. */
  getResult(complete?: boolean): ParseResult;
}
interface Parser {
  parse(input: string): ParseResult;
}
/**
 * Create a parser from a library JSON Schema document.
 * Pass `library.toJSONSchema()` to get the schema.
 *
 * @example
 * ```ts
 * const parser = createParser(library.toJSONSchema());
 * const result = parser.parse(openuiLangString);
 * ```
 */
declare function createParser(schema: LibraryJSONSchema, rootName?: string): Parser;
/**
 * Create a streaming parser from a library JSON Schema document.
 * Pass `library.toJSONSchema()` to get the schema.
 */
declare function createStreamingParser(schema: LibraryJSONSchema, rootName?: string): StreamParser;
//#endregion
//#region src/parser/enrich-errors.d.ts
/**
 * Convert parser ValidationErrors into enriched OpenUIErrors with hints.
 *
 * Framework-agnostic — usable by React, Svelte, Vue, or standalone.
 *
 * @deprecated ValidationError.message is already humanized and self-sufficient
 * (signatures, available components, expected types are inlined) — read
 * `result.meta.errors` directly. Will be removed in a future major release.
 */
declare function enrichErrors(validationErrors: ValidationError[], schema: LibraryJSONSchema, componentNames: string[]): OpenUIError[];
//#endregion
//#region src/parser/merge.d.ts
/**
 * Merge an existing program with a patch (partial update).
 * Patch statements override existing ones by name.
 * Unreachable statements are automatically garbage-collected.
 * Returns the merged program as a string.
 */
declare function mergeStatements(existing: string, patch: string, rootId?: string): string;
//#endregion
//#region src/parser/serialize.d.ts
interface SerializeOptions {
  /** $variable declarations to include (e.g. from ParseResult.stateDeclarations). */
  stateDeclarations?: Record<string, unknown>;
}
/**
 * Convert an ElementNode tree back to openui-lang source text.
 * Output is compatible with `mergeStatements()` for patching existing programs.
 *
 * @param json - The root ElementNode tree
 * @param library - The component Library (for positional arg ordering)
 * @param options - Optional state declarations
 * @returns openui-lang source text (multiple statements joined by newlines)
 */
declare function jsonToOpenUI(json: ElementNode, library: Library, options?: SerializeOptions): string;
//#endregion
//#region src/parser/tokens.d.ts
/**
 * Token type discriminant. Uses `const enum` for zero-cost at runtime
 * (TypeScript inlines the numeric values).
 */
declare const enum T$1 {
  Newline = 0,
  LParen = 1,
  // (
  RParen = 2,
  // )
  LBrack = 3,
  // [
  RBrack = 4,
  // ]
  LBrace = 5,
  // {
  RBrace = 6,
  // }
  Comma = 7,
  // ,
  Colon = 8,
  // :
  Equals = 9,
  // =
  True = 10,
  False = 11,
  Null = 12,
  EOF = 13,
  Str = 14,
  // carries string value
  Num = 15,
  // carries numeric value
  Ident = 16,
  // lowercase identifier — becomes a reference
  Type = 17,
  // PascalCase identifier — becomes a component name or reference
  StateVar = 18,
  // $identifier — reactive state reference
  Dot = 19,
  // .
  Plus = 20,
  // +
  Minus = 21,
  // -
  Star = 22,
  // *
  Slash = 23,
  // /
  Percent = 24,
  // %
  EqEq = 25,
  // ==
  NotEq = 26,
  // !=
  Greater = 27,
  // >
  Less = 28,
  // <
  GreaterEq = 29,
  // >=
  LessEq = 30,
  // <=
  And = 31,
  // &&
  Or = 32,
  // ||
  Not = 33,
  // !
  Question = 34,
  // ?
  BuiltinCall = 35
}
type Token = {
  t: T$1;
  v?: string | number;
};
//#endregion
//#region src/parser/expressions.d.ts
/**
 * Parse a token array into an AST node using a Pratt (top-down operator
 * precedence) parser.
 */
declare function parseExpression(tokens: Token[]): ASTNode;
//#endregion
//#region src/parser/lexer.d.ts
/**
 * Tokenize an openui-lang source string into a flat token array.
 *
 * Handles all token types: identifiers, literals, operators,
 * state variables ($name), dot access, ternary.
 */
declare function tokenize(src: string): Token[];
//#endregion
//#region src/parser/statements.d.ts
interface RawStmt {
  id: string;
  /** Token type of the LHS identifier — used to classify statement kind */
  idTokenType: T$1;
  tokens: Token[];
}
/**
 * Auto-close unclosed strings and brackets so that partial/streaming input
 * can be parsed without syntax errors.
 */
declare function autoClose(input: string): {
  text: string;
  wasIncomplete: boolean;
};
/**
 * Splits the flat token stream into individual statements.
 *
 * Each statement has the form `identifier = expression`. Statements are
 * separated by newlines at depth 0 (newlines inside brackets are ignored).
 *
 * Accepts `Ident`, `Type`, and `StateVar` as statement identifiers.
 * For StateVar, the id is the full token value including $ (e.g., "$count").
 *
 * Invalid lines (no `=`, or no identifier) are silently skipped.
 */
declare function split(tokens: Token[]): RawStmt[];
//#endregion
//#region src/reactive.d.ts
/** Mark a schema as reactive. Called by framework adapters' reactive() function. */
declare function markReactive(schema: object): void;
/** Check if a schema was marked reactive. Used by Zod introspection for $binding<> prefix. */
declare function isReactiveSchema(schema: unknown): boolean;
//#endregion
//#region src/runtime/evaluator.d.ts
/** Optional schema context for reactive-aware evaluation. */
interface SchemaContext {
  /** Component library — used to look up reactive schemas per prop. */
  library: {
    components: Record<string, {
      props: {
        shape?: Record<string, unknown>;
      };
    }>;
  };
}
interface EvaluationContext {
  /** Read $variable from the store */
  getState(name: string): unknown;
  /** Resolve a reference to another declaration's evaluated value */
  resolveRef(name: string): unknown;
  /** Extra scope for $value injection during reactive prop evaluation */
  extraScope?: Record<string, unknown>;
  /** Library functions: `library.toJSONSchema().functions` entries plus each `fn`. */
  functions?: Record<string, RuntimeFunction>;
  /** Custom actions: the `library.toJSONSchema().actions` entries. */
  actions?: Record<string, FunctionSchema>;
  /** Reports a runtime error, such as a library function call that evaluated to null. */
  reportError?: (message: string) => void;
}
/** A library function as the evaluator runs it. */
interface RuntimeFunction extends FunctionSchema {
  fn: (args: any) => unknown;
}
interface ReactiveAssign {
  __reactive: "assign";
  target: string;
  expr: ASTNode;
}
declare function isReactiveAssign(value: unknown): value is ReactiveAssign;
/**
 * Evaluate an AST node to a runtime value.
 */
declare function evaluate(node: ASTNode, context: EvaluationContext, schemaCtx?: SchemaContext): unknown;
/**
 * Strip a ReactiveAssign to its current value in a non-reactive context.
 * When transport args or non-reactive props contain a ReactiveAssign, this
 * resolves it to the current state value (or null if getState is unavailable).
 */
declare function stripReactiveAssign(value: unknown, context: EvaluationContext): unknown;
//#endregion
//#region src/runtime/store.d.ts
interface Store {
  get(name: string): unknown;
  /** `pristine: true` restores a declared default that a later parse may still replace. */
  set(name: string, value: unknown, options?: {
    pristine?: boolean;
  }): void;
  subscribe(listener: () => void): () => void;
  getSnapshot(): Record<string, unknown>;
  initialize(defaults: Record<string, unknown>, persisted: Record<string, unknown>): void;
  dispose(): void;
}
declare function createStore(): Store;
//#endregion
//#region src/runtime/evaluate-tree.d.ts
/** Context passed through the evaluation chain — no module-level state. */
interface EvalContext {
  /** AST evaluation context (getState, resolveRef) */
  ctx: EvaluationContext;
  /** Component library for reactive schema lookup */
  library: Library;
  /** Reactive binding store (null in v1 mode) */
  store: Store | null;
  /** Runtime errors collected during prop evaluation (optional — populated by evaluateElementProps). */
  errors?: OpenUIError[];
}
/**
 * Evaluate all AST nodes in an ElementNode tree's props.
 * Returns a new ElementNode with all props resolved to concrete values.
 *
 * Uses the unified evaluator with schema context for reactive-aware evaluation.
 */
declare function evaluateElementProps(el: ElementNode, evalCtx: EvalContext): ElementNode;
/** Evaluate `root`, or the component a runtime entry (`rootExpression`) picks; null if none. */
declare function evaluateRoot(result: Pick<ParseResult, "root" | "rootExpression">, evalCtx: EvalContext): ElementNode | null;
//#endregion
//#region src/runtime/mcp.d.ts
/**
 * MCP utilities — type definitions and result extraction for MCP client integration.
 *
 * The Renderer accepts an MCP client directly as `toolProvider`.
 * It detects the MCP client shape (has `callTool({ name, arguments })`) and
 * wraps responses with `extractToolResult` automatically.
 *
 * @example
 * ```tsx
 * import { Client } from "@modelcontextprotocol/sdk/client/index.js";
 * import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
 *
 * const client = new Client({ name: "my-app", version: "1.0.0" });
 * await client.connect(new StreamableHTTPClientTransport(new URL("/api/mcp")));
 *
 * // Pass directly — Renderer handles MCP response extraction
 * <Renderer toolProvider={client} library={library} response={content} />
 * ```
 */
/**
 * Error thrown when an MCP tool call returns `isError: true`.
 * Preserves the raw error content from the MCP response for structured handling.
 */
declare class McpToolError extends Error {
  readonly toolErrorText: string;
  constructor(errorText: string);
}
/**
 * Minimal shape of an MCP Client — matches @modelcontextprotocol/sdk Client
 * without requiring it as a hard import. Users can pass any object that
 * implements these methods.
 */
interface McpClientLike {
  callTool(params: {
    name: string;
    arguments?: Record<string, unknown>;
  }, options?: unknown): Promise<{
    content: Array<{
      type: string;
      text?: string;
      [key: string]: unknown;
    }>;
    structuredContent?: unknown;
    isError?: boolean;
  }>;
  close?(): Promise<void>;
}
/**
 * Extract the actual data from an MCP callTool result.
 * Prefers structuredContent (machine-readable JSON), falls back to parsing text content.
 */
declare function extractToolResult(result: {
  content: Array<{
    type: string;
    text?: string;
    [key: string]: unknown;
  }>;
  structuredContent?: unknown;
  isError?: boolean;
}): unknown;
//#endregion
//#region src/runtime/queryManager.d.ts
/**
 * ToolProvider interface for Query() and Mutation() tool calls.
 * Framework-agnostic — works with MCP, REST, GraphQL, or any backend.
 *
 * @example
 * ```ts
 * // Function map (Renderer normalizes this automatically)
 * <Renderer toolProvider={{
 *   get_users: (args) => fetch(`/api/users`).then(r => r.json()),
 * }} />
 *
 * // MCP client (Renderer wraps extractToolResult automatically)
 * <Renderer toolProvider={mcpClient} />
 * ```
 */
interface ToolProvider {
  callTool(toolName: string, args: Record<string, unknown>): Promise<unknown>;
}
interface QueryNode {
  statementId: string;
  toolName: string;
  args: unknown;
  defaults: unknown;
  /** Evaluated dependency value — included in cache key to force re-fetch on change. */
  deps: unknown;
  /** Auto-refresh interval in seconds. */
  refreshInterval?: number;
  complete: boolean;
}
interface MutationNode {
  statementId: string;
  toolName: string;
}
interface MutationResult {
  status: "idle" | "loading" | "success" | "error";
  data?: unknown;
  error?: unknown;
}
interface QuerySnapshot extends Record<string, unknown> {
  __openui_loading: string[];
  __openui_refetching: string[];
  __openui_errors: OpenUIError[];
}
interface QueryManager {
  evaluateQueries(queryNodes: QueryNode[]): void;
  getResult(statementId: string): unknown;
  isLoading(statementId: string): boolean;
  isAnyLoading(): boolean;
  invalidate(statementIds?: string[]): void;
  registerMutations(nodes: MutationNode[]): void;
  fireMutation(statementId: string, evaluatedArgs: Record<string, unknown>, refreshQueryIds?: string[]): Promise<boolean>;
  getMutationResult(statementId: string): MutationResult | null;
  subscribe(listener: () => void): () => void;
  getSnapshot(): QuerySnapshot;
  activate(): void;
  dispose(): void;
}
declare function createQueryManager(toolProvider: ToolProvider | null): QueryManager;
//#endregion
//#region src/runtime/state-field.d.ts
interface StateField<T = unknown> {
  name: string;
  value: T;
  setValue: (newValue: T) => void;
  isReactive: boolean;
}
type InferStateFieldValue<T> = T extends StateField<infer U> ? U : T;
declare function resolveStateField<T = unknown>(name: string, bindingValue: unknown, store: Store | null, evaluationContext: EvaluationContext | null, fieldGetter: (fieldName: string) => unknown, fieldSetter: (fieldName: string, value: unknown) => void): StateField<T>;
//#endregion
//#region src/runtime/toolProvider.d.ts
/**
 * Standard error thrown when a tool name is not found in a function-map ToolProvider.
 * Used by Renderer's inline normalization to give clear error messages.
 */
declare class ToolNotFoundError extends Error {
  readonly toolName: string;
  readonly availableTools: string[];
  constructor(toolName: string, availableTools?: string[]);
}
//#endregion
//#region src/utils/validation.d.ts
interface ParsedRule {
  type: string;
  arg?: number | string;
}
declare function parseRules(rules: unknown): ParsedRule[];
type ValidatorFn = (value: unknown, arg?: number | string) => string | undefined;
declare const builtInValidators: Record<string, ValidatorFn>;
/**
 * Run all rules against a value. Stop on first error.
 * Custom validators are checked first, then built-in ones.
 */
declare function validate(value: unknown, rules: ParsedRule[], customValidators?: Record<string, ValidatorFn>): string | undefined;
/**
 * Parse a structured rules object into ParsedRule[].
 * Accepts: { required: true, minLength: 5, email: true, max: 100 }
 * Skips keys with false/undefined values.
 */
declare function parseStructuredRules(rules: unknown): ParsedRule[];
//#endregion
export { ACTION_STEPS, type ASTNode, type ActionEvent, type ActionPlan, type ActionRef, type ActionStep, type BuildMessageInput, BuiltinActionType, type CloudPromptOptions, type ComponentGroup, type ComponentPromptSpec, type ComponentRenderProps, type DefinedAction, type DefinedComponent, type DefinedFunction, type ElementNode, type EvalContext, type EvaluationContext, type InferStateFieldValue, type Library, type LibraryActionEvent, type LibraryDefinition, type LibraryExtension, type LibraryJSONSchema, type LibrarySpec, type McpClientLike, McpToolError, type MutationNode, type MutationResult, type MutationStatementInfo, type OpenUIError, type OpenUIErrorCode, type OpenUIErrorSource, type ParseMessageOptions, type ParseResult, type ParsedMessage, type ParsedRule, type Parser, type PromptOptions, type PromptSpec, type QueryManager, type QueryNode, type QuerySnapshot, type QueryStatementInfo, type ReactiveAssign, type SerializeOptions, type StateField, type Store, type StreamParser, type SubComponentOf, type SystemPromptOptions, type SystemPromptSpec, type ToolDescriptor, ToolNotFoundError, type ToolProvider, type ToolSpec, type ValidationError, type ValidationErrorCode, type ValidatorFn, action, autoClose, buildMessage, buildSignature, builtInValidators, createLibrary, createParser, createQueryManager, createStore, createStreamingParser, defineAction, defineComponent, defineFunction, enrichErrors, evaluate, evaluateElementProps, evaluateRoot, extractToolResult, generatePrompt, generateSystemPrompt, isReactiveAssign, isReactiveSchema, jsonToOpenUI, markReactive, mergeStatements, parse, parseExpression, parseMessage, parseRules, parseStructuredRules, resolveStateField, split, steps, stripReactiveAssign, tagSchemaId, tokenize, validate, walkAST };
//# sourceMappingURL=index.d.mts.map