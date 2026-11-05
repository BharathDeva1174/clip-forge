/**
 * The dependency-audit baseline.
 *
 * `npm audit` reports advisories in the dev-only ESLint chain. They are
 * accepted, not fixed, and each acceptance is recorded here:
 *
 * - GHSA-vfj7-8cjw-p6xm (npm advisory 1240992) against `braces`: there is
 *   no patched `braces` release -- the advisory covers every published
 *   version (3.0.3 is the latest) -- and the only fix npm offers is a
 *   semver-major downgrade of `eslint-config-next` to v14, which is an
 *   eslintrc-style config and breaks the flat config this project uses.
 *
 * - GHSA-g3hv-75wg-m249 (npm advisory 1244012) against `minimatch` < 11
 *   (globparts amplification, moderate): accepted on 2026-10-31. The whole
 *   ESLint toolchain sits on minimatch 10 through `@eslint/config-array`,
 *   and the only fix npm offers is downgrading `eslint` to v4. It is a
 *   lint-tooling dependency: nothing in it ships to the browser or runs in
 *   the backend.
 *
 * Accepting an advisory is only safe while it is the *same* advisory. This
 * script fails the moment a new one appears, so the exception cannot grow
 * silently.
 *
 * Run it with `npm run audit:check`.
 */

import { execFileSync } from "node:child_process";

/** The advisories that are accepted, by their npm advisory ids. */
const ACCEPTED_ADVISORIES = new Set([1240992, 1244012]);

/** The packages the accepted advisories are reported against. */
const ACCEPTED_PACKAGES = new Set([
  "braces",
  "micromatch",
  "fast-glob",
  "minimatch",
  "@eslint/config-array",
  "@eslint-community/eslint-utils",
  "@eslint/eslintrc",
  "@next/eslint-plugin-next",
  "eslint",
  "eslint-config-next",
  "typescript-eslint",
  "@typescript-eslint/eslint-plugin",
  "@typescript-eslint/parser",
  "@typescript-eslint/type-utils",
  "@typescript-eslint/typescript-estree",
  "@typescript-eslint/utils",
  "eslint-plugin-jsx-a11y",
  "eslint-plugin-react",
  "eslint-plugin-import",
]);

/** Read the audit report as JSON. */
function readReport() {
  let raw = "";
  try {
    // The command is fixed and contains nothing that needs quoting. A shell
    // is used because `npm` is a batch file on Windows.
    raw = execFileSync("npm audit --json", {
      encoding: "utf8",
      shell: true,
      stdio: ["ignore", "pipe", "ignore"],
    });
  } catch (error) {
    // `npm audit` exits non-zero when it finds something, which is the
    // normal case here. The report is on stdout either way.
    raw = error.stdout ?? "";
  }
  if (!raw.trim()) {
    return null;
  }
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/**
 * Every advisory id behind a finding.
 *
 * `via` points at what a package depends on, so the chain is followed
 * downwards: `eslint-config-next` -> `@next/eslint-plugin-next` ->
 * `fast-glob` -> `micromatch` -> `braces`, where the advisory itself is.
 */
function advisoryIds(name, finding, vulnerabilities, seen = new Set()) {
  const ids = new Set();
  const via = Array.isArray(finding.via) ? finding.via : [];

  for (const item of via) {
    if (typeof item === "string") {
      if (seen.has(item)) {
        continue;
      }
      seen.add(item);
      const next = vulnerabilities[item];
      if (next) {
        for (const id of advisoryIds(item, next, vulnerabilities, seen)) {
          ids.add(id);
        }
      }
      continue;
    }
    if (item && typeof item === "object" && typeof item.source === "number") {
      ids.add(item.source);
    }
  }
  return ids;
}

const report = readReport();

if (!report) {
  console.error("The audit report could not be read.");
  process.exit(2);
}

const vulnerabilities = report.vulnerabilities ?? {};
const unexpected = [];

for (const [name, finding] of Object.entries(vulnerabilities)) {
  const ids = advisoryIds(name, finding, vulnerabilities);
  const onlyAccepted = ids.size > 0 && [...ids].every((id) => ACCEPTED_ADVISORIES.has(id));
  const knownPackage = ACCEPTED_PACKAGES.has(name);

  if (!onlyAccepted || !knownPackage) {
    unexpected.push({ name, severity: finding.severity, advisories: [...ids] });
  }
}

if (unexpected.length > 0) {
  console.error("New dependency advisories were found:\n");
  for (const item of unexpected) {
    console.error(
      `  ${item.name} (${item.severity}) advisory ids: ${item.advisories.join(", ") || "unknown"}`,
    );
  }
  console.error(
    "\nThese are not the accepted advisories (braces GHSA-vfj7-8cjw-p6xm, " +
      "minimatch GHSA-g3hv-75wg-m249). Fix them, or record the decision in " +
      "PHASE_3_1_REPORT.md and update ACCEPTED_ADVISORIES in " +
      "scripts/check-audit.mjs.",
  );
  process.exit(1);
}

const counts = report.metadata?.vulnerabilities ?? {};
console.log(
  `The audit reports ${counts.high ?? 0} high, ${counts.critical ?? 0} critical ` +
    `and ${counts.total ?? 0} total advisories. All of them are the accepted ` +
    "advisories (braces GHSA-vfj7-8cjw-p6xm, minimatch GHSA-g3hv-75wg-m249) " +
    "in the dev-only ESLint chain.",
);
