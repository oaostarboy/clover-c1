import { i as isRuntimeTelemetryEnabled, n as TELEMETRY_REQUEST_TIMEOUT_MS, r as getPostHogConfig, s as normalizeProjectIdentity, t as PROJECT_ID_SALT } from "./shared-tgSjXCMf.mjs";
import { literal, object, z } from "zod/v4";
import * as z$1 from "zod/v4/core";
//#region src/reactive.ts
/** WeakSet tracks reactive schemas without mutating the schema objects. */
const reactiveSchemas = /* @__PURE__ */ new WeakSet();
/** Mark a schema as reactive. Called by framework adapters' reactive() function. */
function markReactive(schema) {
	reactiveSchemas.add(schema);
}
/** Check if a schema was marked reactive. Used by Zod introspection for $binding<> prefix. */
function isReactiveSchema(schema) {
	return typeof schema === "object" && schema !== null && reactiveSchemas.has(schema);
}
//#endregion
//#region src/signature.ts
const schemaIdTags = /* @__PURE__ */ new WeakMap();
/**
* Tag a schema with an ID for prompt signatures.
* Use for non-component schemas that need friendly type names (e.g. ActionExpression).
* This affects prompt output only, not JSON Schema $defs.
*/
function tagSchemaId(schema, id) {
	schemaIdTags.set(schema, id);
}
const actionRefTags = /* @__PURE__ */ new WeakMap();
function actionRef(name, data) {
	actionRefTags.set(data, name);
	return data;
}
function copySchemaTags(from, to) {
	const id = schemaIdTags.get(from);
	if (id) schemaIdTags.set(to, id);
	const action = actionRefTags.get(from);
	if (action) actionRefTags.set(to, action);
}
function assertV4Schema(schema, componentName) {
	if (schema != null && typeof schema === "object" && "_def" in schema && !("_zod" in schema)) throw new Error(`[OpenUI] Component "${componentName}" was defined with a Zod 3 schema. OpenUI requires Zod 4 schemas. If you're on zod@3.25+, import from "zod/v4" instead of "zod". See: https://zod.dev/v4/versioning`);
}
function getZodDef(schema) {
	return schema?._zod?.def;
}
function getZodType(schema) {
	return getZodDef(schema)?.type;
}
function isOptionalType(schema) {
	const type = getZodType(schema);
	return type === "optional" || type === "default" || type === "nullable";
}
function unwrap(schema) {
	let s = schema;
	let def = getZodDef(s);
	while (def?.type === "optional" || def?.type === "default" || def?.type === "nullable") {
		s = def.innerType;
		def = getZodDef(s);
	}
	return s;
}
function isArrayType(schema) {
	return getZodType(unwrap(schema)) === "array";
}
function getArrayInnerType(schema) {
	const def = getZodDef(unwrap(schema));
	if (def?.type === "array") return def.element ?? def.innerType;
}
function getEnumValues(schema) {
	const def = getZodDef(unwrap(schema));
	if (def?.type !== "enum") return void 0;
	if (Array.isArray(def.values)) return def.values;
	if (def.entries && typeof def.entries === "object") return Object.keys(def.entries);
}
function getSchemaId(schema, reg) {
	try {
		const meta = reg?.get(schema);
		if (meta?.id) return meta.id;
	} catch {}
	if (typeof schema === "object" && schema !== null) return schemaIdTags.get(schema);
}
function getUnionOptions(schema) {
	const def = getZodDef(schema);
	if (def?.type === "union" && Array.isArray(def.options)) return def.options;
}
function getObjectShape(schema) {
	const def = getZodDef(schema);
	if (def?.type === "object" && def.shape && typeof def.shape === "object") return def.shape;
}
/**
* Resolve the type annotation for a schema field.
* Returns a human-readable type string for the schema.
* If the schema is marked reactive(), prefixes with "$binding<...>".
*/
function resolveTypeAnnotation(schema, reg) {
	const isReactive = isReactiveSchema(schema);
	const baseType = resolveBaseType(unwrap(schema), reg);
	if (!baseType) return void 0;
	return isReactive ? `$binding<${baseType}>` : baseType;
}
function resolveBaseType(inner, reg) {
	const actionRef = typeof inner === "object" && inner !== null && actionRefTags.get(inner);
	if (actionRef) return `@${actionRef}`;
	const directId = getSchemaId(inner, reg);
	if (directId) return directId;
	const unionOpts = getUnionOptions(inner);
	if (unionOpts) {
		const names = unionOpts.map((o) => resolveTypeAnnotation(o, reg)).filter(Boolean);
		if (names.length > 0) return names.join(" | ");
	}
	if (isArrayType(inner)) {
		const arrayInner = getArrayInnerType(inner);
		if (!arrayInner) return void 0;
		const innerType = resolveTypeAnnotation(arrayInner, reg);
		if (innerType) return getUnionOptions(unwrap(arrayInner)) !== void 0 ? `(${innerType})[]` : `${innerType}[]`;
		return;
	}
	const zodType = getZodType(inner);
	if (zodType === "string") return "string";
	if (zodType === "number") return "number";
	if (zodType === "boolean") return "boolean";
	if (zodType === "any") return "any";
	if (zodType === "record") {
		const def = getZodDef(inner);
		return `Record<${resolveTypeAnnotation(def?.keyType, reg) ?? "string"}, ${resolveTypeAnnotation(def?.valueType, reg) ?? "any"}>`;
	}
	const enumVals = getEnumValues(inner);
	if (enumVals) return enumVals.map((v) => `"${v}"`).join(" | ");
	if (zodType === "literal") {
		const vals = getZodDef(inner)?.values;
		if (Array.isArray(vals) && vals.length === 1) {
			const v = vals[0];
			return typeof v === "string" ? `"${v}"` : String(v);
		}
	}
	const shape = getObjectShape(inner);
	if (shape) return `{${Object.entries(shape).map(([name, fieldSchema]) => {
		const opt = isOptionalType(fieldSchema) ? "?" : "";
		const fieldType = resolveTypeAnnotation(fieldSchema, reg);
		return fieldType ? `${name}${opt}: ${fieldType}` : `${name}${opt}`;
	}).join(", ")}}`;
	return "any";
}
function analyzeFields(shape, reg) {
	return Object.entries(shape).map(([name, schema]) => ({
		name,
		isOptional: isOptionalType(schema),
		isArray: isArrayType(schema),
		typeAnnotation: resolveTypeAnnotation(schema, reg)
	}));
}
function buildSignature(componentName, fields) {
	return `${componentName}(${fields.map((f) => {
		if (f.typeAnnotation) return f.isOptional ? `${f.name}?: ${f.typeAnnotation}` : `${f.name}: ${f.typeAnnotation}`;
		if (f.isArray) return f.isOptional ? `[${f.name}]?` : `[${f.name}]`;
		return f.isOptional ? `${f.name}?` : f.name;
	}).join(", ")})`;
}
/**
* Prompt signature of a component, function or action: `Name(a: T, b?: U)`,
* plus ` → R` when it declares a return type. Key order is the positional order.
*/
function schemaSignature(name, params, returns, reg) {
	const signature = buildSignature(name, analyzeFields(params._zod.def.shape, reg));
	const type = returns && resolveTypeAnnotation(returns, reg);
	return type ? `${signature} → ${type}` : signature;
}
//#endregion
//#region src/extend.ts
const fail = (message) => {
	throw new Error(`[extend] ${message}`);
};
function extendDefinition(base, ext) {
	const changes = applyComponentVerbs(base, ext.components);
	const placements = placeInSlots(changes, findContentSlots(changes));
	return {
		...base,
		id: void 0,
		components: rebuildComponents(changes, placements),
		componentGroups: withoutRemoved(base.componentGroups, changes.removed),
		functions: [...base.functions ?? [], ...ext.functions?.add ?? []],
		actions: [...base.actions ?? [], ...ext.actions?.add ?? []]
	};
}
function applyComponentVerbs(base, verbs = {}) {
	const byName = new Map(base.components.map((c) => [c.name, c]));
	const nameOf = new Map(base.components.map((c) => [c.props, c.name]));
	const slotRequests = /* @__PURE__ */ new Map();
	const touched = /* @__PURE__ */ new Set();
	const put = (comp) => {
		byName.set(comp.name, comp);
		nameOf.set(comp.props, comp.name);
		touched.add(comp.name);
	};
	for (const entry of verbs.add ?? []) {
		const { component, slots } = "slots" in entry ? entry : {
			component: entry,
			slots: void 0
		};
		const name = component.name;
		if (byName.has(name)) fail(`Component "${name}" already exists. Use override.`);
		if (slots?.length === 0) fail(`"slots" for "${name}" is empty. Omit it to only list it.`);
		put(component);
		if (slots) slotRequests.set(name, slots);
	}
	const overridden = /* @__PURE__ */ new Set();
	for (const comp of verbs.override ?? []) {
		if (!byName.has(comp.name)) fail(`Cannot override "${comp.name}": not in the library. Use add.`);
		if (overridden.has(comp.name)) fail(`Component "${comp.name}" is overridden twice.`);
		overridden.add(comp.name);
		put(comp);
	}
	const removed = new Set(verbs.remove);
	for (const name of removed) {
		if (touched.has(name)) fail(`Cannot remove "${name}": it is added or overridden in the same call.`);
		if (!byName.delete(name)) fail(`Cannot remove "${name}": not in the library.`);
		if (name === base.root) fail(`Cannot remove "${name}": it is the library root.`);
	}
	return {
		byName,
		nameOf,
		slotRequests,
		removed
	};
}
function slotUnion(schema, nameOf) {
	const inner = unwrap(schema);
	const def = getZodDef(inner);
	if (def?.type === "array") return slotUnion(def.element, nameOf);
	return getUnionOptions(inner)?.some((o) => nameOf.has(o)) ? inner : void 0;
}
function findContentSlots({ byName, nameOf }) {
	const slots = [];
	for (const comp of byName.values()) for (const [prop, schema] of Object.entries(comp.props.shape)) if (slotUnion(schema, nameOf)) slots.push(`${comp.name}.${prop}`);
	return slots;
}
function placeInSlots(changes, slots) {
	const placements = /* @__PURE__ */ new Map();
	for (const [name, requested] of changes.slotRequests) for (const slot of requested) {
		const path = resolveSlot(slot, name, slots, changes.byName);
		const placed = placements.get(path) ?? [];
		if (!placed.includes(name)) placements.set(path, [...placed, name]);
	}
	return placements;
}
function resolveSlot(slot, name, slots, byName) {
	if (slots.includes(slot)) return slot;
	const parent = slot.split(".")[0];
	const candidates = slots.filter((s) => s.startsWith(`${parent}.`));
	if (!slot.includes(".") && candidates.length === 1) return candidates[0];
	const what = `Slot "${slot}" for "${name}"`;
	if (!byName.has(parent)) fail(`${what}: "${parent}" is not in the library.`);
	if (!candidates.length) fail(`${what}: "${parent}" has no content slot (a prop that is a union of components, or an optional or array of one).`);
	return fail(`${what} must be one content slot. Candidates: ${candidates.join(", ")}.`);
}
function rebuildComponents(changes, placements) {
	const { byName, nameOf, removed } = changes;
	const final = new Map(byName);
	const shapes = /* @__PURE__ */ new Map();
	for (const name of findRebuilt(changes, placements)) {
		const comp = byName.get(name);
		const shape = {};
		const props = cloneSchema(comp.props, { get shape() {
			return shape;
		} });
		final.set(name, {
			...comp,
			props,
			ref: props
		});
		shapes.set(name, shape);
	}
	const finalProps = (name) => final.get(name).props;
	const onlyType = (name, slot) => fail(`Cannot remove "${name}": it is the only type allowed in ${slot.path}.`);
	const rebind = (schema, slot, memo) => {
		const name = nameOf.get(schema);
		if (name !== void 0) {
			if (removed.has(name)) onlyType(name, slot);
			return finalProps(name);
		}
		const def = getZodDef(schema);
		if (!def || def.type === "lazy") return schema;
		if (memo.has(schema)) return memo.get(schema);
		memo.set(schema, schema);
		const walk = (child) => rebind(child, slot, memo);
		const patch = {};
		const options = getUnionOptions(schema);
		const holdsComponents = options?.some((o) => nameOf.has(o));
		for (const [key, value] of Object.entries(def)) {
			if (key === "checks" || holdsComponents && key === "options") continue;
			const next = rebindField(key, value, walk);
			if (!sameField(next, value)) patch[key] = next;
		}
		if (holdsComponents) {
			const kept = options.filter((o) => !removed.has(nameOf.get(o) ?? "")).map(walk);
			const added = schema === slot.union ? slot.added.filter((a) => !kept.includes(a)) : [];
			if (!kept.length && !added.length) onlyType(nameOf.get(options[0]), slot);
			const next = [...kept, ...added];
			if (!sameField(next, options)) patch.options = next;
		}
		const out = Object.keys(patch).length ? cloneSchema(schema, patch) : schema;
		memo.set(schema, out);
		return out;
	};
	for (const [name, shape] of shapes) for (const [prop, schema] of Object.entries(byName.get(name).props.shape)) {
		const path = `${name}.${prop}`;
		const added = (placements.get(path) ?? []).map(finalProps);
		shape[prop] = rebind(schema, {
			path,
			union: slotUnion(schema, nameOf),
			added
		}, /* @__PURE__ */ new Map());
	}
	return [...final.values()];
}
function findRebuilt({ byName, nameOf, removed }, placements) {
	const nested = /* @__PURE__ */ new Map();
	for (const [name, comp] of byName) {
		const found = /* @__PURE__ */ new Set();
		for (const schema of Object.values(comp.props.shape)) nestedComponents(schema, nameOf, found);
		nested.set(name, found);
	}
	const stale = (s) => {
		const name = nameOf.get(s);
		return removed.has(name) || byName.get(name).props !== s;
	};
	const slotted = new Set([...placements.keys()].map((path) => path.split(".")[0]));
	const rebuilt = new Set([...byName.keys()].filter((n) => slotted.has(n)));
	for (const [name, found] of nested) if ([...found].some(stale)) rebuilt.add(name);
	for (let grew = true; grew;) {
		grew = false;
		for (const [name, found] of nested) {
			if (rebuilt.has(name) || ![...found].some((s) => rebuilt.has(nameOf.get(s)))) continue;
			rebuilt.add(name);
			grew = true;
		}
	}
	return rebuilt;
}
function nestedComponents(schema, nameOf, found, seen = /* @__PURE__ */ new Set()) {
	if (nameOf.has(schema)) return void found.add(schema);
	const def = getZodDef(schema);
	if (!def || def.type === "lazy" || seen.has(schema)) return;
	seen.add(schema);
	for (const [key, value] of Object.entries(def)) {
		if (key === "checks") continue;
		rebindField(key, value, (child) => nestedComponents(child, nameOf, found, seen));
	}
}
function rebindField(key, value, walk) {
	if (getZodDef(value)) return walk(value);
	if (Array.isArray(value)) return value.map(walk);
	if (key !== "shape") return value;
	const shape = {};
	for (const [prop, child] of Object.entries(value)) shape[prop] = walk(child);
	return shape;
}
function sameField(next, old) {
	if (next === old) return true;
	if (getZodDef(next)) return false;
	const entries = Object.entries(next);
	return entries.length === Object.keys(old).length && entries.every(([i, c]) => c === old[i]);
}
function cloneSchema(s, patch) {
	const schema = s;
	const copy = z$1.clone(schema, z$1.util.mergeDefs(schema._zod.def, patch));
	copySchemaTags(schema, copy);
	if (isReactiveSchema(schema)) markReactive(copy);
	const meta = z$1.globalRegistry.get(schema);
	if (meta) z$1.globalRegistry.add(copy, meta);
	return copy;
}
function withoutRemoved(groups, removed) {
	return groups?.map((g) => ({
		...g,
		components: g.components.filter((n) => !removed.has(n))
	})).filter((g) => g.components.length);
}
//#endregion
//#region src/functions.ts
function assertParamsObject(params, name) {
	assertV4Schema(params, name);
	if (params?._zod?.def.type !== "object") throw new Error(`[OpenUI] "${name}" params must be a z.object().`);
}
/** createLibrary({ functions: [defineFunction({name: "Percent", ...})] }) */
function defineFunction(config) {
	assertParamsObject(config.params, config.name);
	if (config.returns) assertV4Schema(config.returns, config.name);
	return config;
}
/** Define a library action for `createLibrary({ actions })`. */
function defineAction(config) {
	assertParamsObject(config.params, config.name);
	const data = object({
		type: literal(config.name),
		params: config.params
	});
	return {
		...config,
		ref: actionRef(config.name, data)
	};
}
//#endregion
//#region src/parser/builtins.ts
/** Built-in functions and action steps (defined like library ones), and reserved calls. */
/** Resolve a field path on an object. Supports dot-paths: "state.name" → obj.state.name */
function resolveField(obj, path) {
	if (!path || obj == null) return void 0;
	if (!path.includes(".")) return obj[path];
	let cur = obj;
	for (const p of path.split(".")) {
		if (cur == null) return void 0;
		cur = cur[p];
	}
	return cur;
}
function toNumber(val) {
	if (typeof val === "number") return val;
	if (typeof val === "string") {
		const n = Number(val);
		return isNaN(n) ? 0 : n;
	}
	if (typeof val === "boolean") return val ? 1 : 0;
	return 0;
}
const sumOf = (arr) => arr.reduce((a, b) => a + toNumber(b), 0);
const array = () => z.array(z.any());
const numbers = () => z.object({ numbers: z.array(z.number()) });
const number = () => z.object({ number: z.number() });
const builtins = [
	defineFunction({
		name: "Count",
		description: "Returns array length",
		params: z.object({ array: array() }),
		returns: z.number(),
		fn: ({ array }) => Array.isArray(array) ? array.length : 0
	}),
	defineFunction({
		name: "First",
		description: "Returns first element of array",
		params: z.object({ array: array() }),
		returns: z.any(),
		fn: ({ array }) => Array.isArray(array) ? array[0] ?? null : null
	}),
	defineFunction({
		name: "Last",
		description: "Returns last element of array",
		params: z.object({ array: array() }),
		returns: z.any(),
		fn: ({ array }) => Array.isArray(array) ? array[array.length - 1] ?? null : null
	}),
	defineFunction({
		name: "Sum",
		description: "Sum of numeric array",
		params: numbers(),
		returns: z.number(),
		fn: ({ numbers }) => Array.isArray(numbers) ? sumOf(numbers) : 0
	}),
	defineFunction({
		name: "Avg",
		description: "Average of numeric array",
		params: numbers(),
		returns: z.number(),
		fn: ({ numbers }) => Array.isArray(numbers) && numbers.length ? sumOf(numbers) / numbers.length : 0
	}),
	defineFunction({
		name: "Min",
		description: "Minimum value in array",
		params: numbers(),
		returns: z.number(),
		fn: ({ numbers }) => Array.isArray(numbers) && numbers.length ? numbers.reduce((acc, b) => Math.min(acc, toNumber(b)), toNumber(numbers[0])) : 0
	}),
	defineFunction({
		name: "Max",
		description: "Maximum value in array",
		params: numbers(),
		returns: z.number(),
		fn: ({ numbers }) => Array.isArray(numbers) && numbers.length ? numbers.reduce((acc, b) => Math.max(acc, toNumber(b)), toNumber(numbers[0])) : 0
	}),
	defineFunction({
		name: "Sort",
		description: "Sort array by field. Direction: \"asc\" (default) or \"desc\"",
		params: z.object({
			array: array(),
			field: z.string(),
			direction: z.enum(["asc", "desc"]).optional()
		}),
		returns: array(),
		fn: ({ array, field, direction }) => {
			if (!Array.isArray(array)) return array;
			const f = String(field ?? "");
			const desc = String(direction ?? "asc") === "desc";
			return [...array].sort((a, b) => {
				const av = f ? resolveField(a, f) : a;
				const bv = f ? resolveField(b, f) : b;
				const aIsNumeric = typeof av === "number" || typeof av === "string" && !isNaN(Number(av)) && av !== "";
				const bIsNumeric = typeof bv === "number" || typeof bv === "string" && !isNaN(Number(bv)) && bv !== "";
				if (aIsNumeric && bIsNumeric) {
					const diff = toNumber(av) - toNumber(bv);
					return desc ? -diff : diff;
				}
				const cmp = String(av ?? "").localeCompare(String(bv ?? ""));
				return desc ? -cmp : cmp;
			});
		}
	}),
	defineFunction({
		name: "Filter",
		description: "Filter array by field value",
		params: z.object({
			array: array(),
			field: z.string(),
			operator: z.enum([
				"==",
				"!=",
				">",
				"<",
				">=",
				"<=",
				"contains"
			]),
			value: z.any()
		}),
		returns: array(),
		fn: ({ array, field, operator, value }) => {
			if (!Array.isArray(array)) return [];
			const f = String(field ?? "");
			const o = String(operator ?? "==");
			return array.filter((item) => {
				const v = f ? resolveField(item, f) : item;
				switch (o) {
					case "==": return v == value;
					case "!=": return v != value;
					case ">": return toNumber(v) > toNumber(value);
					case "<": return toNumber(v) < toNumber(value);
					case ">=": return toNumber(v) >= toNumber(value);
					case "<=": return toNumber(v) <= toNumber(value);
					case "contains": return String(v ?? "").includes(String(value ?? ""));
					default: return false;
				}
			});
		}
	}),
	defineFunction({
		name: "Round",
		description: "Round to N decimal places (default 0)",
		params: z.object({
			number: z.number(),
			decimals: z.number().optional()
		}),
		returns: z.number(),
		fn: ({ number, decimals }) => {
			const num = toNumber(number);
			const d = decimals != null ? toNumber(decimals) : 0;
			const factor = Math.pow(10, d);
			return Math.round(num * factor) / factor;
		}
	}),
	defineFunction({
		name: "Abs",
		description: "Absolute value",
		params: number(),
		returns: z.number(),
		fn: ({ number }) => Math.abs(toNumber(number))
	}),
	defineFunction({
		name: "Floor",
		description: "Round down to nearest integer",
		params: number(),
		returns: z.number(),
		fn: ({ number }) => Math.floor(toNumber(number))
	}),
	defineFunction({
		name: "Ceil",
		description: "Round up to nearest integer",
		params: number(),
		returns: z.number(),
		fn: ({ number }) => Math.ceil(toNumber(number))
	}),
	{
		name: "Each",
		description: "Evaluate template for each element. varName is the loop variable — use it ONLY inside the template expression (inline). Do NOT create a separate statement for the template.",
		params: z.object({
			array: array(),
			varName: z.string(),
			template: z.any()
		}),
		lazy: true
	}
];
/** All built-in functions by name, in prompt order. */
const BUILTINS = Object.fromEntries(builtins.map((b) => [b.name, b]));
/** Maps parser-level action step names → runtime step type values. Single source of truth. */
const ACTION_STEPS = {
	Run: "run",
	ToAssistant: "continue_conversation",
	OpenUrl: "open_url",
	Set: "set",
	Reset: "reset"
};
/** A schema that only names its type in prompt signatures, e.g. `$variable`. */
function named(id) {
	const schema = z.any();
	tagSchemaId(schema, id);
	return schema;
}
const openUrlData = () => z.object({
	type: z.literal("open_url"),
	url: z.string()
});
const toAssistantData = () => z.object({
	type: z.literal("continue_conversation"),
	context: z.string().optional()
});
const actionSchema = z.union([
	openUrlData(),
	toAssistantData(),
	z.object({
		type: z.string(),
		params: z.record(z.string(), z.any()).optional()
	})
]);
tagSchemaId(actionSchema, "ActionExpression");
/**
* Schema for a component's action prop: `action: action().optional()`. Tagged
* `ActionExpression`, so the prompt teaches the @step syntax (one @step, or
* Action([...]) for several). Its JSON schema describes the legacy `{ type, ... }`
* action objects, which also still work. Returns the same schema on every call.
*/
function action() {
	return actionSchema;
}
/** Refs of the built-in steps that have a legacy JSON shape, for restricted action slots. */
const steps = {
	OpenUrl: { ref: actionRef("OpenUrl", openUrlData()) },
	ToAssistant: { ref: actionRef("ToAssistant", toAssistantData()) }
};
/** The action `$defs` of every library, e.g. action: {$ref: "#/$defs/ActionExpression"}. */
const ACTION_DEFS = {
	ActionExpression: actionSchema,
	OpenUrl: steps.OpenUrl.ref,
	ToAssistant: steps.ToAssistant.ref
};
const builtinSteps$1 = [
	{
		...defineAction({
			name: "Run",
			description: "Execute a Mutation or re-fetch a Query (ref must be a declared Query/Mutation)",
			params: z.object({ ref: named("Query | Mutation") })
		}),
		lazy: true
	},
	{
		...defineAction({
			name: "ToAssistant",
			description: "Send a message to the assistant (for conversational buttons like \"Tell me more\", \"Explain this\"). The optional context is any value (object, array, string, number) passed to the assistant as hidden data, e.g. @ToAssistant(\"Show details\", {orderId: 42})",
			params: z.object({
				message: z.string(),
				context: z.any().optional()
			})
		}),
		ref: steps.ToAssistant.ref,
		step: ({ message, context }) => ({
			type: ACTION_STEPS.ToAssistant,
			message: String(message ?? ""),
			context: context ?? void 0
		})
	},
	{
		...defineAction({
			name: "OpenUrl",
			description: "Navigate to a URL",
			params: z.object({ url: z.string() })
		}),
		ref: steps.OpenUrl.ref,
		step: ({ url }) => ({
			type: ACTION_STEPS.OpenUrl,
			url: String(url ?? "")
		})
	},
	{
		...defineAction({
			name: "Set",
			description: "Set a $variable to a specific value",
			params: z.object({
				variable: named("$variable"),
				value: z.any()
			})
		}),
		lazy: true
	},
	{
		...defineAction({
			name: "Reset",
			description: "Reset $variables to their declared defaults; pass several to reset each (e.g. @Reset($title, $priority) restores $title=\"\" and $priority=\"medium\")",
			params: z.object({ variable: named("$variable") })
		}),
		lazy: true
	}
];
/** All built-in steps by name, in prompt order. */
const BUILTIN_STEPS = Object.fromEntries(builtinSteps$1.map((s) => [s.name, s]));
const callDef = (kind, def) => ({
	kind,
	params: Object.keys(def.params.shape).map((name) => ({
		name,
		required: false
	})),
	builtin: true,
	...def.lazy ? { lazy: true } : {}
});
/** The built-in functions, steps and Action: the base of every library's call registry. */
const BUILTIN_CALLS = new Map([
	...builtins.map((b) => [b.name, callDef("function", b)]),
	...builtinSteps$1.map((s) => [s.name, callDef("action", s)]),
	["Action", {
		kind: "action",
		params: [{
			name: "steps",
			required: true
		}],
		builtin: true,
		lazy: true
	}]
]);
function getCallDefs(cat) {
	return cat?.callDefs ?? BUILTIN_CALLS;
}
/** True for a built-in function, step, or Action (not a component). */
function isBuiltin(name) {
	return BUILTIN_CALLS.has(name);
}
/** True for a step call: a built-in step, Action, or one of `actions`. */
function isStep(name, actions) {
	return BUILTIN_CALLS.get(name)?.kind === "action" || !!actions && Object.prototype.hasOwnProperty.call(actions, name);
}
/** Reserved statement-level call names — not builtins, not components */
const RESERVED_CALLS = {
	Query: "Query",
	Mutation: "Mutation"
};
/** Check if a name is a reserved statement call (Query, Mutation) */
function isReservedCall(name) {
	return name in RESERVED_CALLS;
}
//#endregion
//#region src/telemetry/runtime.ts
const SYSTEM_PROMPT_EVENT_NAME = "lang_core_system_prompt_generation_used";
const PARSER_PARSE_EVENT_NAME = "lang_core_parser_parse_used";
const SYSTEM_PROMPT_SAMPLE_RATE = .1;
const PARSER_PARSE_SAMPLE_RATE = .1;
const SDK_VERSION = "1.0.2";
const STATE_KEY = Symbol.for("@openuidev/lang-core/telemetry/v1");
function buildSystemPromptConfigProjection(spec) {
	const hasTools = (spec.tools?.length ?? 0) > 0;
	const toolCalls = spec.toolCalls ?? hasTools;
	const bindings = spec.bindings ?? toolCalls;
	const components = Object.entries(spec.components).sort(([left], [right]) => left.localeCompare(right)).map(([name, component]) => ({
		name,
		signature: component.signature,
		description: component.description ?? null
	}));
	const componentGroups = (spec.componentGroups ?? []).map((group) => ({
		name: group.name,
		components: [...group.components].sort(),
		notes: [...group.notes ?? []]
	})).sort((left, right) => left.name.localeCompare(right.name));
	return {
		root: spec.root ?? "Root",
		components,
		componentGroups,
		modes: {
			toolCalls,
			bindings,
			editMode: spec.editMode === true,
			inlineMode: spec.inlineMode === true
		}
	};
}
async function sha256(value) {
	const subtle = globalThis.crypto?.subtle;
	if (!subtle) throw new Error("Web Crypto is unavailable");
	const bytes = new TextEncoder().encode(value);
	const digest = await subtle.digest("SHA-256", bytes);
	return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
function calculateSystemPromptConfigHash(spec) {
	return sha256(JSON.stringify(buildSystemPromptConfigProjection(spec)));
}
function getProcess() {
	const candidate = globalThis.process;
	if (!candidate || typeof candidate !== "object") return void 0;
	return candidate;
}
function hasBrowserWindow() {
	const target = globalThis;
	return target.navigator?.product === "ReactNative" || Boolean(target.window && target.document);
}
function isBrowserWorker() {
	const workerGlobalScope = globalThis.WorkerGlobalScope;
	return Boolean(workerGlobalScope && workerGlobalScope.prototype.isPrototypeOf(globalThis));
}
function detectRuntime() {
	if (hasBrowserWindow()) return void 0;
	const target = globalThis;
	const processLike = getProcess();
	if (target.Bun && typeof target.Bun === "object") return {
		name: "bun",
		version: target.Bun.version,
		env: processLike?.env,
		process: processLike
	};
	if (target.Deno && typeof target.Deno === "object") return {
		name: "deno",
		version: target.Deno.version?.deno
	};
	if (typeof target.EdgeRuntime === "string") return {
		name: "edge",
		version: target.EdgeRuntime
	};
	if (typeof target.WebSocketPair === "function") return { name: "edge" };
	if (isBrowserWorker()) return void 0;
	if (processLike?.release?.name === "node" || typeof processLike?.versions?.node === "string" && processLike.versions.node.length > 0) return {
		name: "node",
		version: processLike.versions?.node,
		env: processLike.env,
		process: processLike
	};
}
function getEnvironment(env) {
	const value = env?.NODE_ENV;
	if (value === "production" || value === "development" || value === "test") return value;
	return "unknown";
}
function getState() {
	const registry = globalThis;
	registry[STATE_KEY] ??= { runtimeId: globalThis.crypto.randomUUID() };
	return registry[STATE_KEY];
}
function readGitOrigin(processLike) {
	const execFile = (processLike.getBuiltinModule?.("node:child_process"))?.execFile;
	if (!execFile) return Promise.resolve(void 0);
	return new Promise((resolve) => {
		try {
			execFile("git", [
				"config",
				"--local",
				"--get",
				"remote.origin.url"
			], {
				encoding: "utf8",
				timeout: 1e3,
				windowsHide: true
			}, (error, stdout) => {
				const origin = stdout?.trim();
				resolve(error || !origin ? void 0 : origin);
			});
		} catch {
			resolve(void 0);
		}
	});
}
async function getRepositoryIdentifier(processLike) {
	if (!processLike) return void 0;
	const rawValue = await readGitOrigin(processLike) || processLike.env?.REPOSITORY_URL || processLike.cwd?.();
	return rawValue ? normalizeProjectIdentity(rawValue) || void 0 : void 0;
}
function calculateProjectHash(repositoryIdentifier) {
	const normalized = normalizeProjectIdentity(repositoryIdentifier);
	if (!normalized) return Promise.reject(/* @__PURE__ */ new TypeError("Repository identifier is empty"));
	return sha256(`${PROJECT_ID_SALT}\0${normalized}`);
}
function getProjectHash(state, runtime) {
	if (runtime.name !== "node") return Promise.resolve(void 0);
	state.projectHash ??= Promise.resolve().then(() => getRepositoryIdentifier(runtime.process)).then((identifier) => identifier ? calculateProjectHash(identifier) : void 0).catch(() => void 0);
	return state.projectHash;
}
async function getCIInfo(runtime) {
	if (!runtime.env) return {
		id: null,
		isCI: false
	};
	try {
		const ciInfo = await import("ci-info");
		return {
			id: ciInfo.id,
			isCI: ciInfo.isCI
		};
	} catch {
		return {
			id: null,
			isCI: false
		};
	}
}
async function postCapture(event, properties, runtime) {
	const postHog = getPostHogConfig(runtime.env);
	await globalThis.fetch(postHog.captureUrl, {
		method: "POST",
		headers: { "content-type": "application/json" },
		body: JSON.stringify({
			api_key: postHog.apiKey,
			event,
			timestamp: (/* @__PURE__ */ new Date()).toISOString(),
			properties
		}),
		keepalive: true,
		signal: globalThis.AbortSignal.timeout(TELEMETRY_REQUEST_TIMEOUT_MS)
	});
}
async function sendSystemPromptCapture(state, spec, configHash, inputShape, runtime, environment) {
	const [systemPromptConfigHash, projectHash, ciInfo] = await Promise.all([
		configHash,
		getProjectHash(state, runtime),
		getCIInfo(runtime)
	]);
	await postCapture(SYSTEM_PROMPT_EVENT_NAME, {
		distinct_id: state.runtimeId,
		$process_person_profile: false,
		event_id: globalThis.crypto.randomUUID(),
		telemetry_schema_version: 2,
		system_prompt_config_hash_version: 1,
		system_prompt_config_hash: systemPromptConfigHash,
		component_count: Object.keys(spec.components).length,
		tool_count: spec.tools?.length ?? 0,
		sdk_name: "@openuidev/lang-core",
		sdk_version: SDK_VERSION,
		api_surface: "generate_system_prompt",
		input_shape: inputShape,
		runtime: runtime.name,
		runtime_version: runtime.version,
		environment,
		ci: ciInfo.isCI,
		...ciInfo.id ? { ci_name: ciInfo.id } : {},
		sample_rate: SYSTEM_PROMPT_SAMPLE_RATE,
		...projectHash ? {
			project_hash_version: 2,
			project_hash: projectHash
		} : {}
	}, runtime);
}
function recordSystemPromptGeneration(spec, inputShape) {
	try {
		const runtime = detectRuntime();
		if (!runtime || typeof globalThis.fetch !== "function" || !globalThis.crypto?.subtle || typeof globalThis.crypto.randomUUID !== "function" || typeof globalThis.AbortSignal?.timeout !== "function") return;
		const env = runtime.env;
		const environment = getEnvironment(env);
		if (!isRuntimeTelemetryEnabled(env)) return;
		if (Math.random() >= SYSTEM_PROMPT_SAMPLE_RATE) return;
		const state = getState();
		const configHash = calculateSystemPromptConfigHash(spec);
		Promise.resolve().then(() => sendSystemPromptCapture(state, spec, configHash, inputShape, runtime, environment)).catch(() => void 0);
	} catch {}
}
function getValidationErrorCounts(result) {
	const counts = {
		"unknown-component": 0,
		"missing-required": 0,
		"null-required": 0,
		"inline-reserved": 0,
		"excess-args": 0,
		"type-mismatch": 0,
		"unknown-function": 0,
		"no-root": 0
	};
	for (const error of result.meta.errors) counts[error.code] += 1;
	return {
		unknown_component_count: counts["unknown-component"],
		missing_required_count: counts["missing-required"],
		null_required_count: counts["null-required"],
		inline_reserved_count: counts["inline-reserved"],
		excess_args_count: counts["excess-args"]
	};
}
function summarizeParserParse(result) {
	return {
		outcome: result.meta.errors.length > 0 ? "invalid" : result.root ? "valid" : "no_renderable_root",
		incomplete: result.meta.incomplete,
		has_renderable_root: result.root !== null,
		statement_count: result.meta.statementCount,
		unresolved_count: result.meta.unresolved.length,
		orphaned_count: result.meta.orphaned.length,
		validation_error_count: result.meta.errors.length,
		...getValidationErrorCounts(result)
	};
}
async function sendParserParseCapture(context, outcome) {
	const { state, runtime, environment } = context;
	const [projectHash, ciInfo] = await Promise.all([getProjectHash(state, runtime), getCIInfo(runtime)]);
	await postCapture(PARSER_PARSE_EVENT_NAME, {
		distinct_id: state.runtimeId,
		$process_person_profile: false,
		event_id: globalThis.crypto.randomUUID(),
		telemetry_schema_version: 2,
		sdk_name: "@openuidev/lang-core",
		sdk_version: SDK_VERSION,
		api_surface: "parser.parse",
		runtime: runtime.name,
		runtime_version: runtime.version,
		environment,
		ci: ciInfo.isCI,
		...ciInfo.id ? { ci_name: ciInfo.id } : {},
		sample_rate: PARSER_PARSE_SAMPLE_RATE,
		...outcome,
		...projectHash ? {
			project_hash_version: 2,
			project_hash: projectHash
		} : {}
	}, runtime);
}
function prepareParserParseTelemetry() {
	try {
		const runtime = detectRuntime();
		if (!runtime || typeof globalThis.fetch !== "function" || typeof globalThis.crypto?.randomUUID !== "function" || typeof globalThis.AbortSignal?.timeout !== "function") return;
		const env = runtime.env;
		if (!isRuntimeTelemetryEnabled(env)) return void 0;
		if (Math.random() >= PARSER_PARSE_SAMPLE_RATE) return void 0;
		return {
			state: getState(),
			runtime,
			environment: getEnvironment(env)
		};
	} catch {
		return;
	}
}
function captureParserParseResult(context, result) {
	if (!context) return;
	try {
		const outcome = summarizeParserParse(result);
		Promise.resolve().then(() => sendParserParseCapture(context, outcome)).catch(() => void 0);
	} catch {}
}
function captureParserParseException(context) {
	if (!context) return;
	try {
		Promise.resolve().then(() => sendParserParseCapture(context, { outcome: "threw" })).catch(() => void 0);
	} catch {}
}
//#endregion
//#region src/parser/validate-library.ts
const REF_PATTERN = /^#\/\$defs\/(.+)$/;
function isPlainObject(value) {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}
/** Recursively collect every `$ref` and check it resolves within `$defs`. */
function collectRefIssues(node, path, defNames, issues) {
	if (Array.isArray(node)) {
		node.forEach((item, i) => collectRefIssues(item, `${path}/${i}`, defNames, issues));
		return;
	}
	if (!isPlainObject(node)) return;
	const ref = node["$ref"];
	if (typeof ref === "string") {
		const match = REF_PATTERN.exec(ref);
		if (!match || !defNames.has(match[1])) issues.push({
			code: "unresolved-ref",
			message: `Unresolvable $ref "${ref}" at ${path} — refs must be "#/$defs/<name>" pointing at a component in $defs.`,
			path
		});
	}
	for (const [key, value] of Object.entries(node)) {
		if (key === "$ref") continue;
		collectRefIssues(value, `${path}/${key}`, defNames, issues);
	}
}
/**
* Structural validation of your own library.
* Returns ALL issues found (empty array = valid).
*/
function validateChatLibrary(library) {
	const issues = [];
	if (!isPlainObject(library)) return [{
		code: "invalid-shape",
		message: "chatLibrary must be an object."
	}];
	const { root, schema, componentGroups } = library;
	if (root !== void 0 && (typeof root !== "string" || root.length === 0)) issues.push({
		code: "invalid-shape",
		message: "chatLibrary.root, when present, must be a non-empty string.",
		path: "root"
	});
	if (!isPlainObject(schema)) {
		issues.push({
			code: "invalid-shape",
			message: "chatLibrary.schema must be an object with a $defs map of component schemas.",
			path: "schema"
		});
		return issues;
	}
	const defs = schema.$defs;
	if (!isPlainObject(defs) || Object.keys(defs).length === 0) {
		issues.push({
			code: "invalid-shape",
			message: "chatLibrary.schema.$defs must be a non-empty object keyed by component name.",
			path: "schema/$defs"
		});
		return issues;
	}
	const defNames = new Set(Object.keys(defs));
	if (typeof root === "string" && root.length > 0 && !defNames.has(root)) issues.push({
		code: "root-not-found",
		message: `Root component "${root}" was not found in schema.$defs. Available components: ${[...defNames].join(", ")}.`,
		path: "root"
	});
	for (const [name, def] of Object.entries(defs)) {
		const defPath = `$defs/${name}`;
		if (!isPlainObject(def)) {
			issues.push({
				code: "invalid-shape",
				message: `${defPath} must be an object component schema.`,
				path: defPath
			});
			continue;
		}
		const properties = def["properties"];
		if (properties !== void 0 && !isPlainObject(properties)) issues.push({
			code: "invalid-shape",
			message: `${defPath}/properties must be an object.`,
			path: `${defPath}/properties`
		});
		const required = def["required"];
		if (required !== void 0) if (!Array.isArray(required) || required.some((r) => typeof r !== "string")) issues.push({
			code: "invalid-required",
			message: `${defPath}/required must be an array of property names.`,
			path: `${defPath}/required`
		});
		else {
			const propKeys = new Set(isPlainObject(properties) ? Object.keys(properties) : []);
			for (const r of required) if (!propKeys.has(r)) issues.push({
				code: "invalid-required",
				message: `${defPath} lists required property "${r}" that is not in properties.`,
				path: `${defPath}/required`
			});
		}
		collectRefIssues(properties, `${defPath}/properties`, defNames, issues);
	}
	for (const group of componentGroups ?? []) {
		if (!isPlainObject(group) || typeof group.name !== "string" || !Array.isArray(group.components)) {
			issues.push({
				code: "invalid-shape",
				message: "Each componentGroups entry must be {name: string, components: string[], notes?: string[]}.",
				path: "componentGroups"
			});
			continue;
		}
		for (const comp of group.components) if (typeof comp !== "string" || !defNames.has(comp)) issues.push({
			code: "unknown-group-component",
			message: `Component group "${group.name}" references unknown component "${String(comp)}".`,
			path: "componentGroups"
		});
	}
	return issues;
}
//#endregion
//#region src/parser/cloud-config.ts
/** `]]>openui:config\n` — request-direction config block header. Trailing newline is part of the wire contract. */
const CLOUD_CONFIG_MARKER = "]]>openui:config\n";
/**
* Wire pin for OpenUI Cloud's built-in chat library when `generateSystemPrompt({ cloud: true })`
* is called without your own library. Cloud rejects a non-numeric or too-old version.
*/
const CLOUD_CHAT_LIBRARY_VERSION = "0.1.0";
function pickCloudPromptOptions(options) {
	if (!options) return void 0;
	const picked = {};
	if (options.examples) picked.examples = options.examples;
	if (options.preamble) picked.preamble = options.preamble;
	if (options.additionalRules) picked.additionalRules = options.additionalRules;
	return Object.keys(picked).length > 0 ? picked : void 0;
}
function generateCloudConfig(spec) {
	let config;
	if (spec.library) {
		const issues = validateChatLibrary(spec.library);
		if (issues.length > 0) throw new Error(`[generateSystemPrompt] Invalid library: ${issues.map((i) => i.message).join(" ")}`);
		const { components: _components, ...chatLibrary } = spec.library;
		const promptOptions = pickCloudPromptOptions(spec.promptOptions);
		config = {
			chatLibrary,
			...promptOptions ? { systemPromptOptions: promptOptions } : {}
		};
	} else {
		if (spec.promptOptions && pickCloudPromptOptions(spec.promptOptions)) throw new Error("[generateSystemPrompt] promptOptions requires a library — the built-in library ignores it.");
		config = { libraryVersion: CLOUD_CHAT_LIBRARY_VERSION };
	}
	const block = `${CLOUD_CONFIG_MARKER}${JSON.stringify(config)}`;
	return spec.instructions ? `${block}\n${spec.instructions}` : block;
}
//#endregion
//#region src/parser/prompt.ts
function jsonSchemaTypeStr(schema) {
	const type = schema.type;
	if (type === "string") {
		const enumVals = schema.enum;
		if (enumVals) return enumVals.map((v) => `"${v}"`).join(" | ");
		return "string";
	}
	if (type === "number" || type === "integer") return "number";
	if (type === "boolean") return "boolean";
	if (type === "array") {
		const items = schema.items;
		if (items) return `${jsonSchemaTypeStr(items)}[]`;
		return "any[]";
	}
	if (type === "object") {
		const props = schema.properties;
		if (props && Object.keys(props).length > 0) {
			const required = schema.required ?? [];
			return `{${Object.entries(props).map(([k, v]) => {
				return `${k}${required.includes(k) ? "" : "?"}: ${jsonSchemaTypeStr(v)}`;
			}).join(", ")}}`;
		}
		return "object";
	}
	return "any";
}
/** Generate a default-values hint object for an output schema. */
function defaultForSchema(schema) {
	const type = schema.type;
	if (type === "string") return "";
	if (type === "number" || type === "integer") return 0;
	if (type === "boolean") return false;
	if (type === "array") return [];
	if (type === "object") {
		const props = schema.properties;
		if (props && Object.keys(props).length > 0) {
			const result = {};
			for (const [k, v] of Object.entries(props)) result[k] = defaultForSchema(v);
			return result;
		}
		return {};
	}
	return null;
}
const PREAMBLE = `You are an AI assistant that responds using openui-lang, a declarative UI language. Your ENTIRE response must be valid openui-lang code — no markdown, no explanations, just openui-lang.`;
function syntaxRules(rootName, flags) {
	const lines = [
		"## Syntax Rules",
		"",
		"1. Each statement is on its own line: `identifier = Expression`",
		`2. \`root\` is the entry point — every program must define \`root = ${rootName}(...)\``,
		"3. Expressions are: strings (\"...\"), numbers, booleans (true/false), null, arrays ([...]), objects ({...}), or component calls TypeName(arg1, arg2, ...)",
		"4. Use references for readability: define `name = ...` on one line, then use `name` later",
		"5. EVERY variable (except root) MUST be referenced by at least one other variable. Unreferenced variables are silently dropped and will NOT render. Always include defined variables in their parent's children/items array.",
		"6. Arguments are POSITIONAL (order matters, not names). Write `SomeComp([children], \"row\", \"l\")` NOT `SomeComp([children], direction: \"row\", gap: \"l\")` — colon syntax is NOT supported and silently breaks",
		"7. Optional arguments can be omitted from the end"
	];
	let ruleNum = 8;
	if (flags.bindings) lines.push(`${ruleNum++}. Declare mutable state with \`$varName = defaultValue\`. Components marked with \`$binding\` can read/write these. Undeclared $variables are auto-created with null default.`);
	if (flags.supportsExpressions) lines.push(`${ruleNum++}. String concatenation: \`"text" + $var + "more"\``, `${ruleNum++}. Dot member access: \`query.field\` reads a field; on arrays it extracts that field from every element`, `${ruleNum++}. Index access: \`arr[0]\`, \`data[index]\``, `${ruleNum++}. Arithmetic operators: +, -, *, /, % (work on numbers; + is string concat when either side is a string)`, `${ruleNum++}. Comparison: ==, !=, >, <, >=, <=`, `${ruleNum++}. Logical: &&, ||, ! (prefix)`, `${ruleNum++}. Ternary: \`condition ? valueIfTrue : valueIfFalse\``, `${ruleNum++}. Parentheses for grouping: \`(a + b) * c\``);
	lines.push("- Strings use double quotes with backslash escaping");
	return lines.join("\n");
}
function functionLine(f) {
	return f.description ? `@${f.signature} — ${f.description}` : `@${f.signature}`;
}
function builtinFunctionsSection(custom, toolCalls) {
	const lines = [...Object.values(BUILTINS).map((b) => functionLine({
		...b,
		signature: schemaSignature(b.name, b.params, b.returns)
	})), ...custom].join("\n");
	return `## Built-in Functions

Data functions prefixed with \`@\` to distinguish from components. These are the ONLY functions available — do NOT invent new ones.
Use @-prefixed built-in functions (@Count, @Sum, @Avg, @Min, @Max, @Round) on ${toolCalls ? "Query results" : "data"}. Do NOT hardcode computed values.

${lines}

Builtins compose — output of one is input to the next:
\`@Count(@Filter(data.rows, "field", "==", "val"))\` for KPIs/chart values, \`@Round(@Avg(data.rows.score), 1)\`, \`@Each(data.rows, "item", Comp(item.field))\` for per-item rendering.
Array pluck: \`data.rows.field\` extracts a field from every row → use with @Sum, @Avg, charts, tables.

IMPORTANT @Each rule: The loop variable (e.g. "item") is ONLY available inside the @Each template expression. Always inline the template — do NOT extract it to a separate statement.
CORRECT: \`list = @Each(rows, "t", Comp(t.name))\`
WRONG: \`item = Comp(t.name)\` then \`list = @Each(rows, "t", item)\`, because t is undefined in item.`;
}
function querySection() {
	return `## Query — Live Data Fetching

Fetch data from available tools. Returns defaults instantly, swaps in real data when it arrives.

\`\`\`
metrics = Query("tool_name", {arg1: value, arg2: $binding}, {defaultField: 0, defaultData: []}, refreshInterval?)
\`\`\`

- First arg: tool name (string)
- Second arg: arguments object (may reference $bindings — re-fetches automatically on change)
- Third arg: default data (rendered immediately before fetch resolves)
- Fourth arg (optional): refresh interval in seconds (e.g. 30 for auto-refresh every 30s)
- Use dot access on results: metrics.totalEvents, metrics.data.day (array pluck)
- Query results must use regular identifiers: \`metrics = Query(...)\`, NOT \`$metrics = Query(...)\`
- Manual refresh: \`Button("Refresh", Action([@Run(query1), @Run(query2)]), "secondary")\` — re-fetches the listed queries
- Refresh all queries: create Action with @Run for each query`;
}
function mutationSection() {
	return `## Mutation — Write Operations

Execute state-changing tool calls (create, update, delete). Unlike Query (auto-fetches on render), Mutation fires only on button click via Action.

\`\`\`
result = Mutation("tool_name", {arg1: $binding, arg2: "value"})
\`\`\`

- First arg: tool name (string)
- Second arg: arguments object (evaluated with current $binding values at click time)
- result.status: "idle" | "loading" | "success" | "error"
- result.data: tool response on success
- result.error: error message on failure
- Mutation results use regular identifiers: \`result = Mutation(...)\`, NOT \`$result\`
- Show loading state: \`result.status == "loading" ? TextContent("Saving...") : null\``;
}
const ACTION_HEADER = "## Action — Button Behavior";
/** The built-in steps the prompt lists: @Run needs toolCalls, @Set and @Reset need bindings. */
function builtinSteps(flags) {
	return Object.values(BUILTIN_STEPS).filter((s) => {
		if (s.name === "Run") return flags.toolCalls;
		if (s.name === "Set" || s.name === "Reset") return flags.bindings;
		return true;
	});
}
function actionSection(flags, custom) {
	const steps = [...builtinSteps(flags).map((s) => `- ${functionLine({
		...s,
		signature: schemaSignature(s.name, s.params)
	})}`), ...custom];
	const examples = [];
	if (flags.toolCalls) examples.push(`Example — mutation + refresh + reset (PREFERRED pattern):
\`\`\`
$binding = "default"
result = Mutation("tool_name", {field: $binding})
data = Query("tool_name", {}, {rows: []})
onSubmit = Action([@Run(result), @Run(data), @Reset($binding)])
\`\`\``);
	examples.push(`Example — simple nav:
\`\`\`
viewBtn = Button("View", @OpenUrl("https://example.com"))
\`\`\``);
	const rules = ["- An action can be assigned to a variable or inlined: Button(\"Go\", onSubmit) and Button(\"Go\", Action([...])) both work"];
	if (flags.toolCalls) rules.push("- If a @Run(mutation) step fails, remaining steps are skipped (halt on failure)", "- @Run(queryRef) re-fetches the query (fire-and-forget, cannot fail)");
	return `${ACTION_HEADER}

An action wires a button click to operations. Pass one @-prefixed step bare, e.g. Button("Ask", @ToAssistant("Tell me more")). Put several steps in Action([...]), e.g. Button("Read", Action([@OpenUrl("https://example.com"), @ToAssistant("Summarize this page")])). Steps execute in order.
Buttons without an action automatically send their label to the assistant (equivalent to @ToAssistant(label)).

Available steps:
${steps.join("\n")}

${examples.join("\n\n")}

${rules.join("\n")}`;
}
function interactiveFiltersSection() {
	return `## Interactive Filters

To let the user filter data with a dropdown:
1. Declare a $variable with a default: \`$dateRange = "14"\`
2. Create a Select with name, items, and binding: \`Select("dateRange", [SelectItem("7", "Last 7 days"), ...], null, null, $dateRange)\`
3. Wrap in FormControl for a label: \`FormControl("Date Range", Select(...))\`
4. Pass $dateRange in Query args: \`Query("tool", {dateRange: $dateRange}, {defaults})\`
5. When the user changes the Select, $dateRange updates and the Query automatically re-fetches

FILTER WIRING RULE: If a $binding filter is visible in the UI, EVERY relevant Query MUST reference that $binding in its args. Never show a filter dropdown while hardcoding the query args.

Rules for $variables:
- $variables hold simple values (strings or numbers), NOT arrays or objects
- $variables must be bound to a Select/Input component via the value argument (last positional arg) to be interactive
- Queries must use regular identifiers (NOT $variables): \`metrics = Query(...)\` not \`$metrics = Query(...)\`
- **Auto-declare**: You do NOT need to explicitly declare $variables. If you use \`$foo\` without declaring it, the parser auto-creates \`$foo = null\`. You can still declare explicitly to set a default: \`$days = "14"\`

## Forms

Simple form — no $bindings needed. Field values are managed internally by the Form via the name prop:
\`\`\`
contactForm = Form("contact", submitBtn, [nameField, emailField])
nameField = FormControl("Name", Input("name", "Your name", "text", {required: true}))
emailField = FormControl("Email", Input("email", "your@email.com", "email", {required: true, email: true}))
submitBtn = Button("Submit")
\`\`\`

Use $bindings when you need to read field values elsewhere (in Action context, Query args, or conditionals). They are auto-declared:
\`\`\`
$role = "engineer"
contactForm = Form("contact", submitBtn, [nameField, emailField, roleField])
nameField = FormControl("Name", Input("name", "Enter your name", "text", {required: true}, $name))
emailField = FormControl("Email", Input("email", "Enter your email", "email", {required: true, email: true}, $email))
roleField = FormControl("Role", Select("role", [SelectItem("engineer", "Engineer"), SelectItem("designer", "Designer"), SelectItem("pm", "PM")], null, {required: true}, $role))
submitBtn = Button("Submit")
\`\`\`

For form + mutation patterns (create, refresh, reset), see the Action section example above.

IMPORTANT: Always add validation rules to form fields used with Mutations. Use OBJECT syntax: {required: true, email: true, minLength: 8}. The renderer shows error messages automatically and blocks submit when validation fails.`;
}
function editModeSection() {
	return `## Edit Mode

The runtime merges by statement name: same name = replace, new name = append.
Output ONLY statements that changed or are new. Everything else is kept automatically.

### Delete
To remove a component, re-declare its parent without that component in the parent's children array. The removed component and any statements only it referenced are automatically garbage-collected.

### Patch size guide
- Changing a title or label: 1 statement
- Adding a component: 2-3 statements (the new component + parent update)
- Removing a component: 1 statement (re-declare parent without the removed child)
- Adding a filter + wiring to query: 3-5 statements
- Restructuring into tabs: 5-10 statements

### Rules
- Reuse existing statement names exactly — do not rename
- Do NOT re-emit unchanged statements — the runtime keeps them
- A typical edit patch is 1-10 statements, not 20+
- If the existing code already satisfies the request, output only the root statement
- NEVER output the entire program as a patch. Only output what actually changes
- If you are about to output more than 10 statements, reconsider — most edits need fewer`;
}
function streamingRules(rootName, flags) {
	const steps = [`1. \`root = ${rootName}(...)\` — UI shell appears immediately`];
	if (flags.supportsExpressions) {
		steps.push("2. $variable declarations — state ready for bindings");
		if (flags.toolCalls) steps.push("3. Query statements — defaults resolve immediately so components render with data");
		const n = steps.length;
		steps.push(`${n + 1}. Component definitions — fill in with data already available`);
		steps.push(`${n + 2}. Data values — leaf content last`);
	} else {
		steps.push("2. Component definitions — fill in as they stream");
		steps.push("3. Data values — leaf content last");
	}
	return `## Hoisting & Streaming (CRITICAL)

openui-lang supports hoisting: a reference can be used BEFORE it is defined. The parser resolves all references after the full input is parsed.

During streaming, the output is re-parsed on every chunk. Undefined references are temporarily unresolved and appear once their definitions stream in. This creates a progressive top-down reveal — structure first, then data fills in.

**Recommended statement order for optimal streaming:**
${steps.join("\n")}

Always write the root = ${rootName}(...) statement first so the UI shell appears immediately, even before child data has streamed in.`;
}
function inlineModeSection() {
	return `## Inline Mode

You are in inline mode. You can respond in two ways:

### 1. Code response (when the user wants to CREATE or CHANGE the UI)
Wrap openui-lang code in triple-backtick fences. You can include explanatory text before/after:

Here's your dashboard:

\`\`\`openui-lang
root = RootComp([header, content])
header = SomeHeader("Title")
content = SomeContent("Hello world")
\`\`\`

I created a simple layout with a header.

### 2. Text-only response (when the user asks a QUESTION)
If the user asks "what is this?", "explain the chart", "how does this work", etc. — respond with plain text. Do NOT output any openui-lang code. The existing dashboard stays unchanged.

### Rules
- When the user asks for changes, output ONLY the changed/new statements in a fenced block
- When the user asks a question, respond with text only — NO code. The dashboard stays unchanged.
- The parser extracts code from fences automatically. Text outside fences is shown as chat.`;
}
function toolWorkflowSection() {
	return `## Data Workflow

When tools are available, follow this workflow:
1. FIRST: Call the most relevant tool to inspect the real data shape before generating code
2. Use Query() for READ operations (data that should stay live) — NEVER hardcode tool results as literal arrays or objects
3. Use Mutation() for WRITE operations (create, update, delete) — triggered by button clicks via @Run(mutationRef)
4. Use the real data from step 1 as condensed Query defaults (3-5 rows) so the UI renders immediately
5. Use @-prefixed builtins (@Count, @Filter, @Sort, @Sum) on Query results for KPIs and aggregations — the runtime evaluates these live on every refresh
6. Hardcoded arrays are ONLY for static display data (labels, options) where no tool exists

WRONG — you called a tool and got data back, but you inlined the results:
\`\`\`
openCount = 2
item1 = SomeComp("first item title")
item2 = SomeComp("second item title")
list = SomeList([item1, item2])
chart = SomeChart(["A", "B"], [12, 8])
\`\`\`
This is static — it shows stale data and won't update. Creating item1, item2, item3... manually is ALWAYS wrong when a tool exists.

RIGHT — use Query() for live data, Mutation() for writes, @builtins to derive values:
\`\`\`
data = Query("tool_name", {}, {rows: []})
openCount = @Count(@Filter(data.rows, "field", "==", "value"))
list = @Each(data.rows, "item", SomeComp(item.title, item.field))
createResult = Mutation("create_tool", {title: $title})
submitBtn = Button("Create", Action([@Run(createResult), @Run(data), @Reset($title)]))
\`\`\`
Everything derives from the Query — when data refreshes, the entire dashboard updates automatically.`;
}
function importantRules(rootName, flags) {
	const verifyLines = [`1. root = ${rootName}(...) is the FIRST line (for optimal streaming).`, "2. Every referenced name is defined. Every defined name (other than root) is reachable from root."];
	if (flags.toolCalls) verifyLines.push("3. Every Query result is referenced by at least one component.");
	if (flags.bindings) verifyLines.push(`${flags.toolCalls ? "4" : "3"}. Every $binding appears in at least one component or expression.`);
	if (flags.toolCalls && flags.bindings) verifyLines.push("5. Every visible filter $binding appears in at least one Query args object.");
	return `## Important Rules
- Choose components that best represent the content (tables for comparisons, charts for trends, forms for input, etc.)

## Final Verification
Before finishing, walk your output and verify:
${verifyLines.join("\n")}`;
}
function renderToolSignature(tool) {
	let args = "";
	if (tool.inputSchema) {
		const props = tool.inputSchema.properties;
		const required = tool.inputSchema.required ?? [];
		if (props && Object.keys(props).length > 0) args = Object.entries(props).map(([k, v]) => {
			return `${k}${required.includes(k) ? "" : "?"}: ${jsonSchemaTypeStr(v)}`;
		}).join(", ");
	}
	let returnType = "";
	if (tool.outputSchema) returnType = ` → ${jsonSchemaTypeStr(tool.outputSchema)}`;
	let line = `- ${tool.name}(${args})${returnType}`;
	if (tool.description) line += `\n  ${tool.description}`;
	return line;
}
function renderToolsSection(tools) {
	const lines = [];
	const stringTools = [];
	const specTools = [];
	for (const tool of tools) if (typeof tool === "string") stringTools.push(tool);
	else specTools.push(tool);
	lines.push("## Available Tools");
	lines.push("");
	lines.push("Use these with Query() for read operations or Mutation() for write operations. The LLM decides which is appropriate based on the tool's purpose.");
	lines.push("");
	for (const t of stringTools) lines.push(`- ${t}`);
	for (const t of specTools) lines.push(renderToolSignature(t));
	const toolsWithOutput = specTools.filter((t) => t.outputSchema);
	if (toolsWithOutput.length > 0) {
		lines.push("");
		lines.push("### Default values for Query results");
		lines.push("");
		lines.push("Use these shapes as minimal Query defaults:");
		for (const t of toolsWithOutput) {
			const defaults = defaultForSchema(t.outputSchema);
			lines.push(`- ${t.name}: \`${JSON.stringify(defaults)}\``);
		}
	}
	lines.push("");
	lines.push("CRITICAL: Use ONLY the tools listed above in Query() and Mutation() calls. Do NOT invent or guess tool names. If the user asks for functionality that doesn't match any available tool, use realistic mock data instead of fabricating a tool call.");
	return lines.join("\n");
}
const RESTRICTED_SLOT = /[:|] @[A-Z]/;
function generateComponentSignatures(spec, flags) {
	const lines = [
		"## Component Signatures",
		"",
		"Arguments marked with ? are optional. Sub-components can be inline or referenced; prefer references for better streaming."
	];
	if (flags.usesActionExpression) {
		const allSteps = [...builtinSteps(flags).map((step) => step.name), ...Object.keys(spec.actions ?? {})].map((name) => `@${name}`);
		lines.push(`Props typed \`ActionExpression\` accept one @step, or Action([@steps...]) for several. See the Action section for available steps (${allSteps.join(", ")}).`);
		if (Object.values(spec.components).some((c) => RESTRICTED_SLOT.test(c.signature ?? ""))) lines.push("Props typed like `@OpenUrl | @ToAssistant` accept only the steps listed.");
	}
	if (flags.bindings || Object.values(spec.components).some((c) => c.signature?.includes("$binding"))) lines.push("Props marked `$binding<type>` accept a `$variable` reference for two-way binding.");
	const formatSig = (comp) => comp.description ? `${comp.signature} — ${comp.description}` : comp.signature;
	if (spec.componentGroups?.length) {
		const grouped = /* @__PURE__ */ new Set();
		for (const group of spec.componentGroups) {
			lines.push("", `### ${group.name}`);
			for (const name of group.components) {
				if (grouped.has(name)) continue;
				const comp = spec.components[name];
				if (!comp) continue;
				grouped.add(name);
				lines.push(formatSig(comp));
			}
			if (group.notes?.length) for (const note of group.notes) lines.push(note);
		}
		const ungrouped = Object.keys(spec.components).filter((n) => !grouped.has(n));
		if (ungrouped.length) {
			lines.push("", "### Other");
			for (const name of ungrouped) {
				const comp = spec.components[name];
				lines.push(formatSig(comp));
			}
		}
	} else {
		lines.push("");
		for (const [, comp] of Object.entries(spec.components)) lines.push(formatSig(comp));
	}
	return lines.join("\n");
}
/** @deprecated Use {@link generateSystemPrompt}. */
function generatePrompt(spec) {
	const rootName = spec.root ?? "Root";
	const hasTools = !!spec.tools?.length;
	const toolCalls = spec.toolCalls ?? hasTools;
	const bindings = spec.bindings ?? toolCalls;
	const supportsExpressions = toolCalls || bindings;
	const usesActionExpression = Object.values(spec.components).some((c) => c.signature?.includes("ActionExpression") || RESTRICTED_SLOT.test(c.signature ?? ""));
	const parts = [];
	parts.push(spec.preamble ?? PREAMBLE);
	parts.push("");
	parts.push(syntaxRules(rootName, {
		supportsExpressions,
		bindings
	}));
	parts.push("");
	parts.push(generateComponentSignatures(spec, {
		toolCalls,
		bindings,
		usesActionExpression
	}));
	const customFunctions = Object.values(spec.functions ?? {}).map(functionLine);
	if (spec.builtinFunctions ?? supportsExpressions) {
		parts.push("");
		parts.push(builtinFunctionsSection(customFunctions, toolCalls));
	} else if (customFunctions.length) {
		parts.push("");
		parts.push(`## Built-in Functions\n\n${customFunctions.join("\n")}`);
	}
	if (toolCalls) {
		parts.push("");
		parts.push(querySection());
		parts.push("");
		parts.push(mutationSection());
	}
	const customActions = Object.values(spec.actions ?? {}).map((a) => `- ${functionLine(a)}`);
	if (usesActionExpression) {
		parts.push("");
		parts.push(actionSection({
			toolCalls,
			bindings
		}, customActions));
	} else if (customActions.length) {
		parts.push("");
		parts.push(`${ACTION_HEADER}\n\nPass one step, or Action([...]) with several steps, as a button's action.\n\nAvailable steps:\n${customActions.join("\n")}`);
	}
	if (toolCalls && bindings) {
		parts.push("");
		parts.push(interactiveFiltersSection());
	}
	if (toolCalls) {
		parts.push("");
		parts.push(toolWorkflowSection());
	}
	if (spec.tools?.length) {
		parts.push("");
		parts.push(renderToolsSection(spec.tools));
	}
	parts.push("");
	parts.push(streamingRules(rootName, {
		supportsExpressions,
		toolCalls
	}));
	const allExamples = [...spec.examples ?? [], ...spec.toolExamples ?? []];
	if (allExamples.length) {
		parts.push("");
		parts.push("## Examples");
		parts.push("");
		for (const ex of allExamples) {
			parts.push(ex);
			parts.push("");
		}
	}
	if (spec.editMode) {
		parts.push("");
		parts.push(editModeSection());
	}
	if (spec.inlineMode) {
		parts.push("");
		parts.push(inlineModeSection());
	}
	parts.push(importantRules(rootName, {
		toolCalls,
		bindings
	}));
	if (spec.additionalRules?.length) {
		parts.push("");
		for (const rule of spec.additionalRules) parts.push(`- ${rule}`);
	}
	return parts.join("\n");
}
function generateSystemPrompt(spec) {
	if (isCloudSpec(spec)) return generateCloudConfig(spec);
	if (!isSystemPromptSpec(spec)) {
		const prompt = generatePrompt(spec);
		recordSystemPromptGeneration(spec, "legacy_prompt_spec");
		return prompt;
	}
	const merged = {
		...spec.library,
		...spec.promptOptions
	};
	const prompt = generatePrompt(merged);
	recordSystemPromptGeneration(merged, "library_spec");
	return prompt;
}
function isCloudSpec(spec) {
	return "cloud" in spec && spec.cloud === true;
}
function isSystemPromptSpec(spec) {
	return "library" in spec && typeof spec.library === "object";
}
//#endregion
//#region src/library.ts
/**
* Define a component with name, schema, description, and renderer.
* Tags the schema with the component name so it resolves in prompt
* signatures even if the component isn't in every library.
*/
function defineComponent(config) {
	assertV4Schema(config.props, config.name);
	tagSchemaId(config.props, config.name);
	return {
		...config,
		ref: config.props
	};
}
function buildComponentSpecs(components, reg) {
	const specs = {};
	for (const [name, def] of Object.entries(components)) specs[name] = {
		signature: schemaSignature(name, def.props, void 0, reg),
		description: def.description
	};
	return specs;
}
/** Function or action specs in the same shape as component specs. Undefined when there are none. */
function buildCallSpecs(calls, reg) {
	const entries = Object.entries(calls);
	if (!entries.length) return void 0;
	const specs = {};
	for (const [name, def] of entries) specs[name] = {
		signature: schemaSignature(name, def.params, "returns" in def ? def.returns : void 0, reg),
		description: def.description
	};
	return specs;
}
let fallbackLibraryId = 0;
function createLibraryId() {
	if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
	fallbackLibraryId += 1;
	return `openui-lib-${Date.now().toString(36)}-${fallbackLibraryId.toString(36)}`;
}
/**
* Create a component library from an array of defined components.
*/
function createLibrary(input) {
	const componentsRecord = {};
	const reg = z$1.registry();
	const __libraryId = createLibraryId();
	for (const comp of input.components) {
		reg.add(comp.props, { id: comp.name });
		componentsRecord[comp.name] = comp;
	}
	for (const [id, schema] of Object.entries(ACTION_DEFS)) if (!componentsRecord[id]) reg.add(schema, { id });
	if (input.root && !componentsRecord[input.root]) {
		const available = Object.keys(componentsRecord).join(", ");
		throw new Error(`[createLibrary] Root component "${input.root}" was not found in components. Available components: ${available}`);
	}
	const names = new Set(Object.keys(componentsRecord));
	const claim = (kind, name) => {
		if (isBuiltin(name) || isReservedCall(name) || names.has(name)) throw new Error(`[createLibrary] ${kind} "${name}" collides with a built-in, component, function, or action of the same name. Rename it.`);
		names.add(name);
	};
	const functionsRecord = {};
	for (const fn of input.functions ?? []) {
		claim("Function", fn.name);
		functionsRecord[fn.name] = fn;
	}
	const actionsRecord = {};
	for (const action of input.actions ?? []) {
		claim("Action", action.name);
		actionsRecord[action.name] = action;
		reg.add(action.ref, { id: action.name });
	}
	const callSpecs = () => {
		const functions = buildCallSpecs(functionsRecord, reg);
		const actions = buildCallSpecs(actionsRecord, reg);
		return {
			...functions ? { functions } : {},
			...actions ? { actions } : {}
		};
	};
	const library = {
		components: componentsRecord,
		componentGroups: input.componentGroups,
		functions: functionsRecord,
		actions: actionsRecord,
		root: input.root,
		id: input.id,
		__libraryId,
		prompt(options) {
			return generatePrompt({
				root: input.root,
				components: buildComponentSpecs(componentsRecord, reg),
				componentGroups: input.componentGroups,
				...callSpecs(),
				...options
			});
		},
		toSpec() {
			return {
				root: input.root,
				components: buildComponentSpecs(componentsRecord, reg),
				componentGroups: input.componentGroups,
				...callSpecs(),
				schema: buildJSONSchema(),
				...input.id !== void 0 ? { id: input.id } : {}
			};
		},
		toJSONSchema() {
			return buildJSONSchema();
		},
		extend(extension) {
			return createLibrary(extendDefinition(input, extension));
		}
	};
	function toCallJSONSchema(schema) {
		const { $schema: _, $defs: __, ...rest } = z$1.toJSONSchema(schema, { metadata: reg });
		return rest;
	}
	function callSchemas(calls) {
		const entries = Object.entries(calls);
		if (!entries.length) return void 0;
		return Object.fromEntries(entries.map(([name, def]) => [name, {
			description: def.description,
			params: toCallJSONSchema(def.params),
			..."returns" in def && def.returns ? { returns: toCallJSONSchema(def.returns) } : {}
		}]));
	}
	function buildJSONSchema() {
		const combinedSchema = object(Object.fromEntries(Object.entries(componentsRecord).map(([k, v]) => [k, v.props])));
		const schema = z$1.toJSONSchema(combinedSchema, { metadata: reg });
		for (const [name, comp] of Object.entries(componentsRecord)) {
			const def = schema.$defs?.[name];
			if (def && comp.description) def.description = comp.description;
		}
		const functions = callSchemas(functionsRecord);
		if (functions) schema.functions = functions;
		const actions = callSchemas(actionsRecord);
		if (actions) schema.actions = actions;
		return schema;
	}
	return library;
}
//#endregion
//#region src/message.ts
const PREFIX = "]]>openui:";
const MARKER = /^\]\]>openui:([a-z]+)(?:\?(.*))?$/;
const RETRY = /([^\n])(?=\]\]>openui:content(?:\?|\r?\n|$))/g;
const RETRY_CUT = /[^\n](\]\]>[a-z:]*)$/;
const encode = (s) => encodeURIComponent(s).replace(/[!'()~]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`).replace(/%20/g, "+");
const decode = (s) => {
	try {
		return decodeURIComponent(s.replace(/\+/g, " "));
	} catch {
		return s;
	}
};
function readAttributes(query = "") {
	const out = {};
	for (const pair of query.split("&")) {
		const eq = pair.indexOf("=");
		const key = decode(eq === -1 ? pair : pair.slice(0, eq));
		if (key) out[key] = eq === -1 ? "" : decode(pair.slice(eq + 1));
	}
	return out;
}
/** Splits a stored message into its content, context, attributes and end flag. */
function parseMessage(raw, { streaming } = {}) {
	let text = raw.replace(RETRY, "$1\n");
	const cut = streaming ? RETRY_CUT.exec(text) : null;
	if (cut && `${PREFIX}content`.startsWith(cut[1])) text = text.slice(0, cut.index + 1);
	const lines = text.split("\n");
	const last = lines[lines.length - 1];
	const held = streaming && last !== "" && (last.startsWith(PREFIX) || PREFIX.startsWith(last));
	if (held) lines.pop();
	const sections = [{
		kind: null,
		attributes: {},
		lines: []
	}];
	const close = () => {
		const body = sections[sections.length - 1].lines;
		if (body.length) body[body.length - 1] = body[body.length - 1].replace(/\r$/, "");
	};
	let endAt = -1;
	for (const [i, line] of lines.entries()) {
		const match = MARKER.exec(line.replace(/\r$/, ""));
		if (!match) {
			sections[sections.length - 1].lines.push(line);
			continue;
		}
		close();
		if (match[1] === "end") {
			endAt = sections.length - 1;
			if (i === lines.length - 2 && lines[i + 1] === "") break;
			continue;
		}
		sections.push({
			kind: match[1],
			attributes: readAttributes(match[2]),
			lines: []
		});
	}
	if (held) close();
	const kinds = sections.map((s) => s.kind);
	const win = Math.max(0, kinds.lastIndexOf("content"));
	const ctx = kinds.lastIndexOf("context");
	let context = ctx > win ? sections[ctx].lines.join("\n") : null;
	try {
		if (typeof context === "string") context = JSON.parse(context);
	} catch {}
	const winner = sections[win];
	const end = endAt >= win;
	return {
		content: winner.lines.join("\n"),
		context,
		attributes: winner.attributes,
		end
	};
}
/** Writes a stored message with its marker lines. */
function buildMessage({ content, context, attributes, end }) {
	const query = Object.entries(attributes ?? {}).map(([key, value]) => `${encode(key)}=${encode(value)}`).join("&");
	let out = `${PREFIX}content${query ? `?${query}` : ""}\n${content}`;
	if (context != null) out += `\n${PREFIX}context\n${typeof context === "string" ? context : JSON.stringify(context)}`;
	return end ? `${out}\n${PREFIX}end` : out;
}
//#endregion
//#region src/parser/types.ts
function isElementNode(value) {
	if (!value || typeof value !== "object" || Array.isArray(value)) return false;
	const node = value;
	return node.type === "element" && typeof node.typeName === "string" && typeof node.props === "object" && node.props !== null && typeof node.partial === "boolean";
}
/**
* Built-in action types for host app events.
*/
let BuiltinActionType = /* @__PURE__ */ function(BuiltinActionType) {
	BuiltinActionType["ContinueConversation"] = "continue_conversation";
	BuiltinActionType["OpenUrl"] = "open_url";
	return BuiltinActionType;
}({});
//#endregion
//#region src/parser/ast.ts
/** Type guard for runtime expression nodes that survive parser lowering. */
function isRuntimeExpr(node) {
	switch (node.k) {
		case "StateRef":
		case "RuntimeRef":
		case "BinOp":
		case "UnaryOp":
		case "Ternary":
		case "Member":
		case "Index":
		case "Assign": return true;
		default: return false;
	}
}
/** Valid AST discriminant values. */
const AST_KINDS = new Set([
	"Comp",
	"Ref",
	"StateRef",
	"RuntimeRef",
	"BinOp",
	"UnaryOp",
	"Ternary",
	"Member",
	"Index",
	"Assign",
	"Str",
	"Num",
	"Bool",
	"Null",
	"Arr",
	"Obj",
	"Ph"
]);
/** Check if a value is an AST node (has a valid `k` discriminant field). */
function isASTNode(value) {
	if (!value || typeof value !== "object" || Array.isArray(value)) return false;
	return AST_KINDS.has(value.k);
}
function walkAST(node, visit) {
	const walk = (current) => {
		visit(current);
		switch (current.k) {
			case "Comp":
				current.args.forEach(walk);
				Object.values(current.mappedProps ?? {}).forEach(walk);
				break;
			case "Arr":
				current.els.forEach(walk);
				break;
			case "Obj":
				current.entries.forEach(([, value]) => walk(value));
				break;
			case "BinOp":
				walk(current.left);
				walk(current.right);
				break;
			case "UnaryOp":
				walk(current.operand);
				break;
			case "Ternary":
				walk(current.cond);
				walk(current.then);
				walk(current.else);
				break;
			case "Member":
				walk(current.obj);
				break;
			case "Index":
				walk(current.obj);
				walk(current.index);
				break;
			case "Assign":
				walk(current.value);
				break;
		}
	};
	walk(node);
}
function toLiteralAST(value) {
	if (value === null || value === void 0) return { k: "Null" };
	if (typeof value === "string") return {
		k: "Str",
		v: value
	};
	if (typeof value === "number") return {
		k: "Num",
		v: value
	};
	if (typeof value === "boolean") return {
		k: "Bool",
		v: value
	};
	if (Array.isArray(value)) return {
		k: "Arr",
		els: value.map(toLiteralAST)
	};
	if (typeof value === "object") return {
		k: "Obj",
		entries: Object.entries(value).map(([k, v]) => [k, toLiteralAST(v)])
	};
	return { k: "Null" };
}
function literalValue(node) {
	switch (node.k) {
		case "Str":
		case "Num":
		case "Bool": return { v: node.v };
		case "Null": return { v: null };
		case "Arr": {
			const v = [];
			for (const e of node.els) {
				const lit = literalValue(e);
				if (!lit) return void 0;
				v.push(lit.v);
			}
			return { v };
		}
		case "Obj": {
			const v = {};
			for (const [k, e] of node.entries) {
				const lit = literalValue(e);
				if (!lit) return void 0;
				v[k] = lit.v;
			}
			return { v };
		}
		default: return;
	}
}
//#endregion
//#region src/parser/expressions.ts
const PREC_TERNARY = 1;
const PREC_OR = 2;
const PREC_AND = 3;
const PREC_EQ = 4;
const PREC_CMP = 5;
const PREC_ADD = 6;
const PREC_MUL = 7;
const PREC_UNARY = 8;
const PREC_MEMBER = 9;
/**
* Parse a token array into an AST node using a Pratt (top-down operator
* precedence) parser.
*/
function parseExpression(tokens) {
	let pos = 0;
	const cur = () => tokens[pos] ?? { t: 13 };
	const adv = () => {
		const tok = cur();
		pos++;
		return tok;
	};
	const eat = (kind) => {
		if (cur().t === kind) pos++;
	};
	function getInfixPrec(tok) {
		switch (tok.t) {
			case 34: return PREC_TERNARY;
			case 32: return PREC_OR;
			case 31: return PREC_AND;
			case 25:
			case 26: return PREC_EQ;
			case 27:
			case 28:
			case 29:
			case 30: return PREC_CMP;
			case 20:
			case 21: return PREC_ADD;
			case 22:
			case 23:
			case 24: return PREC_MUL;
			case 19:
			case 3: return PREC_MEMBER;
			default: return 0;
		}
	}
	function parseExpr(minPrec = 0) {
		let left = parsePrefix();
		while (getInfixPrec(cur()) > minPrec) left = parseInfix(left);
		return left;
	}
	function parsePrefix() {
		const tok = cur();
		if (tok.t === 14) {
			adv();
			return {
				k: "Str",
				v: tok.v
			};
		}
		if (tok.t === 15) {
			adv();
			return {
				k: "Num",
				v: tok.v
			};
		}
		if (tok.t === 10) {
			adv();
			return {
				k: "Bool",
				v: true
			};
		}
		if (tok.t === 11) {
			adv();
			return {
				k: "Bool",
				v: false
			};
		}
		if (tok.t === 12) {
			adv();
			return { k: "Null" };
		}
		if (tok.t === 3) return parseArr();
		if (tok.t === 5) return parseObj();
		if (tok.t === 18) {
			const name = tok.v;
			adv();
			if (cur().t === 9) {
				adv();
				return {
					k: "Assign",
					target: name,
					value: parseExpr(0)
				};
			}
			return {
				k: "StateRef",
				n: name
			};
		}
		if (tok.t === 17) {
			const name = tok.v;
			if (tokens[pos + 1]?.t === 1 && (!isBuiltin(name) || name === "Action")) return parseComp();
			adv();
			return {
				k: "Ref",
				n: name
			};
		}
		if (tok.t === 35) {
			if (tokens[pos + 1]?.t === 1) {
				const call = parseComp();
				if (call.k === "Comp" && !isBuiltin(call.name)) call.fn = true;
				return call;
			}
			adv();
			return {
				k: "Ref",
				n: tok.v
			};
		}
		if (tok.t === 16) {
			adv();
			return {
				k: "Ref",
				n: tok.v
			};
		}
		if (tok.t === 33) {
			adv();
			return {
				k: "UnaryOp",
				op: "!",
				operand: parseExpr(PREC_UNARY)
			};
		}
		if (tok.t === 21) {
			adv();
			return {
				k: "UnaryOp",
				op: "-",
				operand: parseExpr(PREC_UNARY)
			};
		}
		if (tok.t === 1) {
			adv();
			const inner = parseExpr(0);
			eat(2);
			return inner;
		}
		adv();
		return { k: "Null" };
	}
	function parseInfix(left) {
		const tok = cur();
		if (tok.t === 20) {
			adv();
			return {
				k: "BinOp",
				op: "+",
				left,
				right: parseExpr(PREC_ADD)
			};
		}
		if (tok.t === 21) {
			adv();
			return {
				k: "BinOp",
				op: "-",
				left,
				right: parseExpr(PREC_ADD)
			};
		}
		if (tok.t === 22) {
			adv();
			return {
				k: "BinOp",
				op: "*",
				left,
				right: parseExpr(PREC_MUL)
			};
		}
		if (tok.t === 23) {
			adv();
			return {
				k: "BinOp",
				op: "/",
				left,
				right: parseExpr(PREC_MUL)
			};
		}
		if (tok.t === 24) {
			adv();
			return {
				k: "BinOp",
				op: "%",
				left,
				right: parseExpr(PREC_MUL)
			};
		}
		if (tok.t === 25) {
			adv();
			return {
				k: "BinOp",
				op: "==",
				left,
				right: parseExpr(PREC_EQ)
			};
		}
		if (tok.t === 26) {
			adv();
			return {
				k: "BinOp",
				op: "!=",
				left,
				right: parseExpr(PREC_EQ)
			};
		}
		if (tok.t === 27) {
			adv();
			return {
				k: "BinOp",
				op: ">",
				left,
				right: parseExpr(PREC_CMP)
			};
		}
		if (tok.t === 28) {
			adv();
			return {
				k: "BinOp",
				op: "<",
				left,
				right: parseExpr(PREC_CMP)
			};
		}
		if (tok.t === 29) {
			adv();
			return {
				k: "BinOp",
				op: ">=",
				left,
				right: parseExpr(PREC_CMP)
			};
		}
		if (tok.t === 30) {
			adv();
			return {
				k: "BinOp",
				op: "<=",
				left,
				right: parseExpr(PREC_CMP)
			};
		}
		if (tok.t === 31) {
			adv();
			return {
				k: "BinOp",
				op: "&&",
				left,
				right: parseExpr(PREC_AND)
			};
		}
		if (tok.t === 32) {
			adv();
			return {
				k: "BinOp",
				op: "||",
				left,
				right: parseExpr(PREC_OR)
			};
		}
		if (tok.t === 34) {
			adv();
			const then = parseExpr(0);
			eat(8);
			return {
				k: "Ternary",
				cond: left,
				then,
				else: parseExpr(0)
			};
		}
		if (tok.t === 19) {
			adv();
			const fieldTok = cur();
			return {
				k: "Member",
				obj: left,
				field: fieldTok.t === 16 || fieldTok.t === 17 || fieldTok.t === 14 || fieldTok.t === 15 ? (adv(), String(fieldTok.v)) : fieldTok.t === 18 ? (adv(), fieldTok.v.replace(/^\$/, "")) : (adv(), "?")
			};
		}
		if (tok.t === 3) {
			adv();
			const index = parseExpr(0);
			eat(4);
			return {
				k: "Index",
				obj: left,
				index
			};
		}
		return left;
	}
	/** Parse `TypeName(arg1, arg2, ...)` */
	function parseComp() {
		const name = cur().v;
		adv();
		eat(1);
		const args = [];
		while (cur().t !== 2 && cur().t !== 13) {
			args.push(parseExpr(0));
			if (cur().t === 7) adv();
		}
		eat(2);
		return {
			k: "Comp",
			name,
			args
		};
	}
	/** Parse `[elem1, elem2, ...]` */
	function parseArr() {
		adv();
		const els = [];
		while (cur().t !== 4 && cur().t !== 13) {
			els.push(parseExpr(0));
			if (cur().t === 7) adv();
		}
		eat(4);
		return {
			k: "Arr",
			els
		};
	}
	/** Parse `{ key: value, ... }` */
	function parseObj() {
		adv();
		const entries = [];
		while (cur().t !== 6 && cur().t !== 13) {
			const kt = cur();
			const key = kt.t === 16 || kt.t === 14 || kt.t === 17 || kt.t === 15 ? (adv(), String(kt.v)) : kt.t === 18 ? (adv(), kt.v.replace(/^\$/, "")) : (adv(), "?");
			eat(8);
			entries.push([key, parseExpr(0)]);
			if (cur().t === 7) adv();
		}
		eat(6);
		return {
			k: "Obj",
			entries
		};
	}
	return parseExpr(0);
}
//#endregion
//#region src/parser/lexer.ts
/**
* Tokenize an openui-lang source string into a flat token array.
*
* Handles all token types: identifiers, literals, operators,
* state variables ($name), dot access, ternary.
*/
function tokenize(src) {
	const tokens = [];
	let i = 0;
	const n = src.length;
	while (i < n) {
		while (i < n && (src[i] === " " || src[i] === "	" || src[i] === "\r")) i++;
		if (i >= n) break;
		const c = src[i];
		if (c === "\n") {
			tokens.push({ t: 0 });
			i++;
			continue;
		}
		if (c === "(") {
			tokens.push({ t: 1 });
			i++;
			continue;
		}
		if (c === ")") {
			tokens.push({ t: 2 });
			i++;
			continue;
		}
		if (c === "[") {
			tokens.push({ t: 3 });
			i++;
			continue;
		}
		if (c === "]") {
			tokens.push({ t: 4 });
			i++;
			continue;
		}
		if (c === "{") {
			tokens.push({ t: 5 });
			i++;
			continue;
		}
		if (c === "}") {
			tokens.push({ t: 6 });
			i++;
			continue;
		}
		if (c === ",") {
			tokens.push({ t: 7 });
			i++;
			continue;
		}
		if (c === ":") {
			tokens.push({ t: 8 });
			i++;
			continue;
		}
		if (c === "=") {
			if (i + 1 < n && src[i + 1] === "=") {
				tokens.push({ t: 25 });
				i += 2;
			} else {
				tokens.push({ t: 9 });
				i++;
			}
			continue;
		}
		if (c === "!") {
			if (i + 1 < n && src[i + 1] === "=") {
				tokens.push({ t: 26 });
				i += 2;
			} else {
				tokens.push({ t: 33 });
				i++;
			}
			continue;
		}
		if (c === ">") {
			if (i + 1 < n && src[i + 1] === "=") {
				tokens.push({ t: 29 });
				i += 2;
			} else {
				tokens.push({ t: 27 });
				i++;
			}
			continue;
		}
		if (c === "<") {
			if (i + 1 < n && src[i + 1] === "=") {
				tokens.push({ t: 30 });
				i += 2;
			} else {
				tokens.push({ t: 28 });
				i++;
			}
			continue;
		}
		if (c === "&") {
			if (i + 1 < n && src[i + 1] === "&") {
				tokens.push({ t: 31 });
				i += 2;
			} else {
				tokens.push({ t: 31 });
				i++;
			}
			continue;
		}
		if (c === "|") {
			if (i + 1 < n && src[i + 1] === "|") {
				tokens.push({ t: 32 });
				i += 2;
			} else {
				tokens.push({ t: 32 });
				i++;
			}
			continue;
		}
		if (c === ".") {
			tokens.push({ t: 19 });
			i++;
			continue;
		}
		if (c === "?") {
			tokens.push({ t: 34 });
			i++;
			continue;
		}
		if (c === "+") {
			tokens.push({ t: 20 });
			i++;
			continue;
		}
		if (c === "*") {
			tokens.push({ t: 22 });
			i++;
			continue;
		}
		if (c === "/") {
			tokens.push({ t: 23 });
			i++;
			continue;
		}
		if (c === "%") {
			tokens.push({ t: 24 });
			i++;
			continue;
		}
		if (c === "\"") {
			const start = i;
			i++;
			let isClosed = false;
			while (i < n) if (src[i] === "\\") i += 2;
			else if (src[i] === "\"") {
				i++;
				isClosed = true;
				break;
			} else i++;
			const rawString = src.slice(start, i);
			try {
				const validJsonString = isClosed ? rawString : rawString + "\"";
				tokens.push({
					t: 14,
					v: JSON.parse(validJsonString)
				});
			} catch {
				const stripped = rawString.replace(/^"|"$/g, "");
				tokens.push({
					t: 14,
					v: stripped
				});
			}
			continue;
		}
		if (c === "'") {
			i++;
			let result = "";
			while (i < n) if (src[i] === "\\") {
				i++;
				if (i < n) {
					const esc = src[i];
					if (esc === "'") result += "'";
					else if (esc === "\\") result += "\\";
					else if (esc === "n") result += "\n";
					else if (esc === "t") result += "	";
					else result += esc;
					i++;
				}
			} else if (src[i] === "'") {
				i++;
				break;
			} else {
				result += src[i];
				i++;
			}
			tokens.push({
				t: 14,
				v: result
			});
			continue;
		}
		if (c === "-") {
			const prev = tokens.length > 0 ? tokens[tokens.length - 1] : null;
			if (!(prev != null && (prev.t === 15 || prev.t === 14 || prev.t === 16 || prev.t === 17 || prev.t === 2 || prev.t === 4 || prev.t === 10 || prev.t === 11 || prev.t === 12 || prev.t === 18 || prev.t === 35)) && i + 1 < n && src[i + 1] >= "0" && src[i + 1] <= "9") {} else {
				tokens.push({ t: 21 });
				i++;
				continue;
			}
		}
		const isDigit = c >= "0" && c <= "9";
		const isNegDigit = c === "-" && i + 1 < n && src[i + 1] >= "0" && src[i + 1] <= "9";
		if (isDigit || isNegDigit) {
			const start = i;
			if (src[i] === "-") i++;
			while (i < n && src[i] >= "0" && src[i] <= "9") i++;
			if (i < n && src[i] === "." && i + 1 < n && src[i + 1] >= "0" && src[i + 1] <= "9") {
				i++;
				while (i < n && src[i] >= "0" && src[i] <= "9") i++;
			}
			if (i < n && (src[i] === "e" || src[i] === "E")) {
				i++;
				if (i < n && (src[i] === "+" || src[i] === "-")) i++;
				while (i < n && src[i] >= "0" && src[i] <= "9") i++;
			}
			tokens.push({
				t: 15,
				v: +src.slice(start, i)
			});
			continue;
		}
		if (c === "$" && i + 1 < n && (src[i + 1] >= "a" && src[i + 1] <= "z" || src[i + 1] >= "A" && src[i + 1] <= "Z" || src[i + 1] === "_")) {
			const start = i;
			i++;
			while (i < n && (src[i] >= "a" && src[i] <= "z" || src[i] >= "A" && src[i] <= "Z" || src[i] >= "0" && src[i] <= "9" || src[i] === "_")) i++;
			tokens.push({
				t: 18,
				v: src.slice(start, i)
			});
			continue;
		}
		if (c >= "a" && c <= "z" || c >= "A" && c <= "Z" || c === "_") {
			const start = i;
			while (i < n && (src[i] >= "a" && src[i] <= "z" || src[i] >= "A" && src[i] <= "Z" || src[i] >= "0" && src[i] <= "9" || src[i] === "_")) i++;
			const word = src.slice(start, i);
			if (word === "true") {
				tokens.push({ t: 10 });
				continue;
			}
			if (word === "false") {
				tokens.push({ t: 11 });
				continue;
			}
			if (word === "null") {
				tokens.push({ t: 12 });
				continue;
			}
			const kind = c >= "A" && c <= "Z" ? 17 : 16;
			tokens.push({
				t: kind,
				v: word
			});
			continue;
		}
		if (c === "@" && i + 1 < n && (src[i + 1] >= "a" && src[i + 1] <= "z" || src[i + 1] >= "A" && src[i + 1] <= "Z" || src[i + 1] === "_")) {
			i++;
			const start = i;
			while (i < n && (src[i] >= "a" && src[i] <= "z" || src[i] >= "A" && src[i] <= "Z" || src[i] >= "0" && src[i] <= "9" || src[i] === "_")) i++;
			tokens.push({
				t: 35,
				v: src.slice(start, i)
			});
			continue;
		}
		i++;
	}
	tokens.push({ t: 13 });
	return tokens;
}
//#endregion
//#region src/parser/validation.ts
/** `typeof` results a scalar/enum leaf can be checked against. */
const SCALAR_TYPEOFS = [
	"string",
	"number",
	"boolean"
];
const NO_PROPS = {};
const NO_REQUIRED = [];
function jsType(value) {
	return Array.isArray(value) ? "array" : typeof value;
}
/** `{ $ref: "#/$defs/Name" }` → `"Name"`. */
function refName(s) {
	return typeof s["$ref"] === "string" ? s["$ref"].split("/").pop() : void 0;
}
/** Composite shapes can't be reliably matched to a single plain value. */
function isCompositeSchema(s) {
	return "$ref" in s || "anyOf" in s || "oneOf" in s || "allOf" in s;
}
/** Extract the scalar leaf type/enum from an already-narrowed JSON Schema object. */
function getScalarTypeInfo(s) {
	if (Array.isArray(s["enum"])) return { enumValues: s["enum"] };
	if ("const" in s) return { enumValues: [s["const"]] };
	switch (s["type"]) {
		case "string": return { expectedType: "string" };
		case "number":
		case "integer": return { expectedType: "number" };
		case "boolean": return { expectedType: "boolean" };
		default: return {};
	}
}
/** Read a JSON Schema fragment's `default`, if it carries one. */
function getSchemaDefaultValue(property) {
	if (!property || typeof property !== "object" || Array.isArray(property)) return;
	return property.default;
}
/** Returns the expected type for a param from its schema properties */
function getTypeFromSchema(property) {
	if (!property || typeof property !== "object" || Array.isArray(property)) return;
	const p = property;
	const ref = refName(p);
	if (ref) return ref;
	const leaf = getScalarTypeInfo(p);
	if (leaf.enumValues) return leaf.enumValues.map((v) => JSON.stringify(v)).join("|");
	if (leaf.expectedType) return leaf.expectedType;
	return typeof p.type === "string" ? p.type : void 0;
}
/**
* Build a typed signature like `Header(title*: string, variant: "a"|"b")` from
* compiled params — * marks required. Positional order is preserved so an LLM
* can fix swapped args.
*/
function buildParamsSignature(component, params) {
	return `${component}(${params.map((p) => {
		const type = getTypeFromSchema(p.schema);
		const marked = p.required ? `${p.name}*` : p.name;
		return type ? `${marked}: ${type}` : marked;
	}).join(", ")})`;
}
/** Checks a param's returned value against its expected value */
function checkTypeMismatch(value, info) {
	const actual = typeof value;
	if (info.enumValues) {
		if (!SCALAR_TYPEOFS.includes(actual)) return null;
		if (info.enumValues.includes(value)) return null;
		return {
			expected: `one of [${info.enumValues.map((v) => JSON.stringify(v)).join(", ")}]`,
			actual: JSON.stringify(value)
		};
	}
	if (info.expectedType && info.expectedType !== actual) return {
		expected: info.expectedType,
		actual: jsType(value)
	};
	return null;
}
function validationMessage(component, path, issue) {
	switch (issue.code) {
		case "type-mismatch": return `field "${path}" expects ${issue.expected} but got ${issue.actual}`;
		case "missing-required": return `missing required field "${path}"${issue.signature ? ` — signature: ${issue.signature}` : ""}`;
		case "null-required": return `required field "${path}" cannot be null${issue.signature ? ` — signature: ${issue.signature}` : ""}`;
		case "unknown-component": return `Unknown component "${component}" — not found in catalog or builtins${issue.available?.length ? `. Available components: ${issue.available.join(", ")}` : ""}`;
		case "inline-reserved": return `${component}() must be declared as a top-level statement, not used inline as a value`;
		case "excess-args": return `${component} takes ${issue.declared} arg(s), got ${issue.got} (${issue.got - issue.declared} excess dropped)`;
		case "unknown-function": return `Unknown function "@${component}": not a built-in or action step. The call evaluates to null`;
		case "no-root":
			if (issue.reason === "not-component") return "root does not evaluate to a component";
			if (issue.reason === "fallback") return `No root statement. Rendering the first statement that calls the root component ${component}`;
			return component ? `No root statement, and no statement calls the root component ${component}` : "No root statement";
	}
}
/** Append a validation error, deriving its message from the structured issue. */
function pushValidationIssue(ctx, component, path, issue) {
	ctx.errors.push({
		code: issue.code,
		component,
		path,
		message: validationMessage(component, path, issue),
		statementId: ctx.currentStatementId,
		...issue.code === "no-root" && issue.reason === "fallback" && { severity: "warning" }
	});
}
const INVALID = Symbol("invalid");
function resolveInvalidValue(required, defaultValue) {
	if (defaultValue !== void 0) return defaultValue;
	return required ? INVALID : void 0;
}
/**
* Position check for a component element in a data slot: slots that admit
* components ($ref / anyOf / oneOf members) are left unchecked, a slot
* declaring a plain-data shape can never be satisfied by a component, and
* unconstrained slots stay conservative.
*/
function validateElementPosition(element, s, component, path, ctx) {
	if (isCompositeSchema(s)) return false;
	if (typeof s["type"] === "string" || Array.isArray(s["enum"]) || "const" in s) {
		pushValidationIssue(ctx, component, path, {
			code: "type-mismatch",
			expected: typeof s["type"] === "string" ? s["type"] : "a literal value",
			actual: `component "${element.typeName}"`
		});
		return true;
	}
	return false;
}
/** The `$ref` names a slot allows: one for `$ref`, several for anyOf/oneOf. */
function slotRefNames(s) {
	return (s["anyOf"] ?? s["oneOf"] ?? [s]).map(refName);
}
/** True when every option of the slot is a `$ref` to a component. */
function isOnlyComponentSlot(s, ctx) {
	const names = slotRefNames(s);
	return names.length > 0 && names.every((n) => n !== void 0 && ctx.cat?.has(n));
}
/**
* Validate an object-shaped slot: container type, required keys (absent/null
* ones fill silently from their schema default), then each declared property
* recursively, resolving invalid children via the edge rule. Copies the object
* only when a key changes.
*/
function validateObjectValue(value, s, component, path, ctx) {
	if (typeof value !== "object" || Array.isArray(value)) {
		pushValidationIssue(ctx, component, path, {
			code: "type-mismatch",
			expected: "object",
			actual: jsType(value)
		});
		return INVALID;
	}
	const obj = value;
	let out;
	const props = s["properties"] && typeof s["properties"] === "object" ? s["properties"] : NO_PROPS;
	const required = Array.isArray(s["required"]) ? s["required"] : NO_REQUIRED;
	let invalid = false;
	if (!ctx.partial) for (const key of required) {
		const present = key in obj;
		if (!present || obj[key] == null) {
			const fallback = getSchemaDefaultValue(props[key]);
			if (fallback !== void 0) {
				(out ??= { ...obj })[key] = fallback;
				continue;
			}
			pushValidationIssue(ctx, component, `${path}/${key}`, { code: present ? "null-required" : "missing-required" });
			invalid = true;
		}
	}
	for (const key of Object.keys(props)) {
		if (!(key in obj)) continue;
		const sub = props[key];
		let next = validateSchemaValue(obj[key], sub, component, `${path}/${key}`, ctx);
		if (next === INVALID) {
			next = resolveInvalidValue(required.includes(key), getSchemaDefaultValue(sub));
			if (next === INVALID) {
				invalid = true;
				continue;
			}
		}
		if (next === obj[key]) continue;
		out ??= { ...obj };
		if (next === void 0) delete out[key];
		else out[key] = next;
	}
	return invalid ? INVALID : out ?? obj;
}
/**
* Validate an array slot. Invalid items are pruned; error paths keep the
* original indices. Copies the array only when an item changes.
*/
function validateArrayValue(value, s, component, path, ctx) {
	if (!Array.isArray(value)) {
		pushValidationIssue(ctx, component, path, {
			code: "type-mismatch",
			expected: "array",
			actual: jsType(value)
		});
		return INVALID;
	}
	const items = s["items"];
	if (!items || typeof items !== "object" || Array.isArray(items)) return value;
	let out;
	for (let i = 0; i < value.length; i++) {
		const next = validateSchemaValue(value[i], items, component, `${path}/${i}`, ctx);
		if (next !== value[i] && !out) out = value.slice(0, i);
		if (out && next !== INVALID) out.push(next);
	}
	return out ?? value;
}
/**
* Validate a scalar/enum leaf. Enum membership defers while streaming — a
* partial literal may still complete toward a valid member; scalar type
* checks stay on (a wrong type won't become right with more input).
*/
function validateLeafValue(value, s, component, path, ctx) {
	const leaf = getScalarTypeInfo(s);
	if (leaf.expectedType == null && leaf.enumValues == null) return false;
	if (leaf.enumValues && ctx.partial) return false;
	const mismatch = checkTypeMismatch(value, leaf);
	if (mismatch) {
		pushValidationIssue(ctx, component, path, {
			code: "type-mismatch",
			...mismatch
		});
		return true;
	}
	return false;
}
/**
* Recursively validate a materialized value against a JSON Schema fragment.
* Dispatches on the schema shape: objects and arrays descend recursively
* (reporting nested errors with JSON-pointer paths), leaf scalars check
* type/enum, and component elements get a position check only (their own args
* validate separately).
*
* Every violation is reported, then the same rule (resolveInvalidValue)
* applies at each recursion edge: schema default → substitute; REQUIRED edge →
* parent invalid, caller repeats one level up; OPTIONAL edge → prune. ARRAY
* ITEMS are always pruned. Absent/null required keys fill silently from their
* schema default; reported violations keep their error even when a default steps in.
*
* Never mutates: returns the value itself when nothing changes, a cleaned copy
* when something does, or INVALID.
*/
function validateSchemaValue(value, schema, component, path, ctx) {
	if (!schema || typeof schema !== "object" || Array.isArray(schema)) return value;
	const s = schema;
	if (value == null) return value;
	if (isASTNode(value)) return value;
	if (isElementNode(value)) return validateElementPosition(value, s, component, path, ctx) ? INVALID : value;
	if (isCompositeSchema(s)) {
		if (!isOnlyComponentSlot(s, ctx)) return value;
		pushValidationIssue(ctx, component, path, {
			code: "type-mismatch",
			expected: slotRefNames(s).join(" | "),
			actual: Array.isArray(value) ? "array" : typeof value === "object" ? "plain object" : typeof value
		});
		return INVALID;
	}
	const type = s["type"];
	if (type === "object") return validateObjectValue(value, s, component, path, ctx);
	if (type === "array") return validateArrayValue(value, s, component, path, ctx);
	return validateLeafValue(value, s, component, path, ctx) ? INVALID : value;
}
//#endregion
//#region src/parser/materialize.ts
/**
* Recursively check if a prop value contains any AST nodes that need runtime
* evaluation. Walks into arrays, ElementNode children, and plain objects.
*/
function containsDynamicValue(v) {
	if (v == null || typeof v !== "object") return false;
	if (isASTNode(v)) return true;
	if (Array.isArray(v)) return v.some(containsDynamicValue);
	if (isElementNode(v)) return Object.values(v.props).some(containsDynamicValue);
	const obj = v;
	if (Array.isArray(obj.steps)) return true;
	return Object.values(obj).some(containsDynamicValue);
}
/**
* Resolve a Ref node: inline from symbol table, detect cycles, emit RuntimeRef
* for Query/Mutation declarations. Shared by materializeValue and materializeExpr.
*/
function resolveRef(name, ctx, mode) {
	if (ctx.visited.has(name)) {
		ctx.unres.push(name);
		return mode === "expr" ? {
			k: "Ph",
			n: name
		} : null;
	}
	if (!ctx.syms.has(name)) {
		ctx.unres.push(name);
		return mode === "expr" ? {
			k: "Ph",
			n: name
		} : null;
	}
	const target = ctx.syms.get(name);
	ctx.unreached?.delete(name);
	if (target.k === "Comp" && isReservedCall(target.name)) return {
		k: "RuntimeRef",
		n: name,
		refType: target.name === RESERVED_CALLS.Mutation ? "mutation" : "query"
	};
	ctx.visited.add(name);
	const prevStatementId = ctx.currentStatementId;
	ctx.currentStatementId = name;
	try {
		const result = mode === "value" ? materializeValue(target, ctx) : materializeExpr(target, ctx);
		if (mode === "value" && isElementNode(result)) result.statementId = name;
		return result;
	} finally {
		ctx.currentStatementId = prevStatementId;
		ctx.visited.delete(name);
	}
}
/** Registry calls (functions and steps) and reserved calls stay AST for the runtime. */
function isRuntimeCall(name, ctx) {
	return getCallDefs(ctx.cat).has(name) || isReservedCall(name);
}
function nameArgs(args, params) {
	const named = {};
	for (let i = 0; i < params.length && i < args.length; i++) named[params[i].name] = args[i];
	return named;
}
function mapCallArgs(name, args, def, ctx) {
	const { params } = def;
	if (args.length > params.length) pushValidationIssue(ctx, name, "", {
		code: "excess-args",
		declared: params.length,
		got: args.length
	});
	const mapped = nameArgs(args, params);
	let invalid = false;
	const errorsBefore = ctx.errors.length;
	for (const p of params) {
		const arg = mapped[p.name];
		const lit = arg ? literalValue(arg) : { v: void 0 };
		if (!lit) continue;
		if (lit.v == null) {
			if (!p.required || p.defaultValue !== void 0) continue;
			pushValidationIssue(ctx, name, `/${p.name}`, {
				code: arg ? "null-required" : "missing-required",
				signature: buildParamsSignature(name, params)
			});
			invalid = true;
			continue;
		}
		let next = validateSchemaValue(lit.v, p.schema, name, `/${p.name}`, ctx);
		if (next === INVALID) next = resolveInvalidValue(p.required, p.defaultValue);
		if (next === INVALID) invalid = true;
		else if (next === void 0) delete mapped[p.name];
		else if (next !== lit.v) mapped[p.name] = toLiteralAST(next);
	}
	if (def.kind === "action" && ctx.errors.length > errorsBefore) invalid = true;
	return invalid ? null : mapped;
}
function materializeCall(node, ctx, materializeArg) {
	const args = node.args.map(materializeArg);
	const def = getCallDefs(ctx.cat).get(node.name);
	if (!def || def.lazy) return {
		...node,
		args
	};
	const mappedProps = def.builtin ? nameArgs(args, def.params) : mapCallArgs(node.name, args, def, ctx);
	if (mappedProps) return {
		...node,
		args,
		mappedProps
	};
	return def.kind === "action" ? {
		k: "Comp",
		name: "Action",
		args: [{
			k: "Arr",
			els: []
		}]
	} : { k: "Null" };
}
function nameRawCallArgs(args, ctx, statementId) {
	const prevStatementId = ctx.currentStatementId;
	ctx.currentStatementId = statementId;
	const seen = /* @__PURE__ */ new Set();
	for (const arg of args) walkAST(arg, (node) => {
		if (node.k !== "Comp" || seen.has(node)) return;
		seen.add(node);
		const def = getCallDefs(ctx.cat).get(node.name);
		if (!def || def.lazy) return;
		node.mappedProps = (def.builtin ? null : mapCallArgs(node.name, node.args, def, ctx)) ?? nameArgs(node.args, def.params);
	});
	ctx.currentStatementId = prevStatementId;
}
/**
* If node is a lazy builtin like Each(arr, varName, template), temporarily
* scope the iterator variable during materialization so template refs resolve.
* Returns the materialized Comp node, or null if not a lazy builtin.
*/
function materializeLazyBuiltin(node, ctx, scopedRefs) {
	const def = getCallDefs(ctx.cat).get(node.name);
	if (def?.kind !== "function" || !def.lazy || node.args.length < 3) return null;
	const varArg = node.args[1];
	const varName = varArg.k === "Ref" ? varArg.n : varArg.k === "Str" ? varArg.v : null;
	if (!varName) return null;
	const nextScopedRefs = new Set(scopedRefs);
	nextScopedRefs.add(varName);
	const recursedArgs = node.args.map((a, i) => i === 1 ? a : materializeExprInternal(a, ctx, nextScopedRefs));
	return {
		...node,
		args: recursedArgs
	};
}
function materializeExprInternal(node, ctx, scopedRefs) {
	switch (node.k) {
		case "Ref": return scopedRefs.has(node.n) ? node : resolveRef(node.n, ctx, "expr");
		case "Ph": return node;
		case "Comp": {
			if (node.fn && !isRuntimeCall(node.name, ctx)) {
				pushValidationIssue(ctx, node.name, "", { code: "unknown-function" });
				return { k: "Null" };
			}
			const lazy = materializeLazyBuiltin(node, ctx, scopedRefs);
			if (lazy) return lazy;
			if (isRuntimeCall(node.name, ctx)) return materializeCall(node, ctx, (a) => materializeExprInternal(a, ctx, scopedRefs));
			const recursedArgs = node.args.map((a) => materializeExprInternal(a, ctx, scopedRefs));
			const def = ctx.cat?.get(node.name);
			if (def) {
				const mappedProps = {};
				for (let i = 0; i < def.params.length && i < recursedArgs.length; i++) mappedProps[def.params[i].name] = recursedArgs[i];
				return {
					...node,
					args: recursedArgs,
					mappedProps
				};
			}
			pushValidationIssue(ctx, node.name, "", {
				code: "unknown-component",
				available: ctx.cat && [...ctx.cat.keys()]
			});
			return {
				...node,
				args: recursedArgs
			};
		}
		case "Arr": return {
			...node,
			els: node.els.map((e) => materializeExprInternal(e, ctx, scopedRefs))
		};
		case "Obj": return {
			...node,
			entries: node.entries.map(([k, v]) => [k, materializeExprInternal(v, ctx, scopedRefs)])
		};
		case "BinOp": return {
			...node,
			left: materializeExprInternal(node.left, ctx, scopedRefs),
			right: materializeExprInternal(node.right, ctx, scopedRefs)
		};
		case "UnaryOp": return {
			...node,
			operand: materializeExprInternal(node.operand, ctx, scopedRefs)
		};
		case "Ternary": return {
			...node,
			cond: materializeExprInternal(node.cond, ctx, scopedRefs),
			then: materializeExprInternal(node.then, ctx, scopedRefs),
			else: materializeExprInternal(node.else, ctx, scopedRefs)
		};
		case "Member": return {
			...node,
			obj: materializeExprInternal(node.obj, ctx, scopedRefs)
		};
		case "Index": return {
			...node,
			obj: materializeExprInternal(node.obj, ctx, scopedRefs),
			index: materializeExprInternal(node.index, ctx, scopedRefs)
		};
		case "Assign": return {
			...node,
			value: materializeExprInternal(node.value, ctx, scopedRefs)
		};
		default: return node;
	}
}
/**
* Normalize an AST node for use inside runtime expressions.
* Resolves Refs, adds mappedProps to catalog Comp nodes.
* Returns ASTNode — structure preserved for runtime evaluation by the evaluator.
*/
function materializeExpr(node, ctx) {
	return materializeExprInternal(node, ctx, /* @__PURE__ */ new Set());
}
/**
* Schema-aware materialization: resolves refs, normalizes catalog component args
* to named props, validates required props, applies defaults, converts literals
* to plain values, and preserves runtime expressions as AST nodes — all in a
* single recursive traversal.
*
* Returns:
*   - ElementNode for catalog/unknown components
*   - ASTNode for builtins and runtime expression nodes
*   - Plain values for literals, arrays, objects
*   - null for placeholders
*/
function materializeValue(node, ctx) {
	switch (node.k) {
		case "Ref": return resolveRef(node.n, ctx, "value");
		case "Str": return node.v;
		case "Num": return node.v;
		case "Bool": return node.v;
		case "Null": return null;
		case "Ph": return null;
		case "Arr": {
			const items = [];
			for (const e of node.els) {
				if (e.k === "Ph") continue;
				const value = materializeValue(e, ctx);
				if (value === null && (e.k === "Comp" || e.k === "Ref")) continue;
				items.push(value);
			}
			return items;
		}
		case "Obj": {
			const o = {};
			for (const [k, v] of node.entries) o[k] = materializeValue(v, ctx);
			return o;
		}
		case "Comp": {
			const { name, args } = node;
			if (node.fn && !isRuntimeCall(name, ctx)) {
				pushValidationIssue(ctx, name, "", { code: "unknown-function" });
				return null;
			}
			if (isReservedCall(name)) {
				pushValidationIssue(ctx, name, "", { code: "inline-reserved" });
				return null;
			}
			if (isRuntimeCall(name, ctx)) {
				const lazy = materializeLazyBuiltin(node, ctx, /* @__PURE__ */ new Set());
				if (lazy) return lazy;
				return materializeCall(node, ctx, (a) => materializeExpr(a, ctx));
			}
			const def = ctx.cat?.get(name);
			const props = {};
			if (def) {
				let dropComponent = false;
				for (let i = 0; i < def.params.length && i < args.length; i++) {
					const param = def.params[i];
					const value = materializeValue(args[i], ctx);
					props[param.name] = value;
					if (param.schema === void 0) continue;
					let next = validateSchemaValue(value, param.schema, name, `/${param.name}`, ctx);
					if (next === INVALID) {
						next = resolveInvalidValue(param.required, param.defaultValue);
						if (next === INVALID) dropComponent = true;
						else if (next === void 0) delete props[param.name];
					}
					if (next !== INVALID && next !== void 0) props[param.name] = next;
				}
				if (args.length > def.params.length) pushValidationIssue(ctx, name, "", {
					code: "excess-args",
					declared: def.params.length,
					got: args.length
				});
				const missingRequired = def.params.filter((p) => p.required && (!(p.name in props) || props[p.name] === null));
				if (missingRequired.length) {
					const stillInvalid = missingRequired.filter((p) => {
						if (p.defaultValue !== void 0) {
							props[p.name] = p.defaultValue;
							return false;
						}
						return true;
					});
					if (stillInvalid.length) {
						for (const p of stillInvalid) pushValidationIssue(ctx, name, `/${p.name}`, {
							code: p.name in props ? "null-required" : "missing-required",
							signature: buildParamsSignature(name, def.params)
						});
						return null;
					}
				}
				if (dropComponent) return null;
			} else {
				pushValidationIssue(ctx, name, "", {
					code: "unknown-component",
					available: ctx.cat && [...ctx.cat.keys()]
				});
				return null;
			}
			const hasDynamicProps = Object.values(props).some((v) => containsDynamicValue(v));
			return {
				type: "element",
				typeName: name,
				props,
				partial: ctx.partial,
				hasDynamicProps
			};
		}
		default:
			if (isRuntimeExpr(node)) return materializeExpr(node, ctx);
			return node;
	}
}
//#endregion
//#region src/parser/statements.ts
/**
* Auto-close unclosed strings and brackets so that partial/streaming input
* can be parsed without syntax errors.
*/
function autoClose(input) {
	const stack = [];
	let inStr = false;
	let esc = false;
	for (let i = 0; i < input.length; i++) {
		const c = input[i];
		if (esc) {
			esc = false;
			continue;
		}
		if (c === "\\" && inStr) {
			esc = true;
			continue;
		}
		if (inStr) {
			if (c === inStr) inStr = false;
			continue;
		}
		if (c === "\"" || c === "'") {
			inStr = c;
			continue;
		}
		if (c === "(" || c === "[" || c === "{") stack.push(c);
		else if (c === ")" && stack[stack.length - 1] === "(") stack.pop();
		else if (c === "]" && stack[stack.length - 1] === "[") stack.pop();
		else if (c === "}" && stack[stack.length - 1] === "{") stack.pop();
	}
	if (!(!!inStr || stack.length > 0)) return {
		text: input,
		wasIncomplete: false
	};
	let out = input;
	if (inStr) {
		if (esc) out += "\\";
		out += inStr;
	}
	for (let j = stack.length - 1; j >= 0; j--) out += stack[j] === "(" ? ")" : stack[j] === "[" ? "]" : "}";
	return {
		text: out,
		wasIncomplete: true
	};
}
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
function split(tokens) {
	const stmts = [];
	let pos = 0;
	while (pos < tokens.length) {
		while (pos < tokens.length && tokens[pos].t === 0) pos++;
		if (pos >= tokens.length || tokens[pos].t === 13) break;
		const tok = tokens[pos];
		if (tok.t !== 16 && tok.t !== 17 && tok.t !== 18) {
			while (pos < tokens.length && tokens[pos].t !== 0 && tokens[pos].t !== 13) pos++;
			continue;
		}
		const id = tok.v;
		const idTokenType = tok.t;
		pos++;
		if (pos >= tokens.length || tokens[pos].t !== 9) {
			while (pos < tokens.length && tokens[pos].t !== 0 && tokens[pos].t !== 13) pos++;
			continue;
		}
		pos++;
		const expr = [];
		let depth = 0;
		let ternaryDepth = 0;
		while (pos < tokens.length && tokens[pos].t !== 13) {
			const tt = tokens[pos].t;
			if (tt === 0 && depth <= 0 && ternaryDepth <= 0) {
				let peek = pos + 1;
				while (peek < tokens.length && tokens[peek].t === 0) peek++;
				const nextT = peek < tokens.length ? tokens[peek].t : 13;
				if (nextT === 34 || nextT === 8 && ternaryDepth > 0) {
					pos++;
					continue;
				}
				break;
			}
			if (tt === 0) {
				pos++;
				continue;
			}
			if (tt === 1 || tt === 3 || tt === 5) depth++;
			else if ((tt === 2 || tt === 4 || tt === 6) && depth > 0) depth--;
			else if (tt === 34 && depth === 0) ternaryDepth++;
			else if (tt === 8 && depth === 0 && ternaryDepth > 0) ternaryDepth--;
			expr.push(tokens[pos++]);
		}
		if (expr.length) stmts.push({
			id,
			idTokenType,
			tokens: expr
		});
	}
	return stmts;
}
//#endregion
//#region src/parser/parser.ts
function emptyResult(incomplete = true) {
	return {
		root: null,
		meta: {
			incomplete,
			unresolved: [],
			orphaned: [],
			statementCount: 0,
			errors: []
		},
		stateDeclarations: {},
		queryStatements: [],
		mutationStatements: []
	};
}
/**
* Walk an AST node to collect all StateRef ($variable) names referenced
* within. Used at parse time to pre-compute per-query state dependencies.
*/
function collectQueryDeps(node) {
	if (!isASTNode(node)) return [];
	const refs = /* @__PURE__ */ new Set();
	walkAST(node, (current) => {
		if (current.k === "StateRef") refs.add(current.n);
	});
	return [...refs];
}
/**
* Classify a raw statement + parsed expression into a typed Statement.
* Determined at parse time from token type + expression shape.
*/
function classifyStatement(raw, expr) {
	if (expr.k === "Comp" && expr.name === RESERVED_CALLS.Query) {
		const deps = collectQueryDeps(expr.args[1]);
		return {
			kind: "query",
			id: raw.id,
			call: {
				callee: RESERVED_CALLS.Query,
				args: expr.args
			},
			expr,
			deps: deps.length > 0 ? deps : void 0
		};
	}
	if (expr.k === "Comp" && expr.name === RESERVED_CALLS.Mutation) return {
		kind: "mutation",
		id: raw.id,
		call: {
			callee: RESERVED_CALLS.Mutation,
			args: expr.args
		},
		expr
	};
	if (raw.idTokenType === 18) return {
		kind: "state",
		id: raw.id,
		init: expr
	};
	return {
		kind: "value",
		id: raw.id,
		expr
	};
}
function extractStatements(stmts, ctx, partialRef) {
	const stateDeclarations = {};
	const queryStatements = [];
	const mutationStatements = [];
	for (const stmt of stmts) switch (stmt.kind) {
		case "state":
			stateDeclarations[stmt.id] = materializeValue(stmt.init, ctx);
			break;
		case "query":
			nameRawCallArgs(stmt.call.args, ctx, stmt.id);
			queryStatements.push({
				statementId: stmt.id,
				toolAST: stmt.call.args[0] ?? null,
				argsAST: stmt.call.args[1] ?? null,
				defaultsAST: stmt.call.args[2] ?? null,
				refreshAST: stmt.call.args[3] ?? null,
				deps: stmt.deps,
				complete: true
			});
			break;
		case "mutation":
			nameRawCallArgs(stmt.call.args, ctx, stmt.id);
			mutationStatements.push({
				statementId: stmt.id,
				toolAST: stmt.call.args[0] ?? null,
				argsAST: stmt.call.args[1] ?? null
			});
			break;
	}
	for (const stmt of stmts) {
		const nodes = stmt.kind === "state" ? [stmt.init] : stmt.kind === "value" ? [stmt.expr] : stmt.kind === "query" || stmt.kind === "mutation" ? stmt.call.args : [];
		for (const node of nodes) for (const dep of collectQueryDeps(node)) {
			if (dep === partialRef?.name && partialRef.stmts.has(stmt)) continue;
			if (!(dep in stateDeclarations)) stateDeclarations[dep] = null;
		}
	}
	return {
		stateDeclarations,
		queryStatements,
		mutationStatements
	};
}
const DEFAULT_ROOT_STATEMENT_ID = "root";
/** A call to a library component: not an `@` function, built-in, Query or Mutation. */
function isComponentCall(node) {
	return node?.k === "Comp" && !node.fn && !isBuiltin(node.name) && !isReservedCall(node.name);
}
/** Entry: `root`, else the first statement that calls the root component. */
function pickEntry(stmtMap, rootName) {
	if (stmtMap.has(DEFAULT_ROOT_STATEMENT_ID)) return {
		id: DEFAULT_ROOT_STATEMENT_ID,
		fallback: false
	};
	for (const stmt of stmtMap.values()) if (stmt.kind === "value" && isComponentCall(stmt.expr) && stmt.expr.name === rootName) return {
		id: stmt.id,
		fallback: true
	};
	return null;
}
/** Follow refs to the entry's value; a component call that did not render was dropped by validation. */
function resolvesToComponentCall(node, syms) {
	const seen = /* @__PURE__ */ new Set();
	while (node?.k === "Ref" && !seen.has(node.n)) {
		seen.add(node.n);
		node = syms.get(node.n);
	}
	return isComponentCall(node);
}
/** True when a runtime expression can produce a component, e.g. `$t ? A : B`. */
function containsComponent(node) {
	let found = false;
	walkAST(node, (current) => {
		if (isComponentCall(current)) found = true;
	});
	return found;
}
function buildResult(stmtMap, typedStmts, wasIncomplete, stmtCount, cat, rootName, partialRef) {
	const entry = pickEntry(stmtMap, rootName);
	const syms = /* @__PURE__ */ new Map();
	for (const [id, stmt] of stmtMap) syms.set(id, stmt.kind === "state" ? stmt.init : stmt.expr);
	const unres = [];
	const errors = [];
	const unreached = /* @__PURE__ */ new Set();
	for (const [id, stmt] of stmtMap) {
		if (id === entry?.id) continue;
		if (stmt.kind === "state" || stmt.kind === "query" || stmt.kind === "mutation") continue;
		unreached.add(id);
	}
	const ctx = {
		syms,
		cat,
		errors,
		unres,
		visited: /* @__PURE__ */ new Set(),
		partial: wasIncomplete,
		currentStatementId: entry?.id,
		unreached
	};
	let root = null;
	let rootExpression;
	if (entry) {
		const materialized = materializeValue(syms.get(entry.id), ctx);
		if (isElementNode(materialized)) {
			root = materialized;
			root.statementId = entry.id;
		} else if (isASTNode(materialized) && containsComponent(materialized)) rootExpression = materialized;
	}
	const { stateDeclarations, queryStatements, mutationStatements } = extractStatements(typedStmts, ctx, partialRef);
	ctx.currentStatementId = entry?.id;
	const component = rootName ?? "";
	if (!entry) pushValidationIssue(ctx, component, "", {
		code: "no-root",
		reason: "missing"
	});
	else if (!root && !rootExpression && !resolvesToComponentCall(syms.get(entry.id), syms)) pushValidationIssue(ctx, component, "", {
		code: "no-root",
		reason: "not-component"
	});
	else if (entry.fallback) pushValidationIssue(ctx, component, "", {
		code: "no-root",
		reason: root ? "fallback" : "missing"
	});
	const orphaned = entry ? [...unreached] : [];
	return {
		root,
		...rootExpression ? { rootExpression } : {},
		meta: {
			incomplete: wasIncomplete,
			unresolved: unres,
			orphaned,
			statementCount: stmtCount,
			errors
		},
		stateDeclarations,
		queryStatements,
		mutationStatements
	};
}
function skipString(input, start) {
	if (input[start] !== "\"") return start;
	let i = start + 1;
	while (i < input.length) {
		const c = input[i];
		if (c === "\\") i += 2;
		else if (c === "\"") return i + 1;
		else i++;
	}
	return i;
}
/** Extract code from markdown fences, or return as-is if no fences found.
*  String-context-aware: skips ``` inside double-quoted strings. */
function stripFences(input) {
	const blocks = [];
	let i = 0;
	while (i < input.length) {
		let fenceStart = -1;
		while (i < input.length) {
			const nextI = skipString(input, i);
			if (nextI > i) {
				i = nextI;
				continue;
			}
			if (input[i] === "`" && i + 1 < input.length && input[i + 1] === "`" && i + 2 < input.length && input[i + 2] === "`") {
				fenceStart = i;
				break;
			}
			i++;
		}
		if (fenceStart === -1) break;
		let j = fenceStart + 3;
		while (j < input.length && input[j] !== "\n") j++;
		if (j >= input.length) {
			blocks.push(input.slice(fenceStart + 3).replace(/^[^\n]*\n?/, ""));
			i = input.length;
			break;
		}
		j++;
		let closePos = -1;
		let k = j;
		while (k < input.length) {
			const nextK = skipString(input, k);
			if (nextK > k) {
				k = nextK;
				continue;
			}
			if (input[k] === "`" && k + 1 < input.length && input[k + 1] === "`" && k + 2 < input.length && input[k + 2] === "`") {
				closePos = k;
				break;
			}
			k++;
		}
		if (closePos !== -1) {
			blocks.push(input.slice(j, closePos));
			i = closePos + 3;
		} else {
			blocks.push(input.slice(j));
			i = input.length;
		}
	}
	if (blocks.length > 0) return blocks.join("\n");
	if (input.startsWith("```")) {
		let j = 3;
		while (j < input.length && input[j] !== "\n") j++;
		const start = j < input.length ? j + 1 : 3;
		const body = input.slice(start);
		const trailingFence = body.lastIndexOf("```");
		if (trailingFence !== -1) return body.slice(0, trailingFence);
		return body;
	}
	return input;
}
/** Strip // and # line comments outside of strings (handles both " and ' delimiters). */
function stripComments(input) {
	let inStr = false;
	return input.split("\n").map((line) => {
		for (let i = 0; i < line.length; i++) {
			const c = line[i];
			if (inStr) {
				if (c === "\\" && i + 1 < line.length) {
					i++;
					continue;
				}
				if (c === inStr) inStr = false;
				continue;
			}
			if (c === "\"" || c === "'") {
				inStr = c;
				continue;
			}
			if (c === "/" && line[i + 1] === "/") return line.substring(0, i).trimEnd();
			if (c === "#") return line.substring(0, i).trimEnd();
		}
		return line;
	}).join("\n");
}
/** Clean LLM response: strip fences, comments, whitespace. */
function preprocess(input) {
	return stripComments(stripFences(input.trim())).trim();
}
/**
* Parse a complete openui-lang string in one pass.
*
* @param input  - Full openui-lang source text (may be partial/streaming)
* @param cat    - Param map for positional-arg → named-prop mapping
* @returns      ParseResult with root ElementNode (or null) and metadata
*/
function parse(input, cat, rootName) {
	const trimmed = preprocess(input);
	if (!trimmed) return emptyResult();
	const { text, wasIncomplete } = autoClose(trimmed);
	const stmts = split(tokenize(text));
	if (!stmts.length) return emptyResult(wasIncomplete);
	const stmtMap = /* @__PURE__ */ new Map();
	for (const s of stmts) {
		const stmt = classifyStatement(s, parseExpression(s.tokens));
		stmtMap.set(s.id, stmt);
	}
	return buildResult(stmtMap, [...stmtMap.values()], wasIncomplete, stmtMap.size, cat, rootName);
}
function createStreamParser(cat, rootName) {
	let buf = "";
	let cleaned = "";
	let completedEnd = 0;
	const completedStmtMap = /* @__PURE__ */ new Map();
	let completedCount = 0;
	function addStmt(text) {
		const t = text.trim();
		if (!t) return;
		for (const s of split(tokenize(t))) {
			const stmt = classifyStatement(s, parseExpression(s.tokens));
			completedStmtMap.set(s.id, stmt);
			completedCount++;
		}
	}
	function refreshCleaned() {
		const next = preprocess(buf);
		if (!next.startsWith(cleaned.slice(0, completedEnd))) {
			completedEnd = 0;
			completedStmtMap.clear();
			completedCount = 0;
		}
		cleaned = next;
	}
	function scanNewCompleted() {
		let depth = 0, ternaryDepth = 0, inStr = false, esc = false;
		let stmtStart = completedEnd;
		for (let i = completedEnd; i < cleaned.length; i++) {
			const c = cleaned[i];
			if (esc) {
				esc = false;
				continue;
			}
			if (c === "\\" && inStr) {
				esc = true;
				continue;
			}
			if (inStr) {
				if (c === inStr) inStr = false;
				continue;
			}
			if (c === "\"" || c === "'") {
				inStr = c;
				continue;
			}
			if (c === "(" || c === "[" || c === "{") depth++;
			else if (c === ")" || c === "]" || c === "}") depth = Math.max(0, depth - 1);
			else if (c === "?" && depth === 0) ternaryDepth++;
			else if (c === ":" && depth === 0 && ternaryDepth > 0) ternaryDepth--;
			else if (c === "\n" && depth <= 0 && ternaryDepth <= 0) {
				let peek = i + 1;
				while (peek < cleaned.length && (cleaned[peek] === " " || cleaned[peek] === "	" || cleaned[peek] === "\r" || cleaned[peek] === "\n")) peek++;
				if (peek < cleaned.length && (cleaned[peek] === "?" || cleaned[peek] === ":" && ternaryDepth > 0)) continue;
				const t = cleaned.slice(stmtStart, i).trim();
				if (t) addStmt(t);
				stmtStart = i + 1;
				completedEnd = i + 1;
			}
		}
		return stmtStart;
	}
	function currentResult(complete = false) {
		refreshCleaned();
		const pendingStart = scanNewCompleted();
		const pendingText = cleaned.slice(pendingStart).trim();
		if (!pendingText) {
			if (completedCount === 0) return emptyResult();
			return buildResult(completedStmtMap, [...completedStmtMap.values()], false, completedCount, cat, rootName);
		}
		const { text: closed, wasIncomplete } = autoClose(pendingText);
		const stmts = split(tokenize(closed));
		if (!stmts.length) {
			if (completedCount === 0) return emptyResult(wasIncomplete);
			return buildResult(completedStmtMap, [...completedStmtMap.values()], wasIncomplete, completedCount, cat, rootName);
		}
		const allStmtMap = new Map(completedStmtMap);
		const pendingStmts = /* @__PURE__ */ new Set();
		for (const s of stmts) {
			if (completedStmtMap.has(s.id) && wasIncomplete) continue;
			const stmt = classifyStatement(s, parseExpression(s.tokens));
			allStmtMap.set(s.id, stmt);
			pendingStmts.add(stmt);
		}
		const tail = complete ? void 0 : /\$\w+$/.exec(buf)?.[0];
		const tailName = tail && pendingText.endsWith(tail) && !new RegExp(`\\${tail}(?!\\w)`).test(pendingText.slice(0, -tail.length)) ? tail : void 0;
		return buildResult(allStmtMap, [...allStmtMap.values()], wasIncomplete, completedCount + stmts.length, cat, rootName, tailName ? {
			name: tailName,
			stmts: pendingStmts
		} : void 0);
	}
	function reset() {
		buf = "";
		cleaned = "";
		completedEnd = 0;
		completedStmtMap.clear();
		completedCount = 0;
	}
	return {
		push(chunk, complete) {
			buf += chunk;
			return currentResult(complete);
		},
		set(fullText, complete) {
			if (fullText.length < buf.length || !fullText.startsWith(buf)) reset();
			const delta = fullText.slice(buf.length);
			if (delta) buf += delta;
			return currentResult(complete);
		},
		getResult: currentResult
	};
}
function inlineActionRefs(node, defs) {
	if (!node || typeof node !== "object") return node;
	const ref = node.$ref;
	if (typeof ref === "string") return defs.has(ref) ? inlineActionRefs(defs.get(ref), defs) : node;
	const copy = Array.isArray(node) ? [...node] : { ...node };
	let changed = false;
	for (const [key, value] of Object.entries(copy)) {
		copy[key] = inlineActionRefs(value, defs);
		changed ||= copy[key] !== value;
	}
	return changed ? copy : node;
}
function compileParams(def, defs) {
	const properties = def.properties ?? {};
	const required = def.required ?? [];
	return Object.keys(properties).map((key) => ({
		name: key,
		required: required.includes(key),
		defaultValue: getSchemaDefaultValue(properties[key]),
		schema: defs.size ? inlineActionRefs(properties[key], defs) : properties[key]
	}));
}
function compileSchema(schema) {
	const map = /* @__PURE__ */ new Map();
	const components = schema.properties && new Set(Object.keys(schema.properties));
	const defs = /* @__PURE__ */ new Map();
	for (const name of [...Object.keys(ACTION_DEFS), ...Object.keys(schema.actions ?? {})]) {
		const body = schema.$defs?.[name];
		if (body && !components?.has(name)) defs.set(`#/$defs/${name}`, body);
	}
	for (const [name, def] of Object.entries(schema.$defs ?? {})) {
		if (components ? !components.has(name) : defs.has(`#/$defs/${name}`)) continue;
		map.set(name, { params: compileParams(def, defs) });
	}
	map.callDefs = new Map(BUILTIN_CALLS);
	for (const [name, fn] of Object.entries(schema.functions ?? {})) map.callDefs.set(name, {
		kind: "function",
		params: compileParams(fn.params, defs)
	});
	for (const [name, action] of Object.entries(schema.actions ?? {})) map.callDefs.set(name, {
		kind: "action",
		params: compileParams(action.params, defs)
	});
	return map;
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
function createParser(schema, rootName) {
	const paramMap = compileSchema(schema);
	return { parse(input) {
		const telemetry = prepareParserParseTelemetry();
		try {
			const result = parse(input, paramMap, rootName);
			captureParserParseResult(telemetry, result);
			return result;
		} catch (error) {
			captureParserParseException(telemetry);
			throw error;
		}
	} };
}
/**
* Create a streaming parser from a library JSON Schema document.
* Pass `library.toJSONSchema()` to get the schema.
*/
function createStreamingParser(schema, rootName) {
	return createStreamParser(compileSchema(schema), rootName);
}
//#endregion
//#region src/parser/enrich-errors.ts
/** Build a signature hint like "Header(title*, subtitle, icon)" from JSON schema. */
function buildSignatureHint(componentName, schema) {
	if (!schema?.properties) return void 0;
	const required = new Set(schema.required ?? []);
	return `Signature: ${componentName}(${Object.keys(schema.properties).map((k) => required.has(k) ? `${k}*` : k).join(", ")}) — * marks required`;
}
/**
* Convert parser ValidationErrors into enriched OpenUIErrors with hints.
*
* Framework-agnostic — usable by React, Svelte, Vue, or standalone.
*
* @deprecated ValidationError.message is already humanized and self-sufficient
* (signatures, available components, expected types are inlined) — read
* `result.meta.errors` directly. Will be removed in a future major release.
*/
function enrichErrors(validationErrors, schema, componentNames) {
	return validationErrors.map((ve) => {
		const error = {
			source: "parser",
			code: ve.code,
			message: ve.message,
			component: ve.component,
			path: ve.path || void 0,
			statementId: ve.statementId,
			...ve.severity && { severity: ve.severity }
		};
		if (ve.code === "unknown-component" && componentNames.length) error.hint = `Available components: ${componentNames.join(", ")}`;
		else if (ve.code === "missing-required" || ve.code === "null-required") error.hint = buildSignatureHint(ve.component, schema.$defs?.[ve.component]);
		else if (ve.code === "inline-reserved") error.hint = `Declare as a top-level statement: myVar = ${ve.component}(...)`;
		return error;
	});
}
//#endregion
//#region src/parser/merge.ts
function splitStatementSource(input) {
	const stmts = [];
	let depth = 0;
	let inStr = false;
	let esc = false;
	let start = 0;
	for (let i = 0; i < input.length; i++) {
		const c = input[i];
		if (esc) {
			esc = false;
			continue;
		}
		if (c === "\\" && inStr) {
			esc = true;
			continue;
		}
		if (inStr) {
			if (c === inStr) inStr = false;
			continue;
		}
		if (c === "\"" || c === "'") {
			inStr = c;
			continue;
		}
		if (c === "(" || c === "[" || c === "{") depth++;
		else if (c === ")" || c === "]" || c === "}") depth = Math.max(0, depth - 1);
		else if (c === "\n" && depth <= 0) {
			const stmt = input.slice(start, i).trim();
			if (stmt) stmts.push(stmt);
			start = i + 1;
		}
	}
	const tail = input.slice(start).trim();
	if (tail) stmts.push(tail);
	return stmts;
}
function parseStatements(input) {
	const trimmed = input.trim();
	if (!trimmed) return [];
	const result = [];
	for (const raw of splitStatementSource(trimmed)) {
		const stmt = split(tokenize(raw))[0];
		if (!stmt) continue;
		result.push({
			id: stmt.id,
			ast: parseExpression(stmt.tokens),
			raw
		});
	}
	return result;
}
/**
* Recursively collect all Ref names from an AST node.
*/
function collectRefs(node, out) {
	walkAST(node, (current) => {
		if (current.k === "Ref") out.add(current.n);
		if (current.k === "RuntimeRef") out.add(current.n);
	});
}
/**
* Remove statements unreachable from `root` (garbage collection).
* Walks the AST graph from root, collecting all referenced statement IDs.
* $state variables are always kept (they're referenced at runtime, not by Ref nodes).
*/
function gcUnreachable(order, merged, asts, rootId = "root") {
	if (!asts.get(rootId)) return;
	const reachable = new Set([rootId]);
	const queue = [rootId];
	while (queue.length > 0) {
		const id = queue.pop();
		const ast = asts.get(id);
		if (!ast) continue;
		const refs = /* @__PURE__ */ new Set();
		collectRefs(ast, refs);
		for (const ref of refs) if (!reachable.has(ref) && asts.has(ref)) {
			reachable.add(ref);
			queue.push(ref);
		}
	}
	for (const id of order) if (id.startsWith("$")) reachable.add(id);
	for (let i = order.length - 1; i >= 0; i--) if (!reachable.has(order[i])) {
		merged.delete(order[i]);
		order.splice(i, 1);
	}
}
/**
* Merge an existing program with a patch (partial update).
* Patch statements override existing ones by name.
* Unreachable statements are automatically garbage-collected.
* Returns the merged program as a string.
*/
function mergeStatements(existing, patch, rootId = "root") {
	const existingStmts = parseStatements(existing);
	const patchStmts = parseStatements(stripFences(patch));
	if (!existingStmts.length) return patchStmts.map((stmt) => stmt.raw).join("\n");
	if (!patchStmts.length) return existing;
	const merged = /* @__PURE__ */ new Map();
	const asts = /* @__PURE__ */ new Map();
	const order = [];
	for (const stmt of existingStmts) {
		merged.set(stmt.id, stmt.raw);
		asts.set(stmt.id, stmt.ast);
		order.push(stmt.id);
	}
	for (const stmt of patchStmts) {
		if (stmt.ast.k === "Null") {
			merged.delete(stmt.id);
			asts.delete(stmt.id);
			const idx = order.indexOf(stmt.id);
			if (idx !== -1) order.splice(idx, 1);
			continue;
		}
		if (!merged.has(stmt.id)) order.push(stmt.id);
		merged.set(stmt.id, stmt.raw);
		asts.set(stmt.id, stmt.ast);
	}
	gcUnreachable(order, merged, asts, rootId);
	return order.filter((id) => merged.has(id)).map((id) => merged.get(id)).join("\n");
}
//#endregion
//#region src/parser/serialize.ts
const PRECEDENCE = {
	"||": 2,
	"&&": 3,
	"==": 4,
	"!=": 4,
	">": 5,
	"<": 5,
	">=": 5,
	"<=": 5,
	"+": 6,
	"-": 6,
	"*": 7,
	"/": 7,
	"%": 7
};
var StatementCollector = class {
	statements = [];
	registered = /* @__PURE__ */ new Set();
	/** Register an ElementNode as a named statement. Returns the statementId for use as a reference. */
	register(id, text) {
		if (this.registered.has(id)) return;
		this.registered.add(id);
		this.statements.push({
			id,
			text
		});
	}
	has(id) {
		return this.registered.has(id);
	}
	getStatements() {
		return this.statements;
	}
};
/** `calls` are the library's call names (built-ins, functions, actions), written with `@`. */
function serializeASTNode(node, calls) {
	switch (node.k) {
		case "Str": return JSON.stringify(node.v);
		case "Num": return String(node.v);
		case "Bool": return node.v ? "true" : "false";
		case "Null": return "null";
		case "Ph": return node.n;
		case "StateRef": return node.n;
		case "RuntimeRef": return node.n;
		case "Arr": return "[" + node.els.map((el) => serializeASTNode(el, calls)).join(", ") + "]";
		case "Obj": return "{" + node.entries.map(([k, v]) => `${k}: ${serializeASTNode(v, calls)}`).join(", ") + "}";
		case "BinOp": {
			const left = serializeBinOpChild(node.left, node.op, "left", calls);
			const right = serializeBinOpChild(node.right, node.op, "right", calls);
			return `${left} ${node.op} ${right}`;
		}
		case "UnaryOp": return `${node.op}${serializeASTNode(node.operand, calls)}`;
		case "Ternary": return `${serializeASTNode(node.cond, calls)} ? ${serializeASTNode(node.then, calls)} : ${serializeASTNode(node.else, calls)}`;
		case "Member": return `${serializeASTNode(node.obj, calls)}.${node.field}`;
		case "Index": return `${serializeASTNode(node.obj, calls)}[${serializeASTNode(node.index, calls)}]`;
		case "Assign": return `${node.target} = ${serializeASTNode(node.value, calls)}`;
		case "Comp": {
			const args = node.args.map((arg) => serializeASTNode(arg, calls)).join(", ");
			if (calls.has(node.name) && node.name !== "Action") return `@${node.name}(${args})`;
			return `${node.name}(${args})`;
		}
		case "Ref": return node.n;
		default: return "null";
	}
}
/** Wrap a BinOp child in parens if its precedence is lower than the parent's. */
function serializeBinOpChild(child, parentOp, _side, calls) {
	const inner = serializeASTNode(child, calls);
	if (child.k !== "BinOp") return inner;
	const parentPrec = PRECEDENCE[parentOp] ?? 0;
	if ((PRECEDENCE[child.op] ?? 0) < parentPrec) return `(${inner})`;
	return inner;
}
function serializeValue(value, paramMap, collector) {
	if (value === null || value === void 0) return "null";
	if (typeof value === "string") return JSON.stringify(value);
	if (typeof value === "number") return String(value);
	if (typeof value === "boolean") return value ? "true" : "false";
	if (isElementNode(value)) return serializeElementValue(value, paramMap, collector);
	if (isASTNode(value)) return serializeASTNode(value, getCallDefs(paramMap));
	if (Array.isArray(value)) return "[" + value.map((el) => serializeValue(el, paramMap, collector)).join(", ") + "]";
	if (typeof value === "object") return "{" + Object.entries(value).map(([k, v]) => `${k}: ${serializeValue(v, paramMap, collector)}`).join(", ") + "}";
	return "null";
}
/** Serialize an ElementNode — either as a reference (if it has statementId) or inline. */
function serializeElementValue(node, paramMap, collector) {
	if (node.statementId) {
		if (!collector.has(node.statementId)) {
			const expr = serializeElementExpr(node, paramMap, collector);
			collector.register(node.statementId, `${node.statementId} = ${expr}`);
		}
		return node.statementId;
	}
	return serializeElementExpr(node, paramMap, collector);
}
function serializeElementExpr(node, paramMap, collector) {
	const def = paramMap.get(node.typeName);
	if (def) {
		const args = [];
		for (const param of def.params) {
			const val = node.props[param.name];
			args.push(serializeValue(val, paramMap, collector));
		}
		while (args.length > 0) {
			if (args[args.length - 1] !== "null") break;
			const paramIdx = args.length - 1;
			const param = def.params[paramIdx];
			if (!param) break;
			if (param.required) break;
			args.pop();
		}
		return `${node.typeName}(${args.join(", ")})`;
	}
	const fallbackArgs = Object.values(node.props).map((v) => serializeValue(v, paramMap, collector));
	return `${node.typeName}(${fallbackArgs.join(", ")})`;
}
function serializeStateDeclarations(stateDeclarations, paramMap, collector) {
	const lines = [];
	for (const [name, value] of Object.entries(stateDeclarations)) {
		if (value === null) continue;
		lines.push(`${name} = ${serializeValue(value, paramMap, collector)}`);
	}
	return lines;
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
function jsonToOpenUI(json, library, options) {
	const paramMap = compileSchema(library.toJSONSchema());
	const collector = new StatementCollector();
	const rootExpr = serializeElementExpr(json, paramMap, collector);
	const rootId = json.statementId || "root";
	const lines = [];
	lines.push(`${rootId} = ${rootExpr}`);
	for (const stmt of collector.getStatements()) {
		if (stmt.id === rootId) continue;
		lines.push(stmt.text);
	}
	if (options?.stateDeclarations) {
		const stateLines = serializeStateDeclarations(options.stateDeclarations, paramMap, collector);
		lines.push(...stateLines);
	}
	return lines.join("\n");
}
//#endregion
//#region src/runtime/evaluator.ts
function isReactiveAssign(value) {
	return typeof value === "object" && value !== null && value.__reactive === "assign";
}
/**
* Evaluate an AST node to a runtime value.
*/
function evaluate(node, context, schemaCtx) {
	switch (node.k) {
		case "Str": return node.v;
		case "Num": return node.v;
		case "Bool": return node.v;
		case "Null": return null;
		case "Ph": return null;
		case "StateRef": return context.extraScope?.[node.n] ?? context.getState(node.n);
		case "Ref":
		case "RuntimeRef": return context.resolveRef(node.n);
		case "Arr": return node.els.map((el) => evaluate(el, context));
		case "Obj": return Object.fromEntries(node.entries.map(([k, v]) => [k, evaluate(v, context)]));
		case "Comp": {
			const builtin = BUILTINS[node.name];
			if (builtin) {
				if (builtin.lazy) return evaluateLazyBuiltin(node.name, node.args, context, schemaCtx);
				return builtin.fn(evaluateArgs(node, context));
			}
			if (isStep(node.name)) return evaluateActionCall(node, context);
			const fns = context.functions;
			if (fns && Object.prototype.hasOwnProperty.call(fns, node.name)) return callFunction(node.name, fns[node.name], node, context);
			const acts = context.actions;
			if (acts && Object.prototype.hasOwnProperty.call(acts, node.name)) {
				const def = acts[node.name];
				const args = callArgs(node, context, def.params);
				if (missesRequired(args, def.params)) return { steps: [] };
				const params = checkValue(args, def.params, node.name, "", context, true);
				if (params === INVALID) return { steps: [] };
				const step = {
					type: "custom_action",
					name: node.name,
					params
				};
				customSteps.add(step);
				return { steps: [step] };
			}
			if (node.mappedProps) {
				const def = schemaCtx?.library.components[node.name];
				const props = {};
				for (const [key, val] of Object.entries(node.mappedProps)) {
					const propSchema = def?.props?.shape?.[key];
					if (val.k === "StateRef" && propSchema && isReactiveSchema(propSchema)) props[key] = {
						__reactive: "assign",
						target: val.n,
						expr: {
							k: "StateRef",
							n: "$value"
						}
					};
					else if (val.k === "StateRef") props[key] = schemaCtx ? context.getState(val.n) : val;
					else props[key] = evaluate(val, context, schemaCtx);
				}
				const result = {
					type: "element",
					typeName: node.name,
					props,
					partial: false,
					hasDynamicProps: true
				};
				if (schemaCtx) {
					for (const [key, val] of Object.entries(props)) if (isElementNode(val)) props[key] = evaluateElementInline(val, context, schemaCtx);
					else if (Array.isArray(val)) props[key] = val.map((item) => isElementNode(item) ? evaluateElementInline(item, context, schemaCtx) : item);
				}
				return result;
			}
			console.warn(`[openui] Unexpected unmapped Comp node: ${node.name}`);
			return null;
		}
		case "BinOp": {
			if (node.op === "&&") {
				const left = evaluate(node.left, context);
				return left ? evaluate(node.right, context) : left;
			}
			if (node.op === "||") {
				const left = evaluate(node.left, context);
				return left ? left : evaluate(node.right, context);
			}
			const left = evaluate(node.left, context);
			const right = evaluate(node.right, context);
			switch (node.op) {
				case "+":
					if (typeof left === "string" || typeof right === "string") return String(left ?? "") + String(right ?? "");
					return toNumber(left) + toNumber(right);
				case "-": return toNumber(left) - toNumber(right);
				case "*": return toNumber(left) * toNumber(right);
				case "/": return toNumber(right) === 0 ? 0 : toNumber(left) / toNumber(right);
				case "%": return toNumber(right) === 0 ? 0 : toNumber(left) % toNumber(right);
				case "==": return left == right;
				case "!=": return left != right;
				case ">": return toNumber(left) > toNumber(right);
				case "<": return toNumber(left) < toNumber(right);
				case ">=": return toNumber(left) >= toNumber(right);
				case "<=": return toNumber(left) <= toNumber(right);
				default: return null;
			}
		}
		case "UnaryOp":
			if (node.op === "!") return !evaluate(node.operand, context);
			if (node.op === "-") return -toNumber(evaluate(node.operand, context));
			return null;
		case "Ternary": return evaluate(node.cond, context) ? evaluate(node.then, context) : evaluate(node.else, context);
		case "Member": {
			const obj = evaluate(node.obj, context);
			if (obj == null) return null;
			if (Array.isArray(obj)) {
				if (node.field === "length") return obj.length;
				return obj.map((item) => item?.[node.field] ?? null);
			}
			return obj[node.field];
		}
		case "Index": {
			const obj = evaluate(node.obj, context);
			const idx = evaluate(node.index, context);
			if (obj == null || idx == null) return null;
			if (Array.isArray(obj)) return obj[toNumber(idx)];
			return obj[String(idx)];
		}
		case "Assign": return {
			__reactive: "assign",
			target: node.target,
			expr: node.value
		};
	}
}
/**
* Strip a ReactiveAssign to its current value in a non-reactive context.
* When transport args or non-reactive props contain a ReactiveAssign, this
* resolves it to the current state value (or null if getState is unavailable).
*/
function stripReactiveAssign(value, context) {
	if (!isReactiveAssign(value)) return value;
	return context.getState(value.target) ?? null;
}
/**
* Evaluate an ElementNode's props with schema awareness. Used by evaluate()
* when schema context is available and a Comp produces an ElementNode that
* needs its own props evaluated with reactive schema detection.
*/
function evaluateElementInline(el, context, schemaCtx) {
	if (el.hasDynamicProps === false) return el;
	const def = schemaCtx.library.components[el.typeName];
	const evaluated = {};
	for (const [key, value] of Object.entries(el.props)) {
		const propSchema = def?.props?.shape?.[key];
		evaluated[key] = evaluatePropInline(value, context, schemaCtx, propSchema);
	}
	return {
		...el,
		props: evaluated
	};
}
/**
* Evaluate a single prop value with schema awareness.
* Delegates to shared evaluatePropCore with inline-specific recursion callbacks.
*/
function evaluatePropInline(value, context, schemaCtx, reactiveSchema) {
	return evaluatePropCore(value, context, schemaCtx, reactiveSchema, {
		recurseElement: (el) => evaluateElementInline(el, context, schemaCtx),
		recurse: (v, rs) => evaluatePropInline(v, context, schemaCtx, rs)
	});
}
function evaluateArgs(node, context, params = BUILTIN_CALLS.get(node.name)?.params ?? []) {
	const named = node.mappedProps ?? nameArgs(node.args, params);
	const args = {};
	for (const key in named) args[key] = evaluate(named[key], context);
	return args;
}
/** A library function's or action's args; an explicit null for an optional param is omitted, so its default applies. */
function callArgs(node, context, params) {
	const args = evaluateArgs(node, context, Object.keys(params.properties ?? {}).map((name) => ({
		name,
		required: false
	})));
	for (const key in args) if (args[key] === null && !params.required?.includes(key)) delete args[key];
	return args;
}
function checkValue(value, schema, name, path, context, strict = false) {
	const ctx = {
		errors: [],
		partial: false
	};
	const checked = validateSchemaValue(value, schema, name, path, ctx);
	for (const e of ctx.errors) context.reportError?.(`@${name}: ${e.message}`);
	return strict && ctx.errors.length > 0 ? INVALID : checked;
}
function missesRequired(args, params) {
	return !!params.required?.some((key) => args[key] == null && getSchemaDefaultValue(params.properties?.[key]) === void 0);
}
function callFunction(name, def, node, context) {
	const raw = callArgs(node, context, def.params);
	if (missesRequired(raw, def.params)) return null;
	const args = checkValue(raw, def.params, name, "", context);
	if (args === INVALID) return null;
	let result;
	try {
		result = def.fn(args);
	} catch (e) {
		context.reportError?.(`@${name} threw: ${e instanceof Error ? e.message : String(e)}`);
		return null;
	}
	if (!def.returns) return result;
	const checked = checkValue(result, def.returns, name, "/returns", context);
	return checked === INVALID ? null : checked ?? null;
}
/** True for an evaluated action plan (`{ steps: [...] }`). */
function isActionPlan(value) {
	return typeof value === "object" && value !== null && Array.isArray(value.steps);
}
/** Flatten plans (and stray step objects) into one plan; other values are dropped. */
function flattenPlans(values) {
	return ownSteps({ steps: values.flatMap((v) => {
		if (isActionPlan(v)) return v.steps;
		return v != null && typeof v === "object" && "type" in v ? [v] : [];
	}) });
}
/** The custom steps made by real calls; a custom step read from data is not among them. */
const customSteps = /* @__PURE__ */ new WeakSet();
/** Drops the custom steps no real call made, so data cannot pose as a custom action. */
function ownSteps(plan) {
	const own = (s) => s?.type !== "custom_action" || customSteps.has(s);
	return plan.steps.every(own) ? plan : {
		...plan,
		steps: plan.steps.filter(own)
	};
}
/** A step call is a one-step plan, an invalid step an empty plan; Action([...]) flattens. */
function evaluateActionCall(node, context) {
	const { name, args } = node;
	if (name === "Action") {
		const list = args.length > 0 ? evaluate(args[0], context) : [];
		return flattenPlans(Array.isArray(list) ? list : []);
	}
	const def = BUILTIN_STEPS[name];
	const step = def.step ? def.step(evaluateArgs(node, context)) : evaluateLazyStep(name, args);
	return { steps: step ? [step] : [] };
}
/** The steps whose args are refs, read as AST. */
function evaluateLazyStep(name, args) {
	switch (name) {
		case "Run": {
			const refNode = args[0];
			if (refNode?.k !== "RuntimeRef") return null;
			return {
				type: ACTION_STEPS.Run,
				statementId: refNode.n,
				refType: refNode.refType
			};
		}
		case "Set":
			if (args.length < 2 || args[0].k !== "StateRef") return null;
			return {
				type: ACTION_STEPS.Set,
				target: args[0].n,
				valueAST: args[1]
			};
		case "Reset": {
			const targets = args.filter((a) => a.k === "StateRef").map((a) => a.n);
			if (targets.length === 0) return null;
			return {
				type: ACTION_STEPS.Reset,
				targets
			};
		}
		default: return null;
	}
}
/**
* Substitute all Ref(varName) nodes in an AST tree with a literal value.
* This pre-resolves loop variables so deferred expressions (like Action steps)
* don't lose scope when evaluated later at click time.
*/
function substituteRef(node, varName, value) {
	switch (node.k) {
		case "Ref": return node.n === varName ? toLiteralAST(value) : node;
		case "Member":
			if (isASTNode(node.obj)) {
				const subObj = substituteRef(node.obj, varName, value);
				if (subObj.k === "Obj") {
					const entry = subObj.entries.find(([k]) => k === node.field);
					if (entry) return entry[1];
				}
				return {
					...node,
					obj: subObj
				};
			}
			return node;
		case "Index": return {
			...node,
			obj: isASTNode(node.obj) ? substituteRef(node.obj, varName, value) : node.obj,
			index: isASTNode(node.index) ? substituteRef(node.index, varName, value) : node.index
		};
		case "BinOp": return {
			...node,
			left: substituteRef(node.left, varName, value),
			right: substituteRef(node.right, varName, value)
		};
		case "UnaryOp": return {
			...node,
			operand: substituteRef(node.operand, varName, value)
		};
		case "Ternary": return {
			...node,
			cond: substituteRef(node.cond, varName, value),
			then: substituteRef(node.then, varName, value),
			else: substituteRef(node.else, varName, value)
		};
		case "Arr": return {
			...node,
			els: node.els.map((e) => substituteRef(e, varName, value))
		};
		case "Obj": return {
			...node,
			entries: node.entries.map(([k, v]) => [k, substituteRef(v, varName, value)])
		};
		case "Comp": {
			const sub = (a) => substituteRef(a, varName, value);
			const result = {
				...node,
				args: node.args.map(sub)
			};
			if (node.mappedProps) result.mappedProps = mapValues(node.mappedProps, sub);
			return result;
		}
		case "Assign": return {
			...node,
			value: substituteRef(node.value, varName, value)
		};
		default: return node;
	}
}
function mapValues(record, fn) {
	const out = {};
	for (const key in record) out[key] = fn(record[key]);
	return out;
}
/**
* Each(array, varName, template) — evaluate template once per array item.
* varName is user-defined (e.g. `issue`, `ticket`) — no $ prefix collision.
*
* Before evaluation, substitutes all Ref(varName) in the template with the
* current item's literal value. This ensures deferred expressions (like
* Action/Set steps) capture concrete values instead of dangling loop refs.
*/
function evaluateLazyBuiltin(name, args, context, schemaCtx) {
	if (name === "Each") {
		if (args.length < 3) return [];
		const arr = evaluate(args[0], context);
		if (!Array.isArray(arr)) return [];
		const varName = args[1].k === "Ref" ? args[1].n : args[1].k === "Str" ? args[1].v : null;
		if (!varName) return [];
		const template = args[2];
		return arr.map((item, _idx) => {
			const substituted = substituteRef(template, varName, item);
			const childCtx = {
				...context,
				resolveRef: (refName) => {
					if (refName === varName) return item;
					return context.resolveRef(refName);
				}
			};
			const result = evaluate(substituted, childCtx, schemaCtx);
			if (schemaCtx && isElementNode(result)) return evaluateElementInline(result, childCtx, schemaCtx);
			return result;
		});
	}
	return null;
}
//#endregion
//#region src/runtime/evaluate-prop.ts
/**
* Shared prop value evaluation logic.
*
* Both evaluator.ts (inline path) and evaluate-tree.ts (React path) need
* identical prop evaluation — AST resolution, ReactiveAssign handling,
* ElementNode recursion, ActionPlan preservation. The only difference is
* how they recurse into ElementNodes. This module extracts the shared core
* and takes recursion callbacks so each caller can supply its own strategy.
*
* Also fixes the nested reactive drop bug: reactiveSchema is now correctly
* passed through plain object recursion.
*/
/**
* Evaluate a single prop value with schema awareness. Handles AST nodes,
* ReactiveAssign markers, nested ElementNodes, arrays, and ActionPlans.
*/
function evaluatePropCore(value, context, schemaCtx, reactiveSchema, callbacks) {
	const result = evaluatePropValue$1(value, context, schemaCtx, reactiveSchema, callbacks);
	return isActionPlan(result) ? ownSteps(result) : result;
}
function evaluatePropValue$1(value, context, schemaCtx, reactiveSchema, callbacks) {
	if (value == null) return value;
	if (typeof value !== "object") return value;
	if (isASTNode(value)) {
		if (value.k === "StateRef" && reactiveSchema && isReactiveSchema(reactiveSchema)) return {
			__reactive: "assign",
			target: value.n,
			expr: {
				k: "StateRef",
				n: "$value"
			}
		};
		const result = evaluate(value, context, schemaCtx);
		if (isElementNode(result)) return callbacks.recurseElement(result);
		if (Array.isArray(result)) return result.map((item) => isElementNode(item) ? callbacks.recurseElement(item) : item);
		if (isReactiveAssign(result) && !(reactiveSchema && isReactiveSchema(reactiveSchema))) return context.getState(result.target) ?? null;
		return result;
	}
	if (typeof value === "string" && reactiveSchema && isReactiveSchema(reactiveSchema)) return value;
	if (Array.isArray(value)) return value.map((v) => callbacks.recurse(v, reactiveSchema));
	if (isElementNode(value)) return callbacks.recurseElement(value);
	if (isActionPlan(value)) return value;
	const obj = value;
	let needsEval = false;
	for (const val of Object.values(obj)) if (typeof val === "object" && val !== null) {
		needsEval = true;
		break;
	}
	if (needsEval) {
		const result = {};
		for (const [k, v] of Object.entries(obj)) result[k] = callbacks.recurse(v, reactiveSchema);
		return result;
	}
	return value;
}
//#endregion
//#region src/runtime/evaluate-tree.ts
/**
* Evaluate all AST nodes in an ElementNode tree's props.
* Returns a new ElementNode with all props resolved to concrete values.
*
* Uses the unified evaluator with schema context for reactive-aware evaluation.
*/
function evaluateElementProps(el, evalCtx) {
	if (el.hasDynamicProps === false) return el;
	const schemaCtx = { library: evalCtx.library };
	const def = evalCtx.library.components[el.typeName];
	const evaluated = {};
	let key = "";
	const report = (msg) => {
		evalCtx.errors?.push({
			source: "runtime",
			code: "runtime-error",
			component: el.typeName,
			statementId: el.statementId,
			message: `Evaluating prop "${key}" on ${el.typeName} failed: ${msg}`,
			hint: `Check the expression used for prop "${key}"`
		});
	};
	const propCtx = evalCtx.ctx.functions || evalCtx.ctx.actions ? {
		...evalCtx,
		ctx: {
			...evalCtx.ctx,
			reportError: report
		}
	} : evalCtx;
	for (const [propKey, value] of Object.entries(el.props)) {
		key = propKey;
		const propSchema = def?.props?.shape?.[key];
		try {
			evaluated[key] = evaluatePropValue(value, propCtx, schemaCtx, propSchema);
		} catch (e) {
			evaluated[key] = value;
			report(e instanceof Error ? e.message : String(e));
		}
	}
	return {
		...el,
		props: evaluated
	};
}
/** Evaluate `root`, or the component a runtime entry (`rootExpression`) picks; null if none. */
function evaluateRoot(result, evalCtx) {
	if (result.root) return evaluateElementProps(result.root, evalCtx);
	if (!result.rootExpression) return null;
	const value = evaluate(result.rootExpression, evalCtx.ctx, { library: evalCtx.library });
	if (isElementNode(value)) return evaluateElementProps(value, evalCtx);
	evalCtx.errors?.push({
		source: "runtime",
		code: "no-root",
		statementId: "root",
		message: "root does not evaluate to a component"
	});
	return null;
}
/**
* Evaluate a single prop value with schema awareness.
* Delegates to shared evaluatePropCore with evaluate-tree-specific recursion callbacks.
*/
function evaluatePropValue(value, evalCtx, schemaCtx, reactiveSchema) {
	return evaluatePropCore(value, evalCtx.ctx, schemaCtx, reactiveSchema, {
		recurseElement: (el) => evaluateElementProps(el, evalCtx),
		recurse: (v, rs) => evaluatePropValue(v, evalCtx, schemaCtx, rs)
	});
}
//#endregion
//#region src/runtime/mcp.ts
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
var McpToolError = class extends Error {
	toolErrorText;
	constructor(errorText) {
		super(`MCP tool error: ${errorText || "Unknown error"}`);
		this.name = "McpToolError";
		this.toolErrorText = errorText;
	}
};
/**
* Extract the actual data from an MCP callTool result.
* Prefers structuredContent (machine-readable JSON), falls back to parsing text content.
*/
function extractToolResult(result) {
	if (result.isError) {
		const errorText = result.content?.filter((c) => c.type === "text").map((c) => c.text).join("\n");
		throw new McpToolError(errorText || "Unknown error");
	}
	if (result.structuredContent != null) return result.structuredContent;
	const textParts = result.content?.filter((c) => c.type === "text").map((c) => c.text ?? "");
	if (textParts?.length) {
		const text = textParts.join("");
		try {
			return JSON.parse(text);
		} catch {
			return text;
		}
	}
	return null;
}
//#endregion
//#region src/runtime/toolProvider.ts
/**
* Standard error thrown when a tool name is not found in a function-map ToolProvider.
* Used by Renderer's inline normalization to give clear error messages.
*/
var ToolNotFoundError = class extends Error {
	toolName;
	availableTools;
	constructor(toolName, availableTools = []) {
		super(`[openui] No handler for tool "${toolName}". Available: ${availableTools.join(", ") || "(none)"}`);
		this.name = "ToolNotFoundError";
		this.toolName = toolName;
		this.availableTools = availableTools;
	}
};
//#endregion
//#region src/runtime/queryManager.ts
/** JSON.stringify with stable key ordering at all nesting levels. */
function stableStringify(value) {
	return JSON.stringify(value, (_key, val) => {
		if (val && typeof val === "object" && !Array.isArray(val)) {
			const sorted = {};
			for (const k of Object.keys(val).sort()) sorted[k] = val[k];
			return sorted;
		}
		if (val === void 0) return "__undefined__";
		if (typeof val === "number") {
			if (Number.isNaN(val)) return "__NaN__";
			if (val === Infinity) return "__Inf__";
			if (val === -Infinity) return "__-Inf__";
		}
		return val;
	});
}
function buildCacheKey(toolName, args, deps) {
	const depsKey = deps != null ? "::" + stableStringify(deps) : "";
	return toolName + "::" + stableStringify(args) + depsKey;
}
function createQueryManager(toolProvider) {
	const queries = /* @__PURE__ */ new Map();
	const mutations = /* @__PURE__ */ new Map();
	const cache = /* @__PURE__ */ new Map();
	const listeners = /* @__PURE__ */ new Set();
	let snapshot = {
		__openui_loading: [],
		__openui_refetching: [],
		__openui_errors: []
	};
	let snapshotJson = JSON.stringify(snapshot);
	let disposed = false;
	let generation = 0;
	function rebuildSnapshot() {
		const out = {
			__openui_loading: [],
			__openui_refetching: [],
			__openui_errors: []
		};
		for (const [sid, q] of queries) {
			const entry = cache.get(q.cacheKey);
			if (entry && entry.data !== void 0) out[sid] = entry.data;
			else if (q.prevCacheKey) {
				const prev = cache.get(q.prevCacheKey);
				if (prev && prev.data !== void 0) out[sid] = prev.data;
				else out[sid] = q.defaults;
			} else out[sid] = q.defaults;
			if (q.loading) {
				out.__openui_loading.push(sid);
				if (q.everFetched) out.__openui_refetching.push(sid);
			}
			if (q.error) out.__openui_errors.push(q.error);
		}
		for (const [sid, m] of mutations) {
			out[sid] = m.result;
			if (m.error) out.__openui_errors.push(m.error);
		}
		try {
			const outJson = JSON.stringify(out);
			if (outJson === snapshotJson) return false;
			snapshot = out;
			snapshotJson = outJson;
		} catch {
			snapshot = out;
			snapshotJson = "";
		}
		return true;
	}
	function notify() {
		for (const listener of [...listeners]) listener();
	}
	async function executeFetch(cacheKey, statementId) {
		if (!toolProvider) return;
		const q = queries.get(statementId);
		if (!q) return;
		const fetchKey = cacheKey;
		const toolName = q.toolName;
		const args = q.args;
		let entry = cache.get(fetchKey);
		if (!entry) {
			entry = {
				data: void 0,
				inFlight: true
			};
			cache.set(fetchKey, entry);
		} else entry.inFlight = true;
		q.loading = true;
		rebuildSnapshot();
		notify();
		try {
			const data = await toolProvider.callTool(toolName, args ?? {});
			if (disposed) return;
			const current = queries.get(statementId);
			if (!current || current.cacheKey !== fetchKey) {
				entry.inFlight = false;
				return;
			}
			entry.data = data ?? null;
			current.everFetched = true;
			current.error = void 0;
			if (current.prevCacheKey && current.prevCacheKey !== fetchKey) {
				const prevKey = current.prevCacheKey;
				current.prevCacheKey = void 0;
				cleanupCacheEntry(prevKey);
			}
		} catch (err) {
			const current = queries.get(statementId);
			if (current && current.cacheKey === fetchKey) if (err instanceof ToolNotFoundError) current.error = {
				source: "query",
				code: "tool-not-found",
				message: `Query tool "${toolName}" not found`,
				statementId,
				component: "Query",
				toolName,
				hint: err.availableTools.length ? `Available tools: ${err.availableTools.join(", ")}` : void 0
			};
			else if (err instanceof McpToolError) current.error = {
				source: "query",
				code: "mcp-error",
				message: `Query "${toolName}" returned an error: ${err.toolErrorText}`,
				statementId,
				component: "Query",
				toolName
			};
			else current.error = {
				source: "query",
				code: "tool-error",
				message: `Query "${toolName}" failed: ${err instanceof Error ? err.message : String(err)}`,
				statementId,
				component: "Query",
				toolName
			};
			console.error(`Query "${toolName}" failed:`, err);
		} finally {
			entry.inFlight = false;
			const current = queries.get(statementId);
			if (current && current.cacheKey === fetchKey) {
				current.loading = false;
				if (rebuildSnapshot()) notify();
				if (current.needsRefetch) {
					current.needsRefetch = false;
					executeFetch(current.cacheKey, statementId);
				}
			} else if (rebuildSnapshot()) notify();
		}
	}
	/** Remove a cache entry if no query references it. */
	function cleanupCacheEntry(cacheKey) {
		for (const q of queries.values()) if (q.cacheKey === cacheKey || q.prevCacheKey === cacheKey) return;
		cache.delete(cacheKey);
	}
	function evaluateQueries(queryNodes) {
		if (disposed) return;
		const activeIds = new Set(queryNodes.map((n) => n.statementId));
		for (const [sid, q] of queries) if (!activeIds.has(sid)) {
			if (q.timer) clearInterval(q.timer);
			queries.delete(sid);
			cleanupCacheEntry(q.cacheKey);
			if (q.prevCacheKey) cleanupCacheEntry(q.prevCacheKey);
		}
		for (const node of queryNodes) {
			if (!node.complete) continue;
			const cacheKey = buildCacheKey(node.toolName, node.args, node.deps);
			const existing = queries.get(node.statementId);
			if (existing) {
				if (existing.cacheKey !== cacheKey) existing.prevCacheKey = existing.cacheKey;
				existing.toolName = node.toolName;
				existing.args = node.args;
				existing.defaults = node.defaults;
				existing.cacheKey = cacheKey;
			} else queries.set(node.statementId, {
				toolName: node.toolName,
				args: node.args,
				defaults: node.defaults,
				cacheKey,
				loading: false,
				everFetched: false,
				refreshInterval: 0,
				needsRefetch: false
			});
			const q = queries.get(node.statementId);
			const entry = cache.get(cacheKey);
			const hasSettledData = entry && entry.data !== void 0 && !entry.inFlight;
			if (toolProvider && !hasSettledData && !entry?.inFlight) executeFetch(cacheKey, node.statementId);
			const newInterval = node.refreshInterval ?? 0;
			if (newInterval !== q.refreshInterval) {
				if (q.timer) {
					clearInterval(q.timer);
					q.timer = void 0;
				}
				if (newInterval > 0) q.timer = setInterval(() => {
					if (disposed || !toolProvider) return;
					if (!cache.get(q.cacheKey)?.inFlight) executeFetch(q.cacheKey, node.statementId);
				}, newInterval * 1e3);
				q.refreshInterval = newInterval;
			}
		}
		if (rebuildSnapshot()) notify();
	}
	function getResult(statementId) {
		const q = queries.get(statementId);
		if (!q) return null;
		const entry = cache.get(q.cacheKey);
		if (entry && entry.data !== void 0) return entry.data;
		if (q.prevCacheKey) {
			const prev = cache.get(q.prevCacheKey);
			if (prev && prev.data !== void 0) return prev.data;
		}
		return q.defaults;
	}
	function isLoading(statementId) {
		return queries.get(statementId)?.loading ?? false;
	}
	function isAnyLoading() {
		for (const q of queries.values()) if (q.loading) return true;
		return false;
	}
	function invalidate(statementIds) {
		if (disposed || !toolProvider) return;
		const targets = statementIds?.length ? statementIds.filter((sid) => queries.has(sid)) : [...queries.keys()];
		for (const sid of targets) {
			const q = queries.get(sid);
			if (!q) continue;
			if (cache.get(q.cacheKey)?.inFlight) q.needsRefetch = true;
			else executeFetch(q.cacheKey, sid);
		}
	}
	function registerMutations(nodes) {
		const activeIds = new Set(nodes.map((n) => n.statementId));
		for (const sid of mutations.keys()) if (!activeIds.has(sid)) mutations.delete(sid);
		for (const node of nodes) {
			const existing = mutations.get(node.statementId);
			if (existing) {
				if (existing.toolName !== node.toolName) {
					existing.toolName = node.toolName;
					existing.result = {
						status: "idle",
						data: null,
						error: null
					};
					existing.error = void 0;
				}
			} else mutations.set(node.statementId, {
				toolName: node.toolName,
				result: { status: "idle" }
			});
		}
		if (rebuildSnapshot()) notify();
	}
	async function fireMutation(statementId, evaluatedArgs, refreshQueryIds) {
		if (disposed || !toolProvider) return false;
		const m = mutations.get(statementId);
		if (!m) return false;
		if (m.result.status === "loading") return false;
		const gen = generation;
		m.result = { status: "loading" };
		rebuildSnapshot();
		notify();
		let success = false;
		try {
			const data = await toolProvider.callTool(m.toolName, evaluatedArgs);
			if (disposed || gen !== generation) return false;
			m.result = {
				status: "success",
				data
			};
			m.error = void 0;
			success = true;
		} catch (err) {
			if (disposed || gen !== generation) return false;
			const msg = err instanceof Error ? err.message : String(err);
			m.result = {
				status: "error",
				error: msg
			};
			if (err instanceof ToolNotFoundError) m.error = {
				source: "mutation",
				code: "tool-not-found",
				message: `Mutation tool "${m.toolName}" not found`,
				statementId,
				component: "Mutation",
				toolName: m.toolName,
				hint: err.availableTools.length ? `Available tools: ${err.availableTools.join(", ")}` : void 0
			};
			else if (err instanceof McpToolError) m.error = {
				source: "mutation",
				code: "mcp-error",
				message: `Mutation "${m.toolName}" returned an error: ${err.toolErrorText}`,
				statementId,
				component: "Mutation",
				toolName: m.toolName
			};
			else m.error = {
				source: "mutation",
				code: "tool-error",
				message: `Mutation "${m.toolName}" failed: ${msg}`,
				statementId,
				component: "Mutation",
				toolName: m.toolName
			};
		}
		rebuildSnapshot();
		notify();
		if (success && refreshQueryIds?.length) invalidate(refreshQueryIds);
		return success;
	}
	function getMutationResult(statementId) {
		return mutations.get(statementId)?.result ?? null;
	}
	function subscribe(listener) {
		listeners.add(listener);
		return () => listeners.delete(listener);
	}
	function getSnapshot() {
		return snapshot;
	}
	function activate() {
		disposed = false;
	}
	function dispose() {
		disposed = true;
		generation++;
		listeners.clear();
		for (const q of queries.values()) {
			if (q.timer) {
				clearInterval(q.timer);
				q.timer = void 0;
			}
			q.refreshInterval = 0;
			q.loading = false;
			q.needsRefetch = false;
		}
		mutations.clear();
	}
	return {
		evaluateQueries,
		getResult,
		isLoading,
		isAnyLoading,
		invalidate,
		registerMutations,
		fireMutation,
		getMutationResult,
		subscribe,
		getSnapshot,
		activate,
		dispose
	};
}
//#endregion
//#region src/runtime/state-field.ts
function resolveStateField(name, bindingValue, store, evaluationContext, fieldGetter, fieldSetter) {
	if (isReactiveAssign(bindingValue) && store && evaluationContext) {
		const { target, expr } = bindingValue;
		return {
			name,
			value: store.get(target),
			setValue: (value) => {
				const extraScope = { $value: value };
				const nextValue = evaluate(expr, {
					...evaluationContext,
					extraScope
				});
				store.set(target, nextValue);
			},
			isReactive: true
		};
	}
	return {
		name,
		value: fieldGetter(name) ?? bindingValue,
		setValue: (value) => fieldSetter(name, value),
		isReactive: false
	};
}
//#endregion
//#region src/runtime/store.ts
function createStore() {
	const state = /* @__PURE__ */ new Map();
	const pristine = /* @__PURE__ */ new Set();
	const listeners = /* @__PURE__ */ new Set();
	let snapshot = {};
	function notify() {
		const currentListeners = [...listeners];
		for (const listener of currentListeners) listener();
	}
	function rebuildSnapshot() {
		snapshot = Object.fromEntries(state);
	}
	function get(name) {
		return state.get(name);
	}
	function set(name, value, options) {
		if (options?.pristine) pristine.add(name);
		else pristine.delete(name);
		const existing = state.get(name);
		if (Object.is(existing, value)) return;
		if (value && existing && typeof value === "object" && typeof existing === "object" && !Array.isArray(value) && !Array.isArray(existing)) {
			const nk = Object.keys(value);
			const ok = Object.keys(existing);
			if (nk.length === ok.length && nk.every((k) => Object.is(value[k], existing[k]))) return;
		}
		state.set(name, value);
		rebuildSnapshot();
		notify();
	}
	function subscribe(listener) {
		listeners.add(listener);
		return () => {
			listeners.delete(listener);
		};
	}
	function getSnapshot() {
		return snapshot;
	}
	function initialize(defaults, persisted) {
		let changed = false;
		for (const key of Object.keys(persisted)) {
			pristine.delete(key);
			if (!state.has(key) || !Object.is(state.get(key), persisted[key])) {
				state.set(key, persisted[key]);
				changed = true;
			}
		}
		for (const key of Object.keys(defaults)) if (!state.has(key)) {
			state.set(key, defaults[key]);
			pristine.add(key);
			changed = true;
		} else if (pristine.has(key) && !Object.is(state.get(key), defaults[key])) {
			state.set(key, defaults[key]);
			changed = true;
		}
		if (changed) {
			rebuildSnapshot();
			notify();
		}
	}
	function dispose() {
		state.clear();
		pristine.clear();
		listeners.clear();
		snapshot = {};
	}
	return {
		get,
		set,
		subscribe,
		getSnapshot,
		initialize,
		dispose
	};
}
//#endregion
//#region src/utils/validation.ts
/**
* Parse a rule string into a structured rule.
*   "required"       → { type: "required" }
*   "min:8"          → { type: "min", arg: 8 }
*   "minLength:3"    → { type: "minLength", arg: 3 }
*   "pattern:^[a-z]" → { type: "pattern", arg: "^[a-z]" }
*/
const NUMERIC_RULES = new Set([
	"min",
	"max",
	"minLength",
	"maxLength"
]);
function parseRule(rule) {
	const colonIdx = rule.indexOf(":");
	if (colonIdx === -1) return { type: rule };
	const type = rule.slice(0, colonIdx);
	const rawArg = rule.slice(colonIdx + 1);
	return {
		type,
		arg: NUMERIC_RULES.has(type) && !Number.isNaN(Number(rawArg)) ? Number(rawArg) : rawArg
	};
}
function parseRules(rules) {
	if (!Array.isArray(rules)) return [];
	return rules.filter((r) => typeof r === "string").map(parseRule);
}
function isEmpty(value) {
	if (value === null || value === void 0 || value === "") return true;
	if (Array.isArray(value) && value.length === 0) return true;
	if (value && typeof value === "object" && !Array.isArray(value) && "value" in value) return isEmpty(value.value);
	if (typeof value === "object" && value !== null && !Array.isArray(value) && Object.keys(value).length === 0) return true;
	return false;
}
const builtInValidators = {
	required: (value) => {
		if (isEmpty(value)) return "This field is required";
		if (typeof value === "object" && !Array.isArray(value) && value !== null) {
			const vals = Object.values(value);
			if (vals.length > 0 && vals.every((v) => typeof v === "boolean") && !vals.some(Boolean)) return "At least one option is required";
		}
	},
	email: (value) => {
		if (isEmpty(value)) return void 0;
		if (typeof value !== "string") return "Please enter a valid email";
		return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value) ? void 0 : "Please enter a valid email";
	},
	url: (value) => {
		if (isEmpty(value)) return void 0;
		if (typeof value !== "string") return "Please enter a valid URL";
		try {
			new URL(value);
			return;
		} catch {
			return "Please enter a valid URL";
		}
	},
	numeric: (value) => {
		if (isEmpty(value)) return void 0;
		if (typeof value === "number" && !isNaN(value)) return void 0;
		if (typeof value === "string" && !isNaN(parseFloat(value)) && value.trim() !== "") return void 0;
		return "Must be a number";
	},
	min: (value, arg) => {
		if (isEmpty(value)) return void 0;
		const n = typeof value === "number" ? value : parseFloat(String(value));
		if (isNaN(n)) return void 0;
		const min = Number(arg);
		return n >= min ? void 0 : `Must be at least ${min}`;
	},
	max: (value, arg) => {
		if (isEmpty(value)) return void 0;
		const n = typeof value === "number" ? value : parseFloat(String(value));
		if (isNaN(n)) return void 0;
		const max = Number(arg);
		return n <= max ? void 0 : `Must be no more than ${max}`;
	},
	minLength: (value, arg) => {
		if (isEmpty(value)) return void 0;
		if (typeof value !== "string") return void 0;
		const min = Number(arg);
		return value.length >= min ? void 0 : `Must be at least ${min} characters`;
	},
	maxLength: (value, arg) => {
		if (isEmpty(value)) return void 0;
		if (typeof value !== "string") return void 0;
		const max = Number(arg);
		return value.length <= max ? void 0 : `Must be no more than ${max} characters`;
	},
	pattern: (value, arg) => {
		if (isEmpty(value)) return void 0;
		if (typeof value !== "string" || typeof arg !== "string") return void 0;
		try {
			return new RegExp(arg).test(value) ? void 0 : "Invalid format";
		} catch {
			return;
		}
	}
};
/**
* Run all rules against a value. Stop on first error.
* Custom validators are checked first, then built-in ones.
*/
function validate(value, rules, customValidators) {
	for (const rule of rules) {
		const validator = customValidators?.[rule.type] ?? builtInValidators[rule.type];
		if (!validator) {
			console.warn(`[openui] Unknown validation rule type: "${rule.type}"`);
			continue;
		}
		const error = validator(value, rule.arg);
		if (error) return error;
	}
}
/**
* Parse a structured rules object into ParsedRule[].
* Accepts: { required: true, minLength: 5, email: true, max: 100 }
* Skips keys with false/undefined values.
*/
function parseStructuredRules(rules) {
	if (!rules || typeof rules !== "object" || Array.isArray(rules)) return [];
	const obj = rules;
	const result = [];
	for (const [key, val] of Object.entries(obj)) {
		if (val === false || val === void 0 || val === null) continue;
		if (val === true) result.push({ type: key });
		else result.push({
			type: key,
			arg: val
		});
	}
	return result;
}
//#endregion
export { ACTION_STEPS, BuiltinActionType, McpToolError, ToolNotFoundError, action, autoClose, buildMessage, buildSignature, builtInValidators, createLibrary, createParser, createQueryManager, createStore, createStreamingParser, defineAction, defineComponent, defineFunction, enrichErrors, evaluate, evaluateElementProps, evaluateRoot, extractToolResult, generatePrompt, generateSystemPrompt, isReactiveAssign, isReactiveSchema, jsonToOpenUI, markReactive, mergeStatements, parse, parseExpression, parseMessage, parseRules, parseStructuredRules, resolveStateField, split, steps, stripReactiveAssign, tagSchemaId, tokenize, validate, walkAST };

//# sourceMappingURL=index.mjs.map