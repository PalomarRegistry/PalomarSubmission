#!/usr/bin/env python3
"""Build one immutable, browser-confined Verso rendering of Challenge.lean."""

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
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.verify_submission import (  # noqa: E402
    GITHUB_RE,
    MAX_SOURCE_BYTES,
    SHA_RE,
    VerificationError,
    clone_commit,
    direct_imports,
    github_repository,
    load_comparator_config,
    manifest_packages,
    materialize_packages,
    now,
    require_protected_paths,
    run,
    sandboxed_run,
    sha256,
    systemd_command,
    tool_snapshot,
    tree_size,
    verify_filesystem_confinement,
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
BUILD_TIMEOUT_SECONDS = 60 * 60
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
  const blocked = new Set([
    "APPLET", "BASE", "EMBED", "FORM", "FRAME", "FRAMESET", "IFRAME",
    "META", "OBJECT", "SCRIPT"
  ]);
  const activeAttributes = new Set([
    "action", "formaction", "ping", "srcdoc"
  ]);

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
      if (blocked.has(element.tagName)) {
        element.remove();
        continue;
      }
      for (const attribute of Array.from(element.attributes)) {
        const name = attribute.name.toLowerCase();
        if (name.startsWith("on") || activeAttributes.has(name)) {
          element.removeAttribute(attribute.name);
        } else if (name === "src" && !safeUrl(attribute.value, name)) {
          element.removeAttribute(attribute.name);
        } else if (name === "href" && !safeUrl(attribute.value, name)) {
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
        const box = target.getBoundingClientRect();
        popup.style.position = "fixed";
        popup.style.left = `${Math.max(8, Math.min(box.left, innerWidth - 420))}px`;
        popup.style.top = `${Math.min(innerHeight - 160, box.bottom + 6)}px`;
        popup.style.maxWidth = "400px";
        popup.style.maxHeight = "300px";
        popup.style.overflow = "auto";
        popup.style.zIndex = "2147483647";
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
    challenge: Path, solution: Path, comparator: Path
) -> dict[str, Any]:
    for path in (challenge, solution, comparator):
        if path.is_symlink() or not path.is_file():
            raise VerificationError(f"render metadata input is missing or invalid: {path.name}")
    source = challenge.read_text(encoding="utf-8")
    config = load_comparator_config(comparator)
    declarations = [*config["theorem_names"], *config.get("definition_names", [])]
    if len(declarations) != len(set(declarations)):
        raise VerificationError("comparator declaration names must be unique")
    return {
        "schema_version": 2,
        "imports": direct_imports(source),
        "module_doc": extract_module_doc(source),
        "declarations": declarations,
        "solution_imports": direct_imports(solution.read_text(encoding="utf-8")),
    }


def load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise VerificationError(f"{path.name} must contain a JSON object")
    return value


def toolchain_verso_commit(toolchain: str) -> str:
    mapping = load_json_object(ROOT / "toolchains.json")
    commit = mapping.get("verso", {}).get(toolchain)
    if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
        raise VerificationError(f"no probed Verso renderer for Lean toolchain: {toolchain}")
    return commit


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
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "error",
        "stage": "intake",
        "errors": [],
        "prepared_at": now(),
    }
    try:
        repository, repository_url = normalized_repository(args.repository)
        commit = args.commit.strip().lower()
        challenge_sha256 = args.challenge_sha256.strip().lower()
        if not SHA_RE.fullmatch(commit):
            raise VerificationError("commit must be 40 lowercase hexadecimal characters")
        if not re.fullmatch(r"[0-9a-f]{64}", challenge_sha256):
            raise VerificationError("challenge_sha256 must be 64 lowercase hexadecimal characters")
        source = work / "source"
        clone_commit(repository_url, commit, source)
        if tree_size(source) > MAX_SOURCE_BYTES:
            raise VerificationError("checked-out source exceeds the 500 MiB cap")
        challenge = source / "Challenge.lean"
        toolchain_file = source / "lean-toolchain"
        manifest_file = source / "lake-manifest.json"
        for path in (challenge, toolchain_file, manifest_file):
            if path.is_symlink() or not path.is_file():
                raise VerificationError(f"required regular source file is missing: {path.name}")
        actual_challenge_sha256 = sha256(challenge)
        if actual_challenge_sha256 != challenge_sha256:
            raise VerificationError("Challenge.lean does not match the accepted mechanical report")
        toolchain = toolchain_file.read_text(encoding="utf-8").strip()
        verso_commit = toolchain_verso_commit(toolchain)
        report.update(
            {
                "status": "pending",
                "stage": "prepared",
                "source": {
                    "repository": repository,
                    "repository_url": repository_url,
                    "commit": commit,
                    "challenge_sha256": challenge_sha256,
                },
                "lean_toolchain": toolchain,
                "verso_commit": verso_commit,
            }
        )
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


def trusted_lakefile(source_manifest: dict[str, Any], verso_commit: str) -> str:
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
            if not directory or Path(directory).is_absolute() or ".." in Path(directory).parts:
                raise VerificationError(f"invalid direct path package: {name!r}")
            lines.extend([f"path = {toml_string(directory)}", ""])
        else:
            raise VerificationError(f"unsupported direct package type for {name!r}")
    lines.extend(["[[lean_lib]]", 'name = "Challenge"', ""])
    return "\n".join(lines)


def prepare_workspace(source: Path, workspace: Path, verso_commit: str, work: Path) -> None:
    if workspace.exists():
        raise VerificationError("render workspace already exists")
    source_manifest = load_json_object(source / "lake-manifest.json")
    verso_probe = work / "verso-manifest"
    clone_commit(f"https://github.com/{VERSO_REPOSITORY}", verso_commit, verso_probe)
    try:
        verso_toolchain = (verso_probe / "lean-toolchain").read_text(encoding="utf-8").strip()
        source_toolchain = (source / "lean-toolchain").read_text(encoding="utf-8").strip()
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
    lakefile_lean = workspace / "lakefile.lean"
    if lakefile_lean.exists() or lakefile_lean.is_symlink():
        if lakefile_lean.is_dir() and not lakefile_lean.is_symlink():
            shutil.rmtree(lakefile_lean)
        else:
            lakefile_lean.unlink()
    (workspace / "lakefile.toml").write_text(
        trusted_lakefile(source_manifest, verso_commit), encoding="utf-8"
    )
    write_json(workspace / "lake-manifest.json", merged_manifest)


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
    }

    def __init__(self, declarations: list[str]) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.bases: list[str | None] = []
        self.declarations = set(declarations)
        self.found_declarations: set[str] = set()
        self.script_depth = 0

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
        if self.script_depth:
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
        if self.script_depth:
            if tag == "script":
                self.script_depth = 0
            return
        if tag not in {"base", "meta", "script"}:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.script_depth:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self.script_depth:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.script_depth:
            self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if not self.script_depth:
            self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        if not self.script_depth:
            self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        if not self.script_depth:
            self.parts.append(f"<?{data}>")


