#!/usr/bin/env python3
"""Build one immutable, browser-confined Verso rendering of an accepted Challenge source."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.render_report import (  # noqa: E402
    AcceptedRenderPaths,
    PreparedRenderReport,
    RenderReportError,
    RenderSource,
    intake_report,
    parse_prepared_report,
)
from scripts.submission_contract import GITHUB_RE, SHA_RE  # noqa: E402
from scripts.verification_errors import VerificationError  # noqa: E402
from scripts.verify_submission import (  # noqa: E402
    MAX_SOURCE_BYTES,
    LeanHeader,
    clone_commit,
    ensure_lake_manifest,
    github_repository,
    load_comparator_config,
    manifest_packages,
    materialize_packages,
    module_source_suffix,
    normalized_repository_path,
    now,
    parse_lean_header,
    require_protected_paths,
    resolve_release_commit,
    resolve_repository_path,
    run,
    sandboxed_run,
    sha256,
    supported_toolchain,
    systemd_command,
    tool_snapshot,
    tree_size,
    verify_sandbox_confinement,
    write_json,
)

VERSO_REPOSITORY = "leanprover/verso"

MAX_RENDER_FILES = 2_000
MAX_RENDER_NODES = 4_000
MAX_RENDER_FILE_BYTES = 8 * 1024 * 1024
MAX_RENDER_BYTES = 25 * 1024 * 1024
MAX_RENDER_WORK_BYTES = 12 * 1024 * 1024 * 1024
MAX_BUILD_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_CACHE_ARCHIVES = 10_000
MAX_CACHE_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_CACHE_BYTES = 8 * 1024 * 1024 * 1024
# GitHub-hosted Actions jobs may run for at most six hours. Keep the confined
# build's own deadline at that ceiling too; the workflow job remains the final
# authority if setup time means it reaches GitHub's limit first.
BUILD_TIMEOUT_SECONDS = 6 * 60 * 60
HTML_TIMEOUT_SECONDS = 10 * 60
SANITIZE_TIMEOUT_SECONDS = 2 * 60
RESOURCE_PROPERTIES = (
    "MemoryMax=12G",
    "TasksMax=512",
    "LimitNOFILE=4096",
    f"LimitFSIZE={MAX_BUILD_FILE_BYTES}",
    f"RuntimeMaxSec={BUILD_TIMEOUT_SECONDS}",
    "CPUQuota=400%",
)
CACHE_DOWNLOAD_PROPERTIES = (
    "MemoryMax=2G",
    "TasksMax=256",
    "LimitNOFILE=16384",
    f"LimitFSIZE={MAX_CACHE_ARCHIVE_BYTES}",
    "RuntimeMaxSec=1800",
)

# Mathlib's canonical cache moved new master-built artifacts to the
# ``mathlib4-master`` container. The old flat ``mathlib4`` container remains a
# read-only legacy fallback for artifacts from before the cutover. Keep this
# fixed, trusted chain local rather than executing submitted cache policy; in
# particular, do not read fork containers whose writers are not trusted.
MATHLIB_CACHE_BASE_URLS = (
    "https://lakecache.blob.core.windows.net/mathlib4-master",
    "https://lakecache.blob.core.windows.net/mathlib4",
)

ALLOWED_EXTENSIONS = {
    ".css",
    ".gif",
    ".html",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".md",
    ".png",
    ".ts",
    ".txt",
    ".webp",
    ".woff",
    ".woff2",
}

RUNTIME_SANITIZER = r'''(() => {
  "use strict";
  // SVG and MathML carry their own presentation and geometry attributes, and a
  // `dialog` draws itself over the document without needing any. The
  // literate-code genre emits none of the three, so their subtrees go rather
  // than being filtered attribute by attribute. Foreign tag names keep their
  // own case, so match on the folded one.
  const blocked = new Set([
    "APPLET", "BASE", "DIALOG", "EMBED", "FORM", "FRAME", "FRAMESET", "IFRAME",
    "MATH", "META", "OBJECT", "SCRIPT", "SVG"
  ]);
  const activeAttributes = new Set([
    "action", "formaction", "ping", "srcdoc"
  ]);
  // Presentation and visibility without a stylesheet: the legacy colour
  // attributes, the sizing pair, the top-layer controls, and the one that
  // hides honest content so that dishonest content can stand in for it. Verso
  // emits none of them here, `<meta name="viewport">` being a meta element and
  // dropped whole.
  const presentationAttributes = new Set([
    "alink", "background", "bgcolor", "height", "hidden", "link", "popover",
    "popovertarget", "popovertargetaction", "text", "vlink", "width"
  ]);
  // Verso's literate-code output needs one inline declaration, the indent
  // custom property its stylesheet reads, whose value is the source column the
  // documentation starts at. Any other one is enough to reposition or repaint
  // a page served with `style-src 'unsafe-inline'`. The whitespace is spelled
  // out because `\s` does not mean the same thing here as it does in Python.
  const allowedStyle = /^[ \t\r\n\f]*--indent:[ \t\r\n\f]*[0-9]{1,2}[ \t\r\n\f]*;?[ \t\r\n\f]*$/;

  function safeUrl(value, attribute) {
    const trimmed = value.trim();
    if (!trimmed || trimmed.startsWith("#") || trimmed.startsWith("./")) return true;
    if (trimmed.startsWith("../")) {
      if (trimmed.includes("\\")) return false;
      try {
        return !trimmed.slice(3).split("/")
          .map((part) => decodeURIComponent(part).toLowerCase())
          .some((part) => part === "." || part === "..");
      } catch (_) {
        return false;
      }
    }
    return attribute === "src" && /^data:image\/(?:gif|jpeg|png|webp);/i.test(trimmed);
  }

  function sanitize(root) {
    for (const element of Array.from(root.querySelectorAll("*"))) {
      if (blocked.has(element.tagName.toUpperCase())) {
        element.remove();
        continue;
      }
      for (const attribute of Array.from(element.attributes)) {
        const name = attribute.name.toLowerCase();
        if (name.startsWith("on") || activeAttributes.has(name)
            || presentationAttributes.has(name)) {
          element.removeAttribute(attribute.name);
        } else if (name === "open" && element.tagName.toUpperCase() !== "DETAILS") {
          // Verso emits `open` only on `<details>`: the module tree's, and the
          // trace ones it leaves expanded.
          element.removeAttribute(attribute.name);
        } else if (name === "src" && !safeUrl(attribute.value, name)) {
          element.removeAttribute(attribute.name);
        } else if (name === "href" && !safeUrl(attribute.value, name)) {
          element.removeAttribute(attribute.name);
        } else if (name === "style" && !allowedStyle.test(attribute.value)) {
          element.removeAttribute(attribute.name);
        } else if (name === "target") {
          element.removeAttribute(attribute.name);
        }
      }
    }
  }

  if (typeof marked !== "undefined" && typeof marked.parse === "function") {
    const parse = marked.parse.bind(marked);
    marked.parse = (...args) => {
      const template = document.createElement("template");
      template.innerHTML = parse(...args);
      sanitize(template.content);
      return template.innerHTML;
    };
  }
  window.palomarSanitize = sanitize;
  document.addEventListener("DOMContentLoaded", () => sanitize(document), {once: true});
})();
'''


VERSO_RUNTIME = r'''(() => {
  "use strict";
  const runtimeUrl = document.currentScript?.src;
  const sanitize = (root) => window.palomarSanitize && window.palomarSanitize(root);

  function isolateComparedDeclarations() {
    let declarations;
    try {
      declarations = JSON.parse(document.body.dataset.palomarDeclarations || "[]");
    } catch (_) {
      declarations = [];
    }
    if (!Array.isArray(declarations) || declarations.length === 0) return true;
    const source = document.querySelector("section.code-content");
    if (!source) return false;

    const selected = [];
    for (const name of declarations) {
      const marker = Array.from(source.querySelectorAll(".const[data-binding]"))
        .find((element) => element.dataset.binding === `const-${name}` && element.id);
      const block = marker?.closest("code.hl.lean.block");
      if (!block) return false;
      const docstring = block.previousElementSibling;
      if (docstring?.matches(".md-text:not(.mod-doc)")) selected.push(docstring);
      selected.push(block);
    }

    const main = document.createElement("main");
    main.id = "main-content";
    main.className = "palomar-declaration-surface";
    const content = document.createElement("section");
    content.className = "code-content";
    for (const element of selected) content.append(element);
    for (const anchor of content.querySelectorAll("a[href]")) {
      let fragment = "";
      try {
        fragment = new URL(anchor.href, location.href).hash.slice(1);
      } catch (_) {
        fragment = "";
      }
      if (fragment && !content.querySelector(`#${CSS.escape(fragment)}`)) {
        anchor.replaceWith(...anchor.childNodes);
      }
    }
    main.append(content);
    document.body.replaceChildren(main);
    document.title = "Compared Challenge declaration";
    return true;
  }

  function renderDocstrings(root) {
    if (typeof marked === "undefined" || typeof marked.parse !== "function") return;
    for (const source of root.querySelectorAll("code.docstring, pre.docstring")) {
      const template = document.createElement("template");
      template.innerHTML = marked.parse(source.innerText);
      sanitize(template.content);
      const rendered = document.createElement("div");
      rendered.className = "docstring";
      rendered.append(template.content.cloneNode(true));
      source.replaceWith(rendered);
    }
  }

  function installBindingHighlights() {
    let highlighted = [];
    for (const container of document.querySelectorAll(".hl.lean")) {
      container.addEventListener("mouseover", (event) => {
        const token = event.target.closest(".token[data-binding]");
        if (!token || !container.contains(token) || !token.dataset.binding) return;
        highlighted = Array.from(document.querySelectorAll(".hl.lean .token[data-binding]"))
          .filter((candidate) => candidate.dataset.binding === token.dataset.binding &&
            candidate.closest(".hl.lean")?.dataset.leanContext === container.dataset.leanContext);
        highlighted.forEach((candidate) => candidate.classList.add("binding-hl"));
      });
      container.addEventListener("mouseout", () => {
        highlighted.forEach((candidate) => candidate.classList.remove("binding-hl"));
        highlighted = [];
      });
    }
  }

  function popupContent(target, docs) {
    const content = document.createElement("span");
    content.className = "hl lean popup";
    if (target.classList.contains("tactic")) {
      const state = target.querySelector(":scope > .tactic-state");
      if (!state) return null;
      content.append(state.cloneNode(true));
      return content;
    }
    const inline = target.querySelector(".hover-info");
    if (inline) {
      content.append(inline.cloneNode(true));
      sanitize(content);
      return content;
    }
    const value = docs[String(target.dataset.versoHover || "")];
    if (typeof value !== "string") return null;
    const template = document.createElement("template");
    template.innerHTML = value;
    sanitize(template.content);
    content.append(template.content.cloneNode(true));
    renderDocstrings(content);
    return content;
  }

  const MARGIN = 8;
  const GAP = 6;

  /**
   * Put the popup beside the thing it describes, never on top of it.
   *
   * The old placement clamped the top to `innerHeight - 160`, which for any
   * token in the lower part of the view put the box over the token and so
   * under the pointer. The pointer then left the token, the popup hid, the
   * pointer was over the token again, and it flickered. Choosing the roomier
   * side removes the cause; `pointer-events: none` in the stylesheet removes
   * the possibility.
   */
  function place(popup, box) {
    popup.style.position = "fixed";
    popup.style.maxWidth = `${Math.max(320, innerWidth - 2 * MARGIN)}px`;
    popup.style.maxHeight = "none";
    popup.style.overflow = "visible";
    popup.style.zIndex = "2147483647";
    // Measured after the width is set and before a side is chosen: how tall it
    // wants to be depends on how wide it is allowed to be.
    popup.style.left = `${MARGIN}px`;
    popup.style.top = `${MARGIN}px`;
    const wanted = popup.getBoundingClientRect();

    const below = innerHeight - box.bottom - GAP - MARGIN;
    const above = box.top - GAP - MARGIN;
    const under = wanted.height <= below || below >= above;
    const height = Math.max(64, Math.min(wanted.height, under ? below : above));
    popup.style.maxHeight = `${height}px`;
    popup.style.overflow = "auto";
    popup.style.top = under
      ? `${box.bottom + GAP}px`
      : `${Math.max(MARGIN, box.top - GAP - height)}px`;
    popup.style.left =
      `${Math.max(MARGIN, Math.min(box.left, innerWidth - wanted.width - MARGIN))}px`;
  }

  function installHovers(docs) {
    const popup = document.createElement("div");
    popup.className = "palomar-hover hl lean";
    popup.hidden = true;
    document.body.append(popup);
    const selector = ".hl.lean .token[data-verso-hover], .hl.lean .has-info, .hl.lean .tactic";
    for (const target of document.querySelectorAll(selector)) {
      target.addEventListener("mouseenter", () => {
        const content = popupContent(target, docs);
        if (!content) return;
        popup.replaceChildren(content);
        popup.hidden = false;
        place(popup, target.getBoundingClientRect());
      });
      target.addEventListener("mouseleave", () => { popup.hidden = true; });
    }
  }

  function reportSurfaceHeight() {
    const surface = document.querySelector(".palomar-declaration-surface");
    if (!surface || parent === window) return;
    const send = () => {
      const height = Math.ceil(surface.getBoundingClientRect().height);
      if (Number.isSafeInteger(height) && height > 0) {
        parent.postMessage({type: "palomar-render-height", height}, "*");
      }
    };
    requestAnimationFrame(send);
    if (typeof ResizeObserver === "function") new ResizeObserver(send).observe(surface);
  }

  document.addEventListener("DOMContentLoaded", async () => {
    sanitize(document);
    if (!isolateComparedDeclarations()) {
      const error = document.createElement("p");
      error.className = "palomar-render-error";
      error.textContent = "The compared declaration is missing from this rendering.";
      document.body.replaceChildren(error);
      return;
    }
    renderDocstrings(document);
    installBindingHighlights();
    let docs = {};
    try {
      const root = runtimeUrl ? new URL(".", runtimeUrl) : new URL("../", location.href);
      const response = await fetch(new URL("-verso-docs.json", root), {credentials: "omit"});
      if (response.ok) docs = await response.json();
    } catch (_) {
      docs = {};
    }
    installHovers(docs);
    reportSurfaceHeight();
  }, {once: true});
})();
'''


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def extract_module_doc(text: str) -> str | None:
    """Return the first Lean module doc comment outside strings and other comments."""
    index = 0
    while index < len(text):
        if text.startswith("--", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/-", index):
            is_module_doc = text.startswith("/-!", index)
            content_start = index + 3
            depth = 1
            cursor = index + 2
            while cursor < len(text) and depth:
                if text.startswith("/-", cursor):
                    depth += 1
                    cursor += 2
                elif text.startswith("-/", cursor):
                    depth -= 1
                    if depth == 0:
                        if is_module_doc:
                            return text[content_start:cursor].strip()
                        cursor += 2
                        break
                    cursor += 2
                else:
                    cursor += 1
            if depth:
                raise VerificationError("unterminated Lean block comment")
            index = cursor
            continue
        if text[index] == '"':
            index += 1
            escaped = False
            while index < len(text):
                character = text[index]
                index += 1
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    break
            continue
        index += 1
    return None


def parsed_challenge_metadata(
    challenge: Path,
    solution: Path,
    comparator: Path,
    *,
    challenge_module: str,
    lean: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    for path in (challenge, solution, comparator):
        if path.is_symlink() or not path.is_file():
            raise VerificationError(f"render metadata input is missing or invalid: {path.name}")
    source = challenge.read_text(encoding="utf-8")
    config = load_comparator_config(comparator)
    if config["challenge_module"] != challenge_module:
        raise VerificationError("rendered Challenge module does not match its comparator config")
    declarations = [*config["theorem_names"], *config.get("definition_names", [])]
    if len(declarations) != len(set(declarations)):
        raise VerificationError("comparator declaration names must be unique")
    # The header is read by Lean rather than by a parser of our own, so the
    # rendered page lists what the compiler sees. This step already runs inside
    # the render sandbox, which permits the toolchain.
    return {
        "schema_version": 2,
        "imports": list(source_header(challenge, lean=lean, environment=environment).imports),
        "module_doc": extract_module_doc(source),
        "declarations": declarations,
        "solution_imports": list(
            source_header(solution, lean=lean, environment=environment).imports
        ),
    }


def source_header(lean_source: Path, *, lean: Path, environment: dict[str, str]) -> LeanHeader:
    """Read one Lean source header with the compiler's own parser."""
    proc = run(
        [str(lean), "--deps-json", str(lean_source)],
        cwd=lean_source.parent,
        env=environment,
        check=False,
    )
    if proc.returncode:
        raise VerificationError(f"Lean could not read the header of {lean_source.name}")
    return parse_lean_header(proc.stdout)


