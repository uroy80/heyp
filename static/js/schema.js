// Fit UI values onto a registry tool's options schema (pydantic v2 JSON schema from GET /api/tools): pick the
// property name the server actually uses, coerce to its type, clamp to its range and keep enum values valid.
// The server validates again (extra="forbid"), so this only keeps requests from failing on naming details.

/** Resolve `$ref` / `anyOf` / `allOf` wrappers of a property schema (Optional[...] becomes anyOf [T, null]). */
export function resolveProp(schema, prop) {
  let p = prop;
  for (let i = 0; i < 6 && p; i++) {
    if (p.$ref) {
      const name = String(p.$ref).split('/').pop();
      p = { ...(schema?.$defs?.[name] || schema?.definitions?.[name] || {}), ...withoutKeys(p, ['$ref']) };
      continue;
    }
    const alt = p.anyOf || p.oneOf;
    if (Array.isArray(alt)) {
      const pick = alt.find((x) => x && x.type !== 'null') || alt[0];
      p = { ...withoutKeys(p, ['anyOf', 'oneOf']), ...pick, nullable: alt.some((x) => x?.type === 'null') };
      continue;
    }
    if (Array.isArray(p.allOf) && p.allOf.length === 1) {
      p = { ...withoutKeys(p, ['allOf']), ...p.allOf[0] };
      continue;
    }
    break;
  }
  return p || null;
}

function withoutKeys(o, keys) {
  const out = { ...o };
  for (const k of keys) delete out[k];
  return out;
}

/** UI hint block of a property (Field(json_schema_extra={"ui": {...}})). */
export const uiHint = (p) => (p && typeof p.ui === 'object' ? p.ui : {});

function coerce(p, value) {
  if (value == null) return p?.nullable ? null : undefined;
  const type = Array.isArray(p?.type) ? p.type.find((t) => t !== 'null') : p?.type;
  let v = value;
  if (type === 'integer') v = Math.round(Number(v));
  else if (type === 'number') v = Number(v);
  else if (type === 'boolean') v = Boolean(v);
  else if (type === 'string') v = String(v);
  if ((type === 'integer' || type === 'number') && !Number.isFinite(v)) return undefined;
  if (typeof v === 'number') {
    if (p.minimum != null) v = Math.max(p.minimum, v);
    if (p.maximum != null) v = Math.min(p.maximum, v);
    if (p.exclusiveMinimum != null && v <= p.exclusiveMinimum) v = type === 'integer' ? p.exclusiveMinimum + 1 : p.exclusiveMinimum + 1e-6;
    if (p.exclusiveMaximum != null && v >= p.exclusiveMaximum) v = type === 'integer' ? p.exclusiveMaximum - 1 : p.exclusiveMaximum - 1e-6;
  }
  const choices = p.enum || (p.const !== undefined ? [p.const] : null);
  if (choices && !choices.includes(v)) {
    if (typeof v === 'number' && choices.every((c) => typeof c === 'number')) {
      v = choices.reduce((best, c) => (Math.abs(c - v) < Math.abs(best - v) ? c : best), choices[0]);
    } else {
      const lower = choices.find((c) => String(c).toLowerCase() === String(v).toLowerCase());
      v = lower !== undefined ? lower : p.default !== undefined ? p.default : choices[0];
    }
  }
  return v;
}

/**
 * Build an options object. `fields` = [{names: [...], value, unit?: 'ms' | 's'}]: the first name that exists in
 * the schema wins (without a schema the first name is used as is). A field whose names all are missing from a
 * known schema is left out. A field marked with unit 'ms' is converted when the server's property counts
 * seconds (name ends in "_s" / "seconds", or ui.unit "s").
 */
export function fitOptions(schema, fields) {
  const props = schema?.properties || null;
  const out = {};
  for (const f of fields) {
    if (f.value === undefined) continue;
    if (!props) {
      out[f.names[0]] = f.value;
      continue;
    }
    const name = f.names.find((n) => Object.prototype.hasOwnProperty.call(props, n));
    if (!name) continue;
    const p = resolveProp(schema, props[name]);
    let value = f.value;
    if (f.unit === 'ms' && typeof value === 'number') {
      const unit = uiHint(p).unit || '';
      if (/(^|_)s$|seconds?$/.test(name) || unit === 's') value /= 1000;
    }
    const v = coerce(p, value);
    if (v !== undefined) out[name] = v;
  }
  return out;
}

/** Enum values of a property (first matching name), e.g. allowed widths or formats; null when free. */
export function enumOf(schema, names) {
  const props = schema?.properties;
  if (!props) return null;
  const name = names.find((n) => props[n]);
  if (!name) return null;
  const p = resolveProp(schema, props[name]);
  return Array.isArray(p?.enum) ? p.enum : null;
}

/** Range {min, max, default} of a numeric property (first matching name) or null. */
export function rangeOf(schema, names) {
  const props = schema?.properties;
  if (!props) return null;
  const name = names.find((n) => props[n]);
  if (!name) return null;
  const p = resolveProp(schema, props[name]);
  return { name, min: p?.minimum ?? null, max: p?.maximum ?? null, default: p?.default ?? null };
}
