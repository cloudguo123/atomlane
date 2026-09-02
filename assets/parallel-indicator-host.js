import {
  App,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
} from "@modelcontextprotocol/ext-apps";

function applyHostContext(context) {
  if (!context) return;
  if (context.theme) applyDocumentTheme(context.theme);
  if (context.styles?.variables)
    applyHostStyleVariables(context.styles.variables);
  if (context.styles?.css?.fonts) applyHostFonts(context.styles.css.fonts);
}

function publish(name, detail) {
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

const app = new App(
  { name: "AtomLane Live Indicator", version: "0.14.0" },
  { availableDisplayModes: ["inline"] },
  { autoResize: true },
);

app.addEventListener("hostcontextchanged", applyHostContext);
app.addEventListener("toolinputpartial", (params) => {
  publish("atomlane:tool-input", params?.arguments || {});
});
app.addEventListener("toolinput", (params) => {
  publish("atomlane:tool-input", params?.arguments || {});
});
app.addEventListener("toolresult", (result) => {
  publish(
    "atomlane:tool-result",
    result?.structuredContent || result,
  );
});
app.addEventListener("toolcancelled", (params) => {
  publish("atomlane:tool-cancelled", params || {});
});

app
  .connect()
  .then(() => {
    applyHostContext(app.getHostContext());
    publish("atomlane:host-ready", {});
  })
  .catch(() => {
    // A raw file preview has no MCP Apps host; the HTML supplies a safe demo state.
  });