def load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise VerificationError(f"{path.name} must contain a JSON object")
    return value


def toolchain_verso_commit(toolchain: str) -> str:
    """The Verso release matching this toolchain, resolved to a commit.

    The revision is derived from the toolchain rather than looked up in a
    table that has to be edited for every release. Verso does not tag every
    Lean patch release, so resolve_release_commit falls back to the newest
    tag in the same release line. The resolved commit is what gets recorded,
    so a render always says exactly which Verso built it.
    """
    return resolve_release_commit("leanprover/verso", supported_toolchain(toolchain))


def normalized_repository(value: str) -> tuple[str, str]:
    candidate = value.strip().removesuffix(".git").strip("/")
    match = GITHUB_RE.fullmatch(f"https://github.com/{candidate}")
    if not match:
        raise VerificationError("repository must have the form owner/name")
    repository = f"{match.group('owner')}/{match.group('repo')}"
    return repository, f"https://github.com/{repository}"


def prepare(args: argparse.Namespace) -> int:
    work = Path(args.work_dir).resolve()
    output = Path(args.output).resolve()
    report = intake_report(now())
    try:
        repository, repository_url = normalized_repository(args.repository)
        commit = args.commit.strip().lower()
        challenge_sha256 = args.challenge_sha256.strip().lower()
        if not SHA_RE.fullmatch(commit):
            raise VerificationError("commit must be 40 lowercase hexadecimal characters")
        if not re.fullmatch(r"[0-9a-f]{64}", challenge_sha256):
            raise VerificationError("challenge_sha256 must be 64 lowercase hexadecimal characters")
        supplied_paths = AcceptedRenderPaths(
            project_path=args.project_path.strip(),
            challenge_path=args.challenge_path.strip(),
            solution_path=args.solution_path.strip(),
            comparator_config_path=args.comparator_config_path.strip(),
            lakefile_path=args.lakefile_path.strip(),
            lean_toolchain_path=args.lean_toolchain_path.strip(),
        )
        source = work / "source"
        clone_commit(repository_url, commit, source)
        if tree_size(source) > MAX_SOURCE_BYTES:
            raise VerificationError("checked-out source exceeds the 500 MiB cap")
        project_relative = (
            normalized_repository_path(supplied_paths.project_path, "project_path")
            if supplied_paths.project_path
            else None
        )
        project = (
            resolve_repository_path(source, project_relative, "project_path", kind="directory")
            if project_relative is not None
            else source.resolve()
        )
        challenge_relative = normalized_repository_path(
            supplied_paths.challenge_path, "challenge_path"
        )
        solution_relative = normalized_repository_path(
            supplied_paths.solution_path, "solution_path"
        )
        comparator_relative = normalized_repository_path(
            supplied_paths.comparator_config_path, "comparator_config_path"
        )
        lakefile_relative = normalized_repository_path(
            supplied_paths.lakefile_path, "lakefile_path"
        )
        toolchain_relative = normalized_repository_path(
            supplied_paths.lean_toolchain_path, "lean_toolchain_path"
        )
        challenge = resolve_repository_path(source, challenge_relative, "challenge_path", kind="file")
        solution = resolve_repository_path(source, solution_relative, "solution_path", kind="file")
        comparator = resolve_repository_path(
            source, comparator_relative, "comparator_config_path", kind="file"
        )
        lakefile = resolve_repository_path(source, lakefile_relative, "lakefile_path", kind="file")
        toolchain_file = resolve_repository_path(
            source, toolchain_relative, "lean_toolchain_path", kind="file"
        )
        if lakefile.parent != project or lakefile.name not in {"lakefile.toml", "lakefile.lean"}:
            raise VerificationError("accepted Lakefile is not the selected project's Lakefile")
        for path, field in (
            (challenge, "Challenge source"),
            (solution, "Solution source"),
            (comparator, "Comparator configuration"),
        ):
            try:
                path.relative_to(project)
            except ValueError as error:
                raise VerificationError(f"accepted {field} is outside the selected project") from error
        if toolchain_file.name != "lean-toolchain" or toolchain_file.parent not in {
            project,
            source,
        }:
            raise VerificationError("accepted lean-toolchain is outside its supported locations")
        manifest_file = project / "lake-manifest.json"
        if manifest_file.is_symlink() or (
            lakefile.name == "lakefile.lean" and not manifest_file.is_file()
        ):
            raise VerificationError("lakefile.lean projects require a committed lake-manifest.json")
        load_comparator_config(comparator)
        actual_challenge_sha256 = sha256(challenge)
        if actual_challenge_sha256 != challenge_sha256:
            raise VerificationError("Challenge source does not match the accepted mechanical report")
        toolchain = toolchain_file.read_text(encoding="utf-8").strip()
        verso_commit = toolchain_verso_commit(toolchain)
        accepted_paths = AcceptedRenderPaths(
            project_path=project_relative.as_posix() if project_relative else "",
            challenge_path=challenge_relative.as_posix(),
            solution_path=solution_relative.as_posix(),
            comparator_config_path=comparator_relative.as_posix(),
            lakefile_path=lakefile_relative.as_posix(),
            lean_toolchain_path=toolchain_relative.as_posix(),
        )
        report = PreparedRenderReport(
            source=RenderSource(
                repository=repository,
                repository_url=repository_url,
                commit=commit,
                challenge_sha256=challenge_sha256,
                paths=accepted_paths,
            ),
            lean_toolchain=toolchain,
            verso_commit=verso_commit,
            prepared_at=report["prepared_at"],
        ).as_dict()
        write_json(output, report)
        workflow_output = os.environ.get("GITHUB_OUTPUT")
        if workflow_output:
            with open(workflow_output, "a", encoding="utf-8") as handle:
                handle.write("ready=true\n")
                handle.write(f"lean_toolchain={toolchain}\n")
                handle.write(f"verso_commit={verso_commit}\n")
    except Exception as error:  # noqa: BLE001 - workflow failures become a bounded report
        report["errors"].append(str(error))
        write_json(output, report)
        workflow_output = os.environ.get("GITHUB_OUTPUT")
        if workflow_output:
            with open(workflow_output, "a", encoding="utf-8") as handle:
                handle.write("ready=false\nlean_toolchain=\nverso_commit=\n")
    return 0


