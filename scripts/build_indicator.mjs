import { readFile } from "node:fs/promises";
import { build } from "esbuild";

const entryPoint = "assets/parallel-indicator-host.js";
const outputFile = "assets/parallel-indicator-host.bundle.js";
const zodEvalProbe = /try\{return new Function\(""\),!0\}catch(?:\([^)]*\))?\{return!1\}/g;

const disableBundledEvalProbe = {
  name: "disable-bundled-eval-probe",
  setup(context) {
    context.onLoad({ filter: /app-with-deps\.js$/ }, async (args) => {
      if (!args.path.includes("@modelcontextprotocol/ext-apps")) return null;

      const original = await readFile(args.path, "utf8");
      const matches = original.match(zodEvalProbe) ?? [];
      if (matches.length !== 1) {
        throw new Error(
          `Expected one bundled Zod eval capability probe, found ${matches.length}`,
        );
      }

      return {
        contents: original.replace(zodEvalProbe, "return!1"),
        loader: "js",
      };
    });
  },
};

await build({
  entryPoints: [entryPoint],
  outfile: outputFile,
  bundle: true,
  format: "iife",
  target: "es2020",
  minify: true,
  legalComments: "none",
  plugins: [disableBundledEvalProbe],
});

const output = await readFile(outputFile, "utf8");
if (/\bnew\s+Function\s*\(/.test(output) || /\beval\s*\(/.test(output)) {
  throw new Error("Refusing to publish a UI bundle containing dynamic code evaluation");
}
