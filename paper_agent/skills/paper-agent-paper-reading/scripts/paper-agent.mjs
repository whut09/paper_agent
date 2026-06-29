#!/usr/bin/env node

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

function parseArgs(argv) {
  const options = {
    mode: "summarize",
    input: "",
    output: "paper_agent_files",
    config: "",
    pages: "",
    summaryLanguage: "中文",
    maxAssets: "13",
    service: "openai",
    langIn: "en",
    langOut: "zh",
    thread: "4",
    translateMode: "fast",
    prompt: "",
    help: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const next = argv[index + 1];
    if (arg === "--help" || arg === "-h") {
      options.help = true;
    } else if (arg === "--mode") {
      options.mode = next;
      index += 1;
    } else if (arg === "--input") {
      options.input = next;
      index += 1;
    } else if (arg === "--output") {
      options.output = next;
      index += 1;
    } else if (arg === "--config") {
      options.config = next;
      index += 1;
    } else if (arg === "--pages") {
      options.pages = next;
      index += 1;
    } else if (arg === "--summary-language") {
      options.summaryLanguage = next;
      index += 1;
    } else if (arg === "--max-assets") {
      options.maxAssets = next;
      index += 1;
    } else if (arg === "--service") {
      options.service = next;
      index += 1;
    } else if (arg === "--lang-in") {
      options.langIn = next;
      index += 1;
    } else if (arg === "--lang-out") {
      options.langOut = next;
      index += 1;
    } else if (arg === "--thread") {
      options.thread = next;
      index += 1;
    } else if (arg === "--translate-mode") {
      options.translateMode = next;
      index += 1;
    } else if (arg === "--prompt") {
      options.prompt = next;
      index += 1;
    } else if (!arg.startsWith("--") && !options.input) {
      options.input = arg;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return options;
}

function findRepoRoot() {
  let current = path.dirname(fileURLToPath(import.meta.url));
  for (let depth = 0; depth < 8; depth += 1) {
    if (existsSync(path.join(current, "pyproject.toml")) && existsSync(path.join(current, "paper_agent"))) {
      return current;
    }
    current = path.dirname(current);
  }
  throw new Error("Could not find PaperAgent repository root from skill script.");
}

function buildPythonArgs(options) {
  if (!options.input) {
    throw new Error("Missing --input <paper.pdf>.");
  }

  if (options.mode === "summarize") {
    const args = [
      "-m",
      "paper_agent",
      "summarize",
      options.input,
      "--output",
      options.output,
      "--summary-language",
      options.summaryLanguage,
      "--max-assets",
      options.maxAssets,
    ];
    if (options.config) {
      args.push("--config", options.config);
    }
    if (options.pages) {
      args.push("--pages", options.pages);
    }
    return args;
  }

  if (options.mode === "translate") {
    const args = [
      "-m",
      "paper_agent",
      options.input,
      "--output",
      options.output,
      "--service",
      options.service,
      "--lang-in",
      options.langIn,
      "--lang-out",
      options.langOut,
      "--thread",
      options.thread,
      "--mode",
      options.translateMode,
    ];
    if (options.config) {
      args.push("--config", options.config);
    }
    if (options.pages) {
      args.push("--pages", options.pages);
    }
    if (options.prompt) {
      args.push("--prompt", options.prompt);
    }
    return args;
  }

  throw new Error(`Unsupported --mode ${options.mode}. Use summarize or translate.`);
}

const options = parseArgs(process.argv.slice(2));
if (options.help) {
  process.stdout.write(
    [
      "PaperAgent SkillBridge entrypoint",
      "",
      "Summary:",
      "  --mode summarize --input <paper.pdf> --output <dir> --config <config.local.json>",
      "",
      "Translation:",
      "  --mode translate --input <paper.pdf> --output <dir> --config <config.local.json> --service openai",
      "",
    ].join("\n"),
  );
  process.exit(0);
}
const repoRoot = findRepoRoot();
const python = process.env.PAPER_AGENT_PYTHON || "python";
const child = spawn(python, buildPythonArgs(options), {
  cwd: repoRoot,
  shell: false,
  stdio: ["ignore", "pipe", "pipe"],
});

child.stdout.on("data", (chunk) => process.stdout.write(chunk));
child.stderr.on("data", (chunk) => process.stderr.write(chunk));
child.on("error", (error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
child.on("close", (exitCode) => {
  process.exitCode = exitCode ?? 1;
});