def manifest_package_key(package: dict[str, Any]) -> tuple[str, str, str]:
    package_type = str(package.get("type") or "")
    if package_type == "git":
        repository = github_repository(str(package.get("url") or ""))
        revision = str(package.get("rev") or "")
        if repository is None or not SHA_RE.fullmatch(revision):
            raise VerificationError(f"renderer manifest has an invalid Git package: {package.get('name')!r}")
        return package_type, repository.lower(), revision
    if package_type == "path":
        directory = str(package.get("dir") or "")
        if not directory or Path(directory).is_absolute() or ".." in Path(directory).parts:
            raise VerificationError(f"renderer manifest has an invalid path package: {package.get('name')!r}")
        return package_type, directory, ""
    raise VerificationError(f"renderer manifest has an unsupported package type: {package_type!r}")


def merge_renderer_manifest(
    source_manifest: dict[str, Any],
    verso_manifest: dict[str, Any],
    verso_commit: str,
) -> dict[str, Any]:
    source_packages = source_manifest.get("packages")
    verso_packages = verso_manifest.get("packages")
    if not isinstance(source_packages, list) or not isinstance(verso_packages, list):
        raise VerificationError("Lake manifest packages must be arrays")
    merged: dict[str, dict[str, Any]] = {}

    def add(package: dict[str, Any], *, inherited: bool) -> None:
        if not isinstance(package, dict):
            raise VerificationError("Lake manifest package entries must be objects")
        name = package.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise VerificationError(f"invalid Lake package name: {name!r}")
        candidate = dict(package)
        candidate["inherited"] = inherited
        identity = manifest_package_key(candidate)
        previous = merged.get(name)
        if previous is not None and manifest_package_key(previous) != identity:
            raise VerificationError(f"Verso dependency conflicts with submitted package {name!r}")
        if previous is None:
            merged[name] = candidate

    for package in source_packages:
        add(package, inherited=bool(package.get("inherited")))
    for package in verso_packages:
        add(package, inherited=True)
    add(
        {
            "url": f"https://github.com/{VERSO_REPOSITORY}.git",
            "type": "git",
            "subDir": None,
            "scope": "",
            "rev": verso_commit,
            "name": "verso",
            "manifestFile": "lake-manifest.json",
            "inputRev": verso_commit,
            "inherited": False,
            "configFile": "lakefile.lean",
        },
        inherited=False,
    )
    return {
        "version": str(source_manifest.get("version") or verso_manifest.get("version") or "1.1.0"),
        "packagesDir": ".lake/packages",
        "packages": [merged[name] for name in sorted(merged)],
        "name": "PalomarChallengeRender",
        "lakeDir": ".lake",
        "fixedToolchain": False,
    }


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def trusted_lakefile(
    source_manifest: dict[str, Any],
    verso_commit: str,
    *,
    challenge_module: str,
    challenge_source_root: PurePosixPath,
) -> str:
    packages = source_manifest.get("packages")
    if not isinstance(packages, list):
        raise VerificationError("submitted Lake manifest packages must be an array")
    direct = [
        package
        for package in packages
        if not bool(package.get("inherited")) and package.get("name") != "verso"
    ]
    lines = ['name = "PalomarChallengeRender"', 'defaultTargets = ["Challenge"]', ""]
    seen: set[str] = set()
    for package in [*direct, {
        "name": "verso",
        "type": "git",
        "url": f"https://github.com/{VERSO_REPOSITORY}",
        "rev": verso_commit,
    }]:
        if not isinstance(package, dict):
            raise VerificationError("submitted Lake manifest package entries must be objects")
        name = str(package.get("name") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in seen:
            raise VerificationError(f"invalid or duplicate direct Lake package: {name!r}")
        seen.add(name)
        lines.extend(["[[require]]", f"name = {toml_string(name)}"])
        if package.get("type") == "git":
            repository = github_repository(str(package.get("url") or ""))
            revision = str(package.get("rev") or "")
            if repository is None or not SHA_RE.fullmatch(revision):
                raise VerificationError(f"invalid direct Git package: {name!r}")
            lines.extend(
                [
                    f"git = {toml_string(f'https://github.com/{repository}.git')}",
                    f"rev = {toml_string(revision)}",
                    "",
                ]
            )
        elif package.get("type") == "path":
            directory = str(package.get("dir") or "")
            if not directory or Path(directory).is_absolute() or "\\" in directory:
                raise VerificationError(f"invalid direct path package: {name!r}")
            lines.extend([f"path = {toml_string(directory)}", ""])
        else:
            raise VerificationError(f"unsupported direct package type for {name!r}")
    module_source_suffix(challenge_module)
    if (
        challenge_source_root.is_absolute()
        or ".." in challenge_source_root.parts
        or "\\" in challenge_source_root.as_posix()
    ):
        raise VerificationError("configured Challenge source root is unsafe")
    lines.extend(
        [
            "[[lean_lib]]",
            'name = "Challenge"',
            f"srcDir = {toml_string(challenge_source_root.as_posix())}",
            f"roots = [{toml_string(challenge_module)}]",
            "",
        ]
    )
    return "\n".join(lines)


def replace_workspace_file(path: Path, content: bytes) -> None:
    """Replace one hostile workspace path without following its final symlink."""
    if path.is_symlink() or path.exists():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    path.write_bytes(content)


def configured_module_source_root(
    project: Path,
    source: Path,
    module: str,
    field: str,
) -> PurePosixPath:
    """Bind one accepted source path to its configured dotted module identity."""

    suffix = module_source_suffix(module)
    try:
        relative = source.relative_to(project)
    except ValueError as error:
        raise VerificationError(f"accepted {field} is outside the selected project") from error
    if len(relative.parts) < len(suffix.parts) or relative.parts[-len(suffix.parts) :] != suffix.parts:
        raise VerificationError(
            f"accepted {field} path does not match configured module {module!r}"
        )
    root_parts = relative.parts[: -len(suffix.parts)]
    return PurePosixPath(*root_parts) if root_parts else PurePosixPath(".")


def module_html_entrypoint(module: str) -> PurePosixPath:
    """Return Verso's output path for the original configured Lean module."""

    return module_source_suffix(module).with_suffix("") / "index.html"


@dataclass(frozen=True, slots=True)
class RenderWorkspace:
    project: Path
    challenge: Path
    solution: Path
    comparator: Path
    challenge_module: str


def prepare_workspace(
    source: Path,
    workspace: Path,
    verso_commit: str,
    work: Path,
    accepted_paths: AcceptedRenderPaths,
) -> RenderWorkspace:
    if workspace.exists():
        raise VerificationError("render workspace already exists")
    project_relative = (
        normalized_repository_path(accepted_paths.project_path, "render project_path")
        if accepted_paths.project_path
        else None
    )
    source_project = (
        resolve_repository_path(
            source,
            project_relative,
            "render project_path",
            kind="directory",
        )
        if project_relative
        else source.resolve()
    )
    ensure_lake_manifest(source_project, source)
    source_manifest = load_json_object(source_project / "lake-manifest.json")
    challenge_relative = normalized_repository_path(
        accepted_paths.challenge_path, "render challenge_path"
    )
    solution_relative = normalized_repository_path(
        accepted_paths.solution_path, "render solution_path"
    )
    comparator_relative = normalized_repository_path(
        accepted_paths.comparator_config_path,
        "render comparator_config_path",
    )
    toolchain_relative = normalized_repository_path(
        accepted_paths.lean_toolchain_path,
        "render lean_toolchain_path",
    )
    accepted_challenge_path = resolve_repository_path(
        source, challenge_relative, "render challenge_path", kind="file"
    )
    accepted_solution_path = resolve_repository_path(
        source, solution_relative, "render solution_path", kind="file"
    )
    accepted_comparator_path = resolve_repository_path(
        source, comparator_relative, "render comparator_config_path", kind="file"
    )
    accepted_challenge = accepted_challenge_path.read_bytes()
    accepted_solution = accepted_solution_path.read_bytes()
    accepted_comparator = accepted_comparator_path.read_bytes()
    comparator_config = load_comparator_config(accepted_comparator_path)
    challenge_module = comparator_config["challenge_module"]
    configured_module_source_root(
        source_project,
        accepted_solution_path,
        comparator_config["solution_module"],
        "Solution source",
    )
    challenge_source_root = configured_module_source_root(
        source_project,
        accepted_challenge_path,
        challenge_module,
        "Challenge source",
    )
    source_toolchain_path = resolve_repository_path(
        source, toolchain_relative, "render lean_toolchain_path", kind="file"
    )
    verso_probe = work / "verso-manifest"
    clone_commit(f"https://github.com/{VERSO_REPOSITORY}", verso_commit, verso_probe)
    try:
        verso_toolchain = (verso_probe / "lean-toolchain").read_text(encoding="utf-8").strip()
        source_toolchain = source_toolchain_path.read_text(encoding="utf-8").strip()
        if verso_toolchain != source_toolchain:
            raise VerificationError("pinned Verso commit does not match the submission Lean toolchain")
        verso_manifest = load_json_object(verso_probe / "lake-manifest.json")
        merged_manifest = merge_renderer_manifest(source_manifest, verso_manifest, verso_commit)
    finally:
        shutil.rmtree(verso_probe, ignore_errors=True)
    # Preserve hostile symlinks rather than dereferencing them in the trusted
    # process. Landrun then mediates any attempt to follow them during a build.
    shutil.copytree(
        source,
        workspace,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", ".lake"),
    )
    workspace_project = (
        resolve_repository_path(
            workspace,
            project_relative,
            "render workspace project_path",
            kind="directory",
        )
        if project_relative
        else workspace.resolve()
    )
    for lakefile_name in ("lakefile.lean", "lakefile.toml"):
        submitted_lakefile = workspace_project / lakefile_name
        if submitted_lakefile.exists() or submitted_lakefile.is_symlink():
            if submitted_lakefile.is_dir() and not submitted_lakefile.is_symlink():
                shutil.rmtree(submitted_lakefile)
            else:
                submitted_lakefile.unlink()
    replace_workspace_file(
        workspace_project / "lakefile.toml",
        trusted_lakefile(
            source_manifest,
            verso_commit,
            challenge_module=challenge_module,
            challenge_source_root=challenge_source_root,
        ).encode("utf-8"),
    )
    write_json(workspace_project / "lake-manifest.json", merged_manifest)
    workspace_challenge = resolve_repository_path(
        workspace, challenge_relative, "render workspace challenge_path", kind="file"
    )
    workspace_solution = resolve_repository_path(
        workspace, solution_relative, "render workspace solution_path", kind="file"
    )
    workspace_comparator = resolve_repository_path(
        workspace,
        comparator_relative,
        "render workspace comparator_config_path",
        kind="file",
    )
    if (
        workspace_challenge.read_bytes() != accepted_challenge
        or workspace_solution.read_bytes() != accepted_solution
        or workspace_comparator.read_bytes() != accepted_comparator
    ):
        raise VerificationError("render workspace changed an accepted source file")
    return RenderWorkspace(
        project=workspace_project.resolve(),
        challenge=workspace_challenge,
        solution=workspace_solution,
        comparator=workspace_comparator,
        challenge_module=challenge_module,
    )


def tree_bytes(root: Path, *, stop_after: int | None = None) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        total += path.stat().st_size
        if stop_after is not None and total > stop_after:
            return total
    return total


class _StaticHTMLSanitizer(HTMLParser):
    """Serialize generated HTML while removing executable/navigation attributes.

    Attribute rewriting must be parser-based: whole-document regular
    expressions also see words inside quoted attribute values (including valid
    Lean identifiers) and can corrupt them.
    """

    _STRIPPED_ATTRIBUTES = {
        "srcdoc",
        "action",
        "formaction",
        "ping",
        "target",
        # Presentation and visibility without a stylesheet: the legacy colour
        # attributes, the sizing pair, the top-layer controls, and the one that
        # hides honest content so that dishonest content can stand in for it.
        # Verso emits none of these here. Its only `width` and `height` are in
        # `<meta name="viewport">`, and meta elements are dropped whole.
        "alink",
        "background",
        "bgcolor",
        "height",
        "hidden",
        "link",
        "popover",
        "popovertarget",
        "popovertargetaction",
        "text",
        "vlink",
        "width",
    }

    # Verso emits `open` only on `<details>`, both in the module tree and on
    # the `<details class="trace">` it leaves expanded, so the attribute is
    # kept there and stripped everywhere else.
    _OPEN_ELEMENT = "details"

    # SVG and MathML give presentation and geometry their own attributes, so a
    # single `<rect>` covers the page as effectively as a positioned `style`
    # does, and a `dialog` draws itself over the document without needing any.
    # The literate-code genre Verso builds here emits none of the three (its
    # only SVG is the manual genre's diagram files, `latexMath` renders to
    # nothing, and no genre emits a dialog), so their subtrees are dropped
    # whole.
    _DROPPED_ELEMENTS = {"dialog", "math", "svg"}

    # `style-src` has to keep `'unsafe-inline'`, so any inline declaration a
    # submitter reaches is a free hand over the rendered page: fixed position,
    # colour and size are enough to cover the surrounding text with a
    # convincing imitation of it. Verso's literate-code output needs exactly
    # one declaration, since `code.css` indents prose with
    # `calc(var(--indent, 0) * 1ch)`, so the attribute is filtered down to that
    # custom property rather than dropped outright. Its value is the source
    # column the documentation starts at, which two digits already covers with
    # room to spare, and an unbounded one would be a shove off the page. The
    # whitespace is spelled out because `\s` does not mean the same thing in
    # the browser sanitizer as it does here.
    _ALLOWED_STYLE = re.compile(
        r"[ \t\r\n\f]*--indent:[ \t\r\n\f]*[0-9]{1,2}[ \t\r\n\f]*;?[ \t\r\n\f]*"
    )

    def __init__(self, declarations: list[str]) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.bases: list[str | None] = []
        self.declarations = set(declarations)
        self.found_declarations: set[str] = set()
        self.script_depth = 0
        # The dropped elements this position is inside, innermost last. A stack
        # rather than a depth: `<svg></math>` must not be read as closing
        # anything, or serialization resumes inside a subtree that is still
        # open and the drop is only apparent.
        self.dropped: list[str] = []
        self.mismatched_drop = False

    @property
    def _suppressed(self) -> bool:
        return bool(self.script_depth or self.dropped)

    @staticmethod
    def _rewrite_url(attribute: str, raw: str) -> str:
        value = raw.strip()
        if not value or value.startswith("#"):
            return value
        if value.startswith("data:"):
            safe_data = attribute == "src" and re.match(
                r"^data:image/(?:gif|jpeg|png|webp);", value, flags=re.IGNORECASE
            )
            return value if safe_data else "#"
        if (
            value.startswith(("/", "../"))
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value)
            or any(part == ".." for part in value.split("/"))
        ):
            return "#"
        return f"../{value.removeprefix('./')}"

    def _start_tag(
        self, tag: str, attrs: list[tuple[str, str | None]], *, closed: bool
    ) -> None:
        tag = tag.lower()
        if self.dropped:
            if tag in self._DROPPED_ELEMENTS and not closed:
                self.dropped.append(tag)
            return
        if self.script_depth:
            return
        if tag in self._DROPPED_ELEMENTS:
            if not closed:
                self.dropped.append(tag)
            return
        if tag == "script":
            if not closed:
                self.script_depth = 1
            return
        if tag == "meta":
            return
        if tag == "base":
            self.bases.append(dict(attrs).get("href"))
            return

        attr_names = {name.lower() for name, _value in attrs}
        binding = next(
            (value for name, value in attrs if name.lower() == "data-binding"), None
        )
        if binding and binding.startswith("const-") and "id" in attr_names:
            declaration = binding.removeprefix("const-")
            if declaration in self.declarations:
                self.found_declarations.add(declaration)

        rendered: list[str] = []
        for name, value in attrs:
            name = name.lower()
            if name.startswith("on") or name in self._STRIPPED_ATTRIBUTES:
                continue
            if name == "open" and tag != self._OPEN_ELEMENT:
                continue
            if name == "style" and (
                value is None or not self._ALLOWED_STYLE.fullmatch(value)
            ):
                continue
            if value is None:
                rendered.append(name)
                continue
            if name in {"href", "src", "xlink:href"}:
                value = self._rewrite_url(name, value)
            rendered.append(f'{name}="{html.escape(value, quote=True)}"')
        suffix = " /" if closed else ""
        attributes = f" {' '.join(rendered)}" if rendered else ""
        self.parts.append(f"<{tag}{attributes}{suffix}>")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start_tag(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start_tag(tag, attrs, closed=True)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._DROPPED_ELEMENTS:
            if self.dropped and self.dropped[-1] == tag:
                self.dropped.pop()
            else:
                # Either nothing opened this, or it closes across a subtree
                # that is still open. Refuse the document instead of guessing
                # what it ends, and stay inside the drop until it is refused.
                self.mismatched_drop = True
            return
        if self.dropped:
            return
        if self.script_depth:
            if tag == "script":
                self.script_depth = 0
            return
        if tag not in {"base", "meta", "script"}:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._suppressed:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._suppressed:
            self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if not self._suppressed:
            self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        if not self._suppressed:
            self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        if not self._suppressed:
            self.parts.append(f"<?{data}>")


def static_html_sanitize(
    text: str,
    sanitizer_src: str,
    runtime_src: str,
    declarations: list[str] | None = None,
    *,
    expected_base: str = "../",
) -> str:
    sanitizer = _StaticHTMLSanitizer(declarations or [])
    sanitizer.feed(text)
    if sanitizer.rawdata:
        raise VerificationError("generated HTML ends with incomplete markup")
    sanitizer.close()
    if sanitizer.mismatched_drop:
        raise VerificationError("generated HTML closes a subtree it did not open")
    if sanitizer.dropped:
        raise VerificationError("generated HTML leaves a dropped subtree unclosed")
    if sanitizer.bases != [expected_base]:
        raise VerificationError("generated HTML has an unexpected base URL")
    text = "".join(sanitizer.parts)
    declaration_json = json.dumps(declarations or [], ensure_ascii=False, separators=(",", ":"))
    for declaration in declarations or []:
        if declaration not in sanitizer.found_declarations:
            raise VerificationError(
                f"Verso output does not contain compared declaration: {declaration}"
            )

    # Inject trusted declaration metadata only after every whole-document regex
    # pass. Otherwise an escaped Lean identifier containing text such as
    # `` href=x`` can be mistaken for markup by a later URL rewrite.
    def annotate_body(match: re.Match[str]) -> str:
        attributes = match.group(1) or ""
        if re.search(r"\bdata-palomar-declarations\b", attributes, flags=re.IGNORECASE):
            raise VerificationError("generated HTML contains reserved Palomar metadata")
        value = html.escape(declaration_json, quote=True)
        return f'<body{attributes} data-palomar-declarations="{value}">'

    # Count before substituting: `count=1` stops at the first match, so a
    # replacement count of one only says there was at least one body. A second
    # one matters, because the browser folds its attributes into the first.
    body_pattern = r"<body(\s[^>]*)?>"
    if len(re.findall(body_pattern, text, flags=re.IGNORECASE)) != 1:
        raise VerificationError("generated HTML does not contain exactly one body element")
    text = re.sub(body_pattern, annotate_body, text, count=1, flags=re.IGNORECASE)

    policy = "; ".join(
        [
            "default-src 'none'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "connect-src 'self'",
            "font-src 'self'",
            "object-src 'none'",
            "frame-src 'none'",
            "child-src 'none'",
            "worker-src 'none'",
            "manifest-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            "navigate-to 'self'",
        ]
    )
    meta = (
        f'<meta http-equiv="Content-Security-Policy" content="{policy}">\n    '
        '<meta charset="utf-8">\n    '
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n    '
    )
    scripts = (
        f'<script defer src="{sanitizer_src}"></script>\n'
        f'    <script defer src="{runtime_src}"></script>'
    )
    head_pattern = r"(<head\b[^>]*>)"
    if len(re.findall(head_pattern, text, flags=re.IGNORECASE)) != 1:
        raise VerificationError("generated HTML does not contain exactly one head element")
    text = re.sub(
        head_pattern,
        rf"\1\n    {meta}\n    {scripts}",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    surface_style = """<style id="palomar-declaration-style">
      /* The registry page that frames this document follows the browser's
         light and dark preference, so this document has to follow with it.
         Nothing is passed across the origin boundary to make that happen: the
         frame is laid out by the same browser, so the query below answers the
         same way the page's does.

         Verso's stylesheets name light colours directly, and a published
         bundle is immutable, so the answers are frozen here as a palette
         rather than read from the page. The dark values are the page's own, so
         the frame sits on the paper it is drawn on. Every rule below this block
         spends a token, never a literal.

         The light values are Verso's own wherever a reader meets them: the
         code surface, the docstring frames, the signature popup and the tactic
         marks all render exactly as before. Where Verso reached for a colour
         off its own scale — `black` popup borders, a `red` error underline,
         `#e5e5e5` message panels, Bootstrap greys in the page shell — the
         token takes over and light shifts a shade. Those are the diagnostic
         panels and the shell a reader sees only with scripts disabled.
         Carrying a second light value for each of them would have doubled a
         palette that every future bundle is stuck with, to preserve choices
         that were incidental. */
      :root {
        color-scheme: light dark;
        --palomar-paper: #ffffff;
        --palomar-ink: #24292e;
        --palomar-line: #dddddd;
        --palomar-edge: #888888;
        --palomar-raise: #eeeeee;
        --palomar-panel: #ffffff;
        --palomar-rule: #e1e4e8;
        --palomar-link: #0066cc;
        --palomar-alarm: #b42318;
        --palomar-alarm-paper: #ffb3b3;
        --palomar-info: #4777ff;
        --palomar-info-paper: #4777ff;
        --palomar-mark: #bbbbbb;
        --palomar-mark-on: #999999;
        --palomar-shadow: rgb(0 0 0 / 22%);
      }
      @media (prefers-color-scheme: dark) {
        :root {
          --palomar-paper: #101216;
          --palomar-ink: #e8eaee;
          --palomar-line: #686f7c;
          --palomar-edge: #8e96a3;
          --palomar-raise: #242933;
          --palomar-panel: #1c2027;
          --palomar-rule: #3a404b;
          --palomar-link: #8ab4f8;
          --palomar-alarm: #ff9e91;
          --palomar-alarm-paper: #3a1c1a;
          --palomar-info: #8ab4f8;
          --palomar-info-paper: #1c3a6b;
          --palomar-mark: #686f7c;
          --palomar-mark-on: #8e96a3;
          --palomar-shadow: rgb(0 0 0 / 55%);
        }
      }
      html { height: auto !important; min-height: 100%; overflow: auto !important;
        overscroll-behavior: contain; }
      body { height: auto !important; min-height: 100%; margin: 0;
        overflow: visible !important; }
      html, body { background: var(--palomar-paper); color: var(--palomar-ink); }
      .palomar-declaration-surface { box-sizing: border-box; padding: 1rem; }
      .palomar-declaration-surface .code-content { margin: 0; max-width: none; padding: 0; }
      .palomar-declaration-surface .md-text::before,
      .palomar-declaration-surface .md-text::after { width: max-content; white-space: nowrap; }
      /* code.css paints the code surface and its docstring frames outright,
         and this stylesheet is the last one in the head, so restating them at
         equal specificity is enough to take them over. */
      .code-content { background: var(--palomar-paper); color: var(--palomar-ink); }
      .code-content .verso-text,
      .code-content .md-text { border-color: var(--palomar-line); }
      /* Each of these repeats a Verso selector exactly rather than writing a
         shorter one that happens to reach the same elements. `:has()` and
         `:not()` take the specificity of their argument, so a hand-shortened
         selector loses to the original and the rule silently does nothing;
         repeating it makes source order the only thing that decides, and this
         stylesheet is last. */
      @media (hover: hover) {
        .hl.lean .token.binding-hl,
        .hl.lean .literal:hover,
        .hl.lean .token.typed:hover { background-color: var(--palomar-raise); }
        .hl.lean .has-info.error:hover { background-color: var(--palomar-alarm-paper); }
        .hl.lean .has-info.information:hover { background-color: var(--palomar-info-paper); }
        .hl.lean .tactic:has(> .tactic-toggle:not(:checked))
          > label:hover:not(:has(.tactic > label:hover)) {
          background-color: var(--palomar-raise); }
      }
      .hl.lean .token .hover-info,
      .hl.lean .has-info .hover-info { background-color: var(--palomar-panel);
        border-color: var(--palomar-edge); }
      .hl.lean .hover-info code { color: inherit; }
      .hl.lean .hover-info .sep { border-top-color: var(--palomar-rule); }
      .hl.lean .hover-info.messages > code.error { background-color: var(--palomar-panel);
        border-left-color: var(--palomar-alarm); }
      .hl.lean .hover-info.messages > code.information { background-color: var(--palomar-panel);
        border-left-color: var(--palomar-info); }
      .hl.lean .has-info.error :not(.tactic-state):not(.tactic-state *) {
        text-decoration-color: var(--palomar-alarm); }
      /* Verso's own fallback here is the keyword `blue`, which on dark paper is
         an underline nobody can see. */
      .hl.lean .has-info.information :not(.tactic-state):not(.tactic-state *) {
        text-decoration-color: var(--palomar-info); }
      .hl.lean .tactic-state { background-color: var(--palomar-panel);
        border-color: var(--palomar-edge); }
      .hl.lean.popup .tactic-state { background-color: var(--palomar-panel); }
      /* The pill that says a tactic block can be opened, and the triangle that
         says a case can. Verso draws the pill faintly on purpose, so the two
         marks keep their own tokens rather than borrowing the border ones; the
         triangle it draws in `black`, which is text, so it spends the ink. */
      .hl.lean .tactic > label::after { border-color: var(--palomar-mark); }
      .hl.lean .tactic > label:has(+ .tactic-toggle:checked)::after {
        border-color: var(--palomar-mark-on); background-color: var(--palomar-mark-on); }
      .hl.lean .case-label:has(input[type="checkbox"])::before {
        background-color: var(--palomar-ink); }
      .palomar-hover {
        padding: .65rem !important; border: 1px solid var(--palomar-edge) !important;
        border-radius: .35rem; background: var(--palomar-panel) !important;
        color: var(--palomar-ink) !important;
        box-shadow: 0 .25rem 1rem var(--palomar-shadow);
        /* The pointer must never land on the popup. If it does, it has left
           the token, so the popup hides, so the pointer is over the token
           again, so it shows: a flicker that sustains itself. */
        pointer-events: none;
        /* Lean signatures are long and were being cut off inside a 400px box.
           Wrap them instead, and only where they can be broken. */
        white-space: pre-wrap;
        overflow-wrap: break-word;
      }
      .palomar-hover .hl.lean, .palomar-hover .popup,
      .palomar-hover .hover-info { white-space: pre-wrap; }
      .palomar-hover .popup,
      .palomar-hover .hover-info,
      .palomar-hover .docstring { background: var(--palomar-panel) !important;
        color: var(--palomar-ink) !important; }
      /* Verso separates a signature from its docstring with an empty span and
         leaves the spacing to a stylesheet we do not ship, so the two ran
         together: "... : Type (max u_7 u_8)The standard real/complex Euclidean
         space ...". The docstring is also still an inline <code>, because the
         Markdown renderer that would have made it a block is not loaded here. */
      .palomar-hover .sep { display: block; height: 0; }
      .palomar-hover .docstring {
        display: block;
        margin-top: .55rem;
        padding-top: .55rem;
        border-top: 1px solid var(--palomar-rule);
      }
      .palomar-render-error { margin: 1rem; font-family: sans-serif; }
      /* Verso's page shell, which a reader sees only where this document's
         scripts do not run: the runtime replaces the whole body on both its
         success and its failure path. It is answered broadly rather than rule
         by rule, because normalising a shell nobody reaches is not worth
         freezing a dozen more declarations into every published bundle. */
      .sidebar { background: var(--palomar-raise); }
      .title-bar, .hamburger { background: var(--palomar-paper); }
      .sidebar, .title-bar, .hamburger { border-color: var(--palomar-line); }
      .hamburger span { background: var(--palomar-ink); }
      .module-tree summary a, .module-tree .leaf a,
      .breadcrumbs a { color: var(--palomar-link); }
      .module-tree summary:hover,
      .breadcrumbs a:hover { background-color: var(--palomar-raise); }
      .module-tree summary::before { color: var(--palomar-ink); }
      .module-tree summary.current,
      .module-tree .current { background-color: var(--palomar-link);
        color: var(--palomar-paper); }
      .module-tree summary.current a,
      .module-tree .current a { color: var(--palomar-paper); }
      .breadcrumbs .current,
      .breadcrumbs li:not(:last-child)::after { color: var(--palomar-ink); }
    </style>"""
    text, closed_heads = re.subn(
        r"(</head\s*>)", rf"    {surface_style}\n  \1", text, count=1, flags=re.IGNORECASE
    )
    if closed_heads != 1:
        raise VerificationError("generated HTML does not close its head element")
    return text


def validate_output_file(path: Path, root: Path) -> tuple[str, int, str]:
    if path.is_symlink():
        raise VerificationError(f"render output contains a symbolic link: {path}")
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise VerificationError(f"render output contains a non-regular file: {path}")
    relative = path.relative_to(root).as_posix()
    if not relative or any(part in {"", ".", ".."} for part in Path(relative).parts):
        raise VerificationError(f"render output has an invalid path: {relative!r}")
    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise VerificationError(f"render output has an unexpected file type: {relative}")
    size = path.stat().st_size
    if size > MAX_RENDER_FILE_BYTES:
        raise VerificationError(f"render output file exceeds the per-file cap: {relative}")
    return relative, size, sha256(path)


def bounded_output_paths(bundle: Path) -> list[Path]:
    paths: list[Path] = []
    for path in bundle.rglob("*"):
        paths.append(path)
        if len(paths) > MAX_RENDER_NODES:
            raise VerificationError("render output exceeds the filesystem-node cap")
    return sorted(paths)


def artifact_manifest(bundle: Path) -> tuple[list[dict[str, Any]], str]:
    files: list[dict[str, Any]] = []
    total = 0
    for path in bounded_output_paths(bundle):
        if path.is_dir() and not path.is_symlink():
            continue
        if path.relative_to(bundle).as_posix() == "artifact-manifest.json":
            continue
        relative, size, digest = validate_output_file(path, bundle)
        files.append({"path": relative, "bytes": size, "sha256": digest})
        total += size
        if len(files) > MAX_RENDER_FILES:
            raise VerificationError("render output exceeds the file-count cap")
        if total > MAX_RENDER_BYTES:
            raise VerificationError("render output exceeds the total-size cap")
    if not files:
        raise VerificationError("render output is empty")
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return files, sha256_bytes(canonical)


def sanitize_bundle(
    input_dir: Path,
    output_dir: Path,
    *,
    challenge_module: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    if not input_dir.is_dir() or input_dir.is_symlink():
        raise VerificationError("Verso output directory is missing or invalid")
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise VerificationError("sanitized output directory is missing or invalid")
    if any(output_dir.iterdir()):
        raise VerificationError("sanitized output directory is not empty")
    source_files = []
    total = 0
    for path in bounded_output_paths(input_dir):
        if path.is_dir() and not path.is_symlink():
            continue
        relative, size, _ = validate_output_file(path, input_dir)
        source_files.append((path, relative))
        total += size
        if len(source_files) > MAX_RENDER_FILES or total > MAX_RENDER_BYTES:
            raise VerificationError("raw Verso output exceeds the artifact limits")
    metadata = metadata or {
        "schema_version": 1,
        "imports": [],
        "module_doc": None,
        "declarations": [],
    }
    declarations = metadata.get("declarations")
    if not isinstance(declarations, list) or not all(isinstance(item, str) for item in declarations):
        raise VerificationError("render metadata declarations must be an array of strings")
    source_entrypoint = module_html_entrypoint(challenge_module)
    source_base = "../" * (len(source_entrypoint.parts) - 1)
    found_entrypoint = False
    (output_dir / "palomar-sanitize.js").write_text(RUNTIME_SANITIZER, encoding="utf-8")
    (output_dir / "palomar-verso.js").write_text(VERSO_RUNTIME, encoding="utf-8")
    write_json(output_dir / "challenge-metadata.json", metadata)
    for source, relative in source_files:
        if source.suffix.lower() == ".js":
            continue
        relative_path = PurePosixPath(relative)
        is_entrypoint = relative_path == source_entrypoint
        if is_entrypoint:
            found_entrypoint = True
        destination_relative = "Challenge/index.html" if is_entrypoint else relative
        destination = output_dir / destination_relative
        if destination.exists():
            raise VerificationError(
                f"render output paths collide after entrypoint mapping: {destination_relative}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() == ".html":
            sanitizer_src = os.path.relpath(
                output_dir / "palomar-sanitize.js", destination.parent
            ).replace(os.sep, "/")
            runtime_src = os.path.relpath(
                output_dir / "palomar-verso.js", destination.parent
            ).replace(os.sep, "/")
            destination.write_text(
                static_html_sanitize(
                    source.read_text(encoding="utf-8"),
                    sanitizer_src,
                    runtime_src,
                    declarations if is_entrypoint else [],
                    expected_base=(
                        source_base
                        if is_entrypoint
                        else "../"
                    ),
                ),
                encoding="utf-8",
            )
        else:
            shutil.copyfile(source, destination)
    if not found_entrypoint:
        raise VerificationError(
            f"Verso output does not contain configured module {challenge_module!r}"
        )
    entrypoint = output_dir / "Challenge" / "index.html"
    if not entrypoint.is_file():
        raise VerificationError("sanitized render entrypoint is missing")
    files, tree_hash = artifact_manifest(output_dir)
    write_json(
        output_dir / "artifact-manifest.json",
        {"schema_version": 1, "artifact_tree_sha256": tree_hash, "files": files},
    )
    return tree_hash


def sanitize_command(args: argparse.Namespace) -> int:
    metadata = parsed_challenge_metadata(
        Path(args.challenge).resolve(),
        Path(args.solution).resolve(),
        Path(args.comparator).resolve(),
        challenge_module=args.challenge_module,
        lean=Path(args.lean).resolve(strict=True),
        environment=os.environ.copy(),
    )
    sanitize_bundle(
        Path(args.input_dir).resolve(),
        Path(args.output_dir).resolve(),
        challenge_module=args.challenge_module,
        metadata=metadata,
    )
    return 0


def executable_paths(lean_prefix: Path, extra: list[Path]) -> list[Path]:
    result = [lean_prefix, Path(sys.executable).resolve().parent.parent, *extra]
    for path in extra:
        resolved = path.resolve(strict=True)
        if resolved.is_file():
            # Nix packages keep helper executables (for example git-remote-https)
            # beside the requested binary under the same immutable store root.
            result.append(resolved.parent.parent)
            linkage = run(["ldd", str(resolved)], check=False)
            for match in re.finditer(r"(?:=>\s+)?(/[^\s()]+)", linkage.stdout):
                library = Path(match.group(1)).resolve(strict=True)
                if library.parts[:3] == ("/", "nix", "store") and len(library.parts) >= 4:
                    result.append(Path("/nix/store") / library.parts[3])
                else:
                    result.append(library.parent)
    for system_path in (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")):
        if system_path.exists():
            result.append(system_path.resolve())
    nix_store = Path("/nix/store")
    if nix_store.is_dir():
        # Nix compiler wrappers can reference sibling immutable store paths
        # that are not discoverable through ldd (for example ld-wrapper.sh).
        # The store is globally readable and contains no per-user credentials.
        result.append(nix_store)
    return sorted(set(result))


def required_executable(name: str, environment: dict[str, str]) -> Path:
    value = shutil.which(name, path=environment["PATH"])
    if not value:
        raise VerificationError(f"trusted executable is unavailable: {name}")
    # Preserve the invoked basename.  On NixOS, utilities such as ``touch``
    # and ``printenv`` are symlinks to a multicall ``coreutils`` binary whose
    # behavior is selected through argv[0].  ``tool_snapshot`` still resolves
    # and hashes the final target before and after confined execution.
    executable = Path(value).absolute()
    executable.resolve(strict=True)
    return executable


def discover_mathlib_cache_hashes(
    workspace: Path,
    *,
    environment: dict[str, str],
    lake: Path,
    landrun: Path,
    writable_directories: list[Path],
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> tuple[set[str], Path]:
    """Compute cache keys without giving Lake, Lean, or cache code network access."""
    cache_dir = workspace / ".lake" / "config" / "mathlib-cache"
    empty_cache = workspace / ".lake" / "config" / "empty-mathlib-cache"
    for directory in (cache_dir, empty_cache):
        if directory.is_symlink():
            directory.unlink()
        elif directory.exists():
            shutil.rmtree(directory)
    cache_dir.mkdir()
    (empty_cache / "f").mkdir(parents=True)
    cache_env = environment.copy()
    cache_env["MATHLIB_CACHE_DIR"] = str(cache_dir.resolve())
    cache_env["MATHLIB_CACHE_GET_URL"] = f"file://{empty_cache.resolve()}"
    proc = sandboxed_run(
        [str(lake), "exe", "cache", "get-"],
        cwd=workspace,
        environment=cache_env,
        landrun=landrun,
        writable_directories=writable_directories,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
        timeout=1800,
        check=False,
        resource_properties=RESOURCE_PROPERTIES,
    )
    transcript = f"{proc.stdout}\n{proc.stderr}"
    attempted = re.search(r"Attempting to download ([0-9]+) file", transcript)
    hashes = set(re.findall(r"\b([0-9a-f]{16})\.ltar(?:\.part)?\b", transcript))
    expected = int(attempted.group(1)) if attempted else 0
    if expected == 0 or expected != len(hashes):
        detail = transcript.strip().replace("\n", " ")
        if len(detail) > 1_000:
            detail = f"...{detail[-997:]}"
        diagnostic = f"; cache command exited {proc.returncode}"
        if detail:
            diagnostic += f": {detail}"
        raise VerificationError(
            f"Mathlib cache discovery was incomplete ({len(hashes)} of {expected} keys){diagnostic}"
        )
    if expected > MAX_CACHE_ARCHIVES:
        raise VerificationError("Mathlib cache request exceeds the archive-count cap")
    return hashes, cache_dir


def download_mathlib_cache(
    hashes: set[str],
    cache_dir: Path,
    *,
    trusted_work: Path,
    environment: dict[str, str],
    curl: Path,
    env_tool: Path,
) -> tuple[int, int]:
    """Fetch any available fixed-host cache bytes without requiring a cache hit.

    Cache archives are an optimization.  A newly released Lean or Mathlib
    revision may legitimately have no published archives yet; the confined
    build below can compile the required target from source in that case.
    """
    if cache_dir.is_symlink():
        cache_dir.unlink()
    elif cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir()
    config = trusted_work / "mathlib-cache-download.conf"
    if config.is_symlink() or (config.exists() and not config.is_file()):
        raise VerificationError("invalid Mathlib cache download configuration path")
    if config.exists():
        config.unlink()
    clean_path = "/usr/bin:/bin"
    clean_env = {
        "HOME": environment["HOME"],
        "PATH": clean_path,
        "TMPDIR": environment["TMPDIR"],
        "LANG": "C.UTF-8",
    }
    downloaded: set[str] = set()
    total = 0
    try:
        for base_url in MATHLIB_CACHE_BASE_URLS:
            remaining = sorted(hashes - downloaded)
            if not remaining:
                break
            lines: list[str] = []
            for digest in remaining:
                url = f"{base_url}/f/{digest}.ltar"
                destination = cache_dir / f"{digest}.ltar"
                lines.extend(
                    [f"url = {json.dumps(url)}", f"output = {json.dumps(str(destination))}"]
                )
            config.write_text("\n".join(lines) + "\n", encoding="utf-8")
            command = [
                str(env_tool),
                "-i",
                *(f"{name}={value}" for name, value in clean_env.items()),
                str(curl),
                "--disable",
                "--fail",
                "--silent",
                "--show-error",
                "--remove-on-error",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--parallel",
                "--parallel-immediate",
                "--parallel-max",
                "32",
                "--retry",
                "3",
                "--max-filesize",
                str(MAX_CACHE_ARCHIVE_BYTES),
                "--config",
                str(config),
            ]
            run(
                systemd_command(
                    command,
                    cwd=trusted_work,
                    environment=environment,
                    unrestricted_network=True,
                    resource_properties=CACHE_DOWNLOAD_PROPERTIES,
                ),
                cwd=trusted_work,
                # systemd-run needs the caller's user-bus variables. The actual
                # curl child is still launched through ``env -i`` above.
                env=environment,
                timeout=1800,
                check=False,
            )
            downloaded = set()
            total = 0
            for path in cache_dir.iterdir():
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not re.fullmatch(r"[0-9a-f]{16}\.ltar", path.name)
                ):
                    raise VerificationError(f"unexpected Mathlib cache download: {path.name!r}")
                if path.stem not in hashes:
                    raise VerificationError(f"unrequested Mathlib cache download: {path.name!r}")
                size = path.stat().st_size
                if size == 0 or size > MAX_CACHE_ARCHIVE_BYTES:
                    raise VerificationError(f"invalid Mathlib cache archive size: {path.name}")
                downloaded.add(path.stem)
                total += size
                if total > MAX_CACHE_BYTES:
                    raise VerificationError("Mathlib cache downloads exceed the byte cap")
    finally:
        config.unlink(missing_ok=True)
    return len(downloaded), total


def hydrate_mathlib_cache(
    workspace: Path,
    *,
    trusted_work: Path,
    environment: dict[str, str],
    lake: Path,
    landrun: Path,
    curl: Path,
    env_tool: Path,
    writable_directories: list[Path],
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> dict[str, int]:
    hashes, cache_dir = discover_mathlib_cache_hashes(
        workspace,
        environment=environment,
        lake=lake,
        landrun=landrun,
        writable_directories=writable_directories,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
    )
    downloaded, total = download_mathlib_cache(
        hashes,
        cache_dir,
        trusted_work=trusted_work,
        environment=environment,
        curl=curl,
        env_tool=env_tool,
    )
    cache_env = environment.copy()
    cache_env["MATHLIB_CACHE_DIR"] = str(cache_dir.resolve())
    if downloaded:
        sandboxed_run(
            [str(lake), "exe", "cache", "unpack"],
            cwd=workspace,
            environment=cache_env,
            landrun=landrun,
            writable_directories=writable_directories,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            timeout=1800,
            resource_properties=RESOURCE_PROPERTIES,
        )
    shutil.rmtree(cache_dir)
    return {"requested": len(hashes), "downloaded": downloaded, "bytes": total}


def prepare_build_metadata_files(workspace: Path) -> tuple[Path, ...]:
    """Create only the sidecars Lake must update beside tracked ProofWidgets assets."""
    proofwidgets = next(
        (
            package
            for package in manifest_packages(workspace)
            if package["name"] == "proofwidgets"
        ),
        None,
    )
    if proofwidgets is None or proofwidgets["repository"].lower() != "leanprover-community/proofwidgets4":
        raise VerificationError("renderer requires the canonical pinned ProofWidgets package")
    package_dir = workspace / ".lake" / "packages" / "proofwidgets"
    relative_paths = (
        Path("widget/package-lock.json.hash"),
        Path("widget/js/lake.trace.hash"),
    )
    result: list[Path] = []
    for relative in relative_paths:
        path = package_dir / relative
        if path.exists() or path.is_symlink() or not path.parent.is_dir():
            raise VerificationError(f"unexpected ProofWidgets build sidecar: {relative}")
        path.write_bytes(b"")
        result.append(path.resolve())
    return tuple(result)


def validate_build_metadata_files(paths: tuple[Path, ...]) -> None:
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024:
            raise VerificationError(f"invalid dependency build sidecar: {path.name}")


def execute(args: argparse.Namespace) -> int:
    work = Path(args.work_dir).resolve()
    output = Path(args.output).resolve()
    report = load_json_object(output)
    try:
        prepared = parse_prepared_report(report)
    except RenderReportError as error:
        previous_errors = report.get("errors")
        failure = intake_report(now())
        failure["stage"] = "contract"
        failure["errors"] = [
            *(
                item
                for item in (previous_errors if isinstance(previous_errors, list) else [])
                if isinstance(item, str)
            ),
            str(error),
        ]
        write_json(output, failure)
        return 1
    report = prepared.as_dict()
    report.update(
        {
            "status": "error",
            "stage": "workspace",
            "renderer_commit": args.renderer_commit,
            "landrun_commit": args.landrun_commit,
            "workflow_url": args.workflow_url,
        }
    )
    tools: dict[Path, str] = {}
    try:
        if not SHA_RE.fullmatch(args.renderer_commit) or not SHA_RE.fullmatch(args.landrun_commit):
            raise VerificationError("renderer and Landrun commits must be immutable SHAs")
        source = work / "source"
        challenge_relative = normalized_repository_path(
            prepared.source.paths.challenge_path,
            "render challenge_path",
        )
        accepted_challenge = resolve_repository_path(
            source, challenge_relative, "render challenge_path", kind="file"
        )
        if sha256(accepted_challenge) != prepared.source.challenge_sha256:
            raise VerificationError("accepted Challenge source changed after render intake")
        workspace_checkout = work / "workspace"
        render_workspace = prepare_workspace(
            source,
            workspace_checkout,
            prepared.verso_commit,
            work,
            prepared.source.paths,
        )
        workspace = render_workspace.project

        landrun = Path(args.landrun).resolve(strict=True)
        renderer = Path(__file__).resolve(strict=True)
        env = os.environ.copy()
        env.pop("LAKE_PKG_URL_MAP", None)
        env.update(
            {
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        env["LEAN_ABORT_ON_PANIC"] = "1"
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        elan_command = shutil.which("elan", path=env["PATH"])
        if not elan_command:
            raise VerificationError("trusted elan executable is unavailable")
        toolchain_env = env.copy()
        toolchain_env["ELAN_TOOLCHAIN"] = prepared.lean_toolchain
        lean = Path(
            run([elan_command, "which", "lean"], cwd=work, env=toolchain_env).stdout.strip()
        ).resolve(strict=True)
        lean_prefix = lean.parent.parent
        lake = (lean_prefix / "bin" / "lake").resolve(strict=True)
        if lean != (lean_prefix / "bin" / "lean").resolve(strict=True):
            raise VerificationError("elan returned an unexpected Lean executable path")
        python = Path(sys.executable).resolve(strict=True)
        printenv = required_executable("printenv", env)
        touch = required_executable("touch", env)
        curl = required_executable("curl", env)
        git = required_executable("git", env)
        env_tool = required_executable("env", env)
        env["PATH"] = f"{lean_prefix / 'bin'}:{env['PATH']}"

        writable_directories = materialize_packages(
            workspace, checkout=workspace_checkout, base_env=env
        )
        readable_paths = [workspace_checkout.resolve()]
        sandbox_home = workspace / ".lake" / "config" / "home"
        sandbox_tmp = workspace / ".lake" / "config" / "tmp"
        sandbox_home.mkdir()
        sandbox_tmp.mkdir()
        env["HOME"] = str(sandbox_home.resolve())
        env["TMPDIR"] = str(sandbox_tmp.resolve())
        allowed_exec = executable_paths(
            lean_prefix,
            [landrun, renderer, lake, lean, python, printenv, touch, curl, git, env_tool],
        )
        require_protected_paths(
            [
                output,
                landrun,
                renderer,
                lake,
                lean,
                python,
                printenv,
                touch,
                curl,
                git,
                env_tool,
                lean_prefix,
            ],
            writable_directories,
        )
        tools = tool_snapshot(
            [landrun, renderer, lake, lean, python, printenv, touch, curl, git, env_tool]
        )
        # The render build is where untrusted compile-time Lean runs, so it
        # gets the verifier's whole probe set rather than a write-only subset.
        # Every phase Landrun confines from here on is network-disabled. The
        # only outbound step that follows is trusted `curl` fetching Mathlib
        # cache archives, outside Landrun and without loading submitted Lake
        # configuration. The trusted Verso clone and the pinned package
        # fetches above were also outbound and also outside Landrun; neither
        # executes submitted Lean or Lake code, and no submitted code has run
        # at this point. Egress denial therefore holds for exactly the
        # confined phases this probe stands for.
        verify_sandbox_confinement(
            work / "render-landrun-write-denial-probe",
            work / "render-landrun-read-denial-probe",
            positive_read=render_workspace.challenge,
            python=python,
            touch=touch,
            cwd=workspace,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            readable_paths=readable_paths,
            executable_paths=allowed_exec,
            tools=tools,
        )
        report["mathlib_cache"] = hydrate_mathlib_cache(
            workspace,
            trusted_work=work,
            environment=env,
            lake=lake,
            landrun=landrun,
            curl=curl,
            env_tool=env_tool,
            writable_directories=writable_directories,
            readable_paths=readable_paths,
            executable_paths=allowed_exec,
            tools=tools,
        )
        writable_files = prepare_build_metadata_files(workspace)

        report["stage"] = "literate"
        write_json(output, report)
        sandboxed_run(
            [str(lake), "build", "Challenge:literate"],
            cwd=workspace,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            writable_files=writable_files,
            readable_paths=readable_paths,
            executable_paths=allowed_exec,
            tools=tools,
            timeout=BUILD_TIMEOUT_SECONDS,
            resource_properties=RESOURCE_PROPERTIES,
        )
        if tree_bytes(workspace / ".lake", stop_after=MAX_RENDER_WORK_BYTES) > MAX_RENDER_WORK_BYTES:
            raise VerificationError("render workspace exceeds the disk cap")
        validate_build_metadata_files(writable_files)

        raw_output = workspace / ".lake" / "build" / "palomar-render-raw"
        clean_output = workspace / ".lake" / "build" / "palomar-render-clean"
        raw_output.mkdir()
        clean_output.mkdir()
        report["stage"] = "verso-html"
        write_json(output, report)
        sandboxed_run(
            [
                str(lake),
                "exe",
                "verso-html",
                str(workspace / ".lake" / "build" / "literate"),
                str(raw_output),
            ],
            cwd=workspace,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            writable_files=writable_files,
            readable_paths=readable_paths,
            executable_paths=allowed_exec,
            tools=tools,
            timeout=HTML_TIMEOUT_SECONDS,
            resource_properties=RESOURCE_PROPERTIES,
        )

        report["stage"] = "sanitize"
        write_json(output, report)
        sandboxed_run(
            [
                str(python),
                str(renderer),
                "sanitize",
                "--input-dir",
                str(raw_output),
                "--output-dir",
                str(clean_output),
                "--challenge",
                str(render_workspace.challenge),
                "--solution",
                str(render_workspace.solution),
                "--comparator",
                str(render_workspace.comparator),
                "--challenge-module",
                render_workspace.challenge_module,
                "--lean",
                str(lean),
            ],
            cwd=workspace,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            writable_files=writable_files,
            readable_paths=readable_paths,
            executable_paths=allowed_exec,
            tools=tools,
            timeout=SANITIZE_TIMEOUT_SECONDS,
            resource_properties=RESOURCE_PROPERTIES,
        )
        files, tree_hash = artifact_manifest(clean_output)
        validate_build_metadata_files(writable_files)
        embedded_manifest = load_json_object(clean_output / "artifact-manifest.json")
        if (
            embedded_manifest.get("files") != files
            or embedded_manifest.get("artifact_tree_sha256") != tree_hash
        ):
            raise VerificationError("sanitized artifact manifest is inconsistent")
        surface = load_json_object(clean_output / "challenge-metadata.json")
        result_dir = work / "result"
        if result_dir.exists():
            raise VerificationError("render result directory already exists")
        result_dir.mkdir()
        shutil.copytree(clean_output, result_dir / "bundle")
        report.update(
            {
                "status": "pass",
                "stage": "complete",
                "format": "verso-html",
                "entrypoint": "Challenge/index.html",
                "artifact_tree_sha256": tree_hash,
                "surface": surface,
                "rendered_at": now(),
                "limits": {
                    "wall_seconds": BUILD_TIMEOUT_SECONDS,
                    "memory_bytes": 12 * 1024 * 1024 * 1024,
                    "tasks": 512,
                    "open_files": 4096,
                    "per_file_bytes": MAX_RENDER_FILE_BYTES,
                    "build_file_bytes": MAX_BUILD_FILE_BYTES,
                    "artifact_files": MAX_RENDER_FILES,
                    "artifact_nodes": MAX_RENDER_NODES,
                    "artifact_bytes": MAX_RENDER_BYTES,
                    "workspace_bytes": MAX_RENDER_WORK_BYTES,
                },
            }
        )
        write_json(result_dir / "challenge-render.json", report)
        write_json(output, report)
        return 0
    except subprocess.TimeoutExpired:
        report["errors"].append("Challenge rendering timed out")
    except Exception as error:  # noqa: BLE001 - workflow failures become a bounded report
        report["errors"].append(str(error))
    write_json(output, report)
    return 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--repository", required=True)
    prepare_parser.add_argument("--commit", required=True)
    prepare_parser.add_argument("--challenge-sha256", required=True)
    prepare_parser.add_argument("--project-path", required=True)
    prepare_parser.add_argument("--challenge-path", required=True)
    prepare_parser.add_argument("--solution-path", required=True)
    prepare_parser.add_argument("--comparator-config-path", required=True)
    prepare_parser.add_argument("--lakefile-path", required=True)
    prepare_parser.add_argument("--lean-toolchain-path", required=True)
    prepare_parser.add_argument("--work-dir", required=True)
    prepare_parser.add_argument("--output", required=True)
    prepare_parser.set_defaults(func=prepare)
    execute_parser = commands.add_parser("execute")
    execute_parser.add_argument("--work-dir", required=True)
    execute_parser.add_argument("--output", required=True)
    execute_parser.add_argument("--landrun", required=True)
    execute_parser.add_argument("--renderer-commit", required=True)
    execute_parser.add_argument("--landrun-commit", required=True)
    execute_parser.add_argument("--workflow-url", required=True)
    execute_parser.set_defaults(func=execute)
    sanitize_parser = commands.add_parser("sanitize")
    sanitize_parser.add_argument("--input-dir", required=True)
    sanitize_parser.add_argument("--output-dir", required=True)
    sanitize_parser.add_argument("--challenge", required=True)
    sanitize_parser.add_argument("--solution", required=True)
    sanitize_parser.add_argument("--comparator", required=True)
    sanitize_parser.add_argument("--challenge-module", required=True)
    sanitize_parser.add_argument("--lean", required=True)
    sanitize_parser.set_defaults(func=sanitize_command)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        return args.func(args)
    except (OSError, ValueError, VerificationError, json.JSONDecodeError) as error:
        print(f"render challenge: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