def static_html_sanitize(
    text: str,
    sanitizer_src: str,
    runtime_src: str,
    declarations: list[str] | None = None,
) -> str:
    sanitizer = _StaticHTMLSanitizer(declarations or [])
    sanitizer.feed(text)
    if sanitizer.rawdata:
        raise VerificationError("generated HTML ends with incomplete markup")
    sanitizer.close()
    if sanitizer.bases != ["../"]:
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

    text, body_count = re.subn(
        r"<body(\s[^>]*)?>", annotate_body, text, count=1, flags=re.IGNORECASE
    )
    if body_count != 1:
        raise VerificationError("generated HTML does not contain exactly one body element")

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
    text, count = re.subn(
        r"(<head\b[^>]*>)",
        rf"\1\n    {meta}\n    {scripts}",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    if count != 1:
        raise VerificationError("generated HTML does not contain exactly one head element")
    surface_style = """<style id="palomar-declaration-style">
      html { height: auto !important; min-height: 100%; overflow: auto !important;
        overscroll-behavior: contain; }
      body { height: auto !important; min-height: 100%; margin: 0;
        overflow: visible !important; }
      .palomar-declaration-surface { box-sizing: border-box; padding: 1rem; }
      .palomar-declaration-surface .code-content { margin: 0; max-width: none; padding: 0; }
      .palomar-declaration-surface .md-text::before,
      .palomar-declaration-surface .md-text::after { width: max-content; white-space: nowrap; }
      .palomar-hover {
        padding: .65rem !important; border: 1px solid #888 !important;
        border-radius: .35rem; background: #fff !important; color: #24292e !important;
        box-shadow: 0 .25rem 1rem rgb(0 0 0 / 22%);
      }
      .palomar-hover .popup,
      .palomar-hover .hover-info,
      .palomar-hover .docstring { background: #fff !important; color: #24292e !important; }
      .palomar-render-error { margin: 1rem; font-family: sans-serif; }
    </style>"""
    text = re.sub(
        r"(</head\s*>)", rf"    {surface_style}\n  \1", text, count=1, flags=re.IGNORECASE
    )
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
    (output_dir / "palomar-sanitize.js").write_text(RUNTIME_SANITIZER, encoding="utf-8")
    (output_dir / "palomar-verso.js").write_text(VERSO_RUNTIME, encoding="utf-8")
    write_json(output_dir / "challenge-metadata.json", metadata)
    for source, relative in source_files:
        if source.suffix.lower() == ".js":
            continue
        destination = output_dir / relative
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
                    declarations if relative == "Challenge/index.html" else [],
                ),
                encoding="utf-8",
            )
        else:
            shutil.copyfile(source, destination)
    entrypoint = output_dir / "Challenge" / "index.html"
    if not entrypoint.is_file():
        raise VerificationError("Verso output does not contain Challenge/index.html")
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
    )
    sanitize_bundle(
        Path(args.input_dir).resolve(),
        Path(args.output_dir).resolve(),
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
    """Fetch fixed-host opaque cache bytes without executing source-derived code."""
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
    lines: list[str] = []
    for digest in sorted(hashes):
        url = f"https://lakecache.blob.core.windows.net/mathlib4/f/{digest}.ltar"
        destination = cache_dir / f"{digest}.ltar"
        lines.extend([f"url = {json.dumps(url)}", f"output = {json.dumps(str(destination))}"])
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    clean_path = "/usr/bin:/bin"
    clean_env = {
        "HOME": environment["HOME"],
        "PATH": clean_path,
        "TMPDIR": environment["TMPDIR"],
        "LANG": "C.UTF-8",
    }
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
    proc = run(
        systemd_command(
            command,
            cwd=trusted_work,
            environment=environment,
            unrestricted_network=True,
            resource_properties=CACHE_DOWNLOAD_PROPERTIES,
        ),
        cwd=trusted_work,
        # systemd-run needs the caller's user-bus variables.  The actual curl
        # child is still launched through ``env -i`` above.
        env=environment,
        timeout=1800,
        check=False,
    )
    config.unlink()
    downloaded = 0
    total = 0
    for path in cache_dir.iterdir():
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[0-9a-f]{16}\.ltar", path.name):
            raise VerificationError(f"unexpected Mathlib cache download: {path.name!r}")
        if path.stem not in hashes:
            raise VerificationError(f"unrequested Mathlib cache download: {path.name!r}")
        size = path.stat().st_size
        if size == 0 or size > MAX_CACHE_ARCHIVE_BYTES:
            raise VerificationError(f"invalid Mathlib cache archive size: {path.name}")
        downloaded += 1
        total += size
        if total > MAX_CACHE_BYTES:
            raise VerificationError("Mathlib cache downloads exceed the byte cap")
    if downloaded == 0:
        detail = (proc.stderr or proc.stdout).strip()[-1000:]
        raise VerificationError(f"no Mathlib cache archives were downloaded: {detail}")
    return downloaded, total


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
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> dict[str, int]:
    hashes, cache_dir = discover_mathlib_cache_hashes(
        workspace,
        environment=environment,
        lake=lake,
        landrun=landrun,
        writable_directories=writable_directories,
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
    sandboxed_run(
        [str(lake), "exe", "cache", "unpack"],
        cwd=workspace,
        environment=cache_env,
        landrun=landrun,
        writable_directories=writable_directories,
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
    if report.get("status") != "pending":
        return 1
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
        if sha256(source / "Challenge.lean") != report["source"]["challenge_sha256"]:
            raise VerificationError("Challenge.lean changed after render intake")
        workspace = work / "workspace"
        prepare_workspace(source, workspace, str(report["verso_commit"]), work)

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
        toolchain_env["ELAN_TOOLCHAIN"] = str(report["lean_toolchain"])
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

        writable_directories = materialize_packages(workspace, base_env=env)
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
        verify_filesystem_confinement(
            work / "render-landrun-denial-probe",
            touch=touch,
            cwd=workspace,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
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
                str(workspace / "Challenge.lean"),
                "--solution",
                str(workspace / "Solution.lean"),
                "--comparator",
                str(workspace / "comparator.json"),
            ],
            cwd=workspace,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            writable_files=writable_files,
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
