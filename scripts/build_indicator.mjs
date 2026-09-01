import { readFile, rename, rm, writeFile } from "node:fs/promises";
import { build } from "esbuild";

const entryPoint = "assets/parallel-indicator-host.js";
const outputFile = "assets/parallel-indicator-host.bundle.js";
const provenanceFile = "third_party/bundle-dependencies.json";
const zodEvalProbe =
  /export const allowsEval = \/\* @__PURE__\*\/ cached\(\(\) => \{[\s\S]*?\n\}\);/g;

const disableZodEvalProbe = {
  name: "disable-zod-eval-probe",
  setup(context) {
    context.onLoad(
      { filter: /[\\/]zod[\\/]v4[\\/]core[\\/]util\.js$/ },
      async (args) => {
        const original = await readFile(args.path, "utf8");
        const matches = original.match(zodEvalProbe) ?? [];
        if (matches.length !== 1) {
          throw new Error(
            `Expected one Zod eval capability probe, found ${matches.length}`,
          );
        }

        return {
          contents: original.replace(
            zodEvalProbe,
            "export const allowsEval = /* @__PURE__*/ cached(() => false);",
          ),
          loader: "js",
        };
      },
    );
  },
};

const result = await build({
  entryPoints: [entryPoint],
  outfile: outputFile,
  bundle: true,
  format: "iife",
  target: "es2020",
  minify: true,
  legalComments: "none",
  metafile: true,
  plugins: [disableZodEvalProbe],
  write: false,
});

const lock = JSON.parse(await readFile("package-lock.json", "utf8"));
const provenance = JSON.parse(await readFile(provenanceFile, "utf8"));
if (
  provenance.schema_version !== "1.0" ||
  provenance.bundle !== outputFile ||
  !Array.isArray(provenance.dependencies)
) {
  throw new Error(
    `Invalid browser bundle provenance manifest: ${provenanceFile}`,
  );
}
const expectedBundledPackages = new Set(
  provenance.dependencies.map(({ name, version }) => `${name}@${version}`),
);
if (expectedBundledPackages.size !== provenance.dependencies.length) {
  throw new Error(`Duplicate browser dependency identity in ${provenanceFile}`);
}
const bundledPackages = new Set();
const emittedInputs = Object.values(result.metafile.outputs).flatMap((output) =>
  Object.entries(output.inputs ?? {})
    .filter(([, contribution]) => contribution.bytesInOutput > 0)
    .map(([input]) => input),
);
for (const input of emittedInputs) {
  const parts = input.replaceAll("\\", "/").split("/");
  const nodeModulesIndex = parts.lastIndexOf("node_modules");
  if (nodeModulesIndex === -1) continue;
  const scoped = parts[nodeModulesIndex + 1]?.startsWith("@");
  const packageEnd = nodeModulesIndex + (scoped ? 3 : 2);
  const lockKey = parts.slice(0, packageEnd).join("/");
  const name = parts.slice(nodeModulesIndex + 1, packageEnd).join("/");
  const version = lock.packages?.[lockKey]?.version;
  if (!name || !version) {
    throw new Error(`Cannot bind bundled input to package-lock: ${input}`);
  }
  bundledPackages.add(`${name}@${version}`);
}
const actualPackages = [...bundledPackages].sort();
const expectedPackages = [...expectedBundledPackages].sort();
if (JSON.stringify(actualPackages) !== JSON.stringify(expectedPackages)) {
  throw new Error(
    `Browser bundle dependency set changed: expected ${expectedPackages.join(", ")}; ` +
      `found ${actualPackages.join(", ")}`,
  );
}

if (result.outputFiles?.length !== 1) {
  throw new Error(
    `Expected one browser bundle output, found ${result.outputFiles?.length}`,
  );
}
const [builtOutput] = result.outputFiles;
const output = builtOutput.text;
if (/\bnew\s+Function\s*\(/.test(output) || /\beval\s*\(/.test(output)) {
  throw new Error(
    "Refusing to publish a UI bundle containing dynamic code evaluation",
  );
}

const temporaryOutput = `${outputFile}.${process.pid}.tmp`;
try {
  await writeFile(temporaryOutput, builtOutput.contents);
  await rename(temporaryOutput, outputFile);
} finally {
  await rm(temporaryOutput, { force: true });
}
