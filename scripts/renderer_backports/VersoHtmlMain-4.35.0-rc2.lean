/-
Copyright (c) 2025 Lean FRO LLC. All rights reserved.
Released under Apache 2.0 license as described in the file LICENSE.
Author: David Thrane Christiansen
-/
module
public import VersoLiterateCode
public import VersoSearch.DomainSearch
public import Verso.Code.Highlighted.WebAssets
public import Verso.Output.Html.CssVars

public section

open Lean

open Verso.Output.Html
open Verso.Code
open Verso (withLogger)

open VersoLiterate
open VersoLiterateCode
open Std

structure Config where
  inputDir : System.FilePath
  outputDir : System.FilePath
  declarationsFile : Option System.FilePath := none

def getConfig (args : List String) : Except String Config :=
  match args with
  | [i, o] => pure { inputDir := i, outputDir := o }
  | [i, o, d] => pure { inputDir := i, outputDir := o, declarationsFile := some d }
  | _ => throw s!"Didn't understand args: {args}. Expected INDIR OUTDIR"

def code.css := include_str "code.css"

open Verso Output Html Search in
/--
The `<head>` fragment shared by module pages and the full-page search results view.
Bundles the code-highlighting assets and every piece of the search UI (combobox on
normal pages; plain input + live-updating list on `/search/`).
-/
private def headContents : Html := {{
  <!-- Stop favicon requests -->
  <link rel="icon" href="data:," />

  <script src="popper.js"></script>
  <script src="tippy.js"></script>
  <script>{{Html.text false highlightingJs}}</script>
  <style>{{Html.text false highlightingStyle}}</style>
  <link rel="stylesheet" href="verso-vars.css"/>
  <link rel="stylesheet" href="tippy-border.css"/>
  <link rel="stylesheet" href="code.css"/>

  {{ searchAssetTags }}
}}

open Verso Output Doc Html in
def emitMod (root : Dir) (outDir: System.FilePath) (mod : LitMod) (selected : Array Name := #[]) : EmitM Unit := do
  let components := mod.name.components
  let nesting := components.length

  let siteRoot := if nesting = 0 then "./" else nesting.fold (init := "") fun _ _ s => s ++ "../"

  let mut breadcrumbs := Html.empty
  for h : i in [0:components.length - 1] do
    let addr := components.take (i+1) |>.map (·.toString) |> "/".intercalate
    have : i < components.length := by
      have := h.2.1
      grind
    breadcrumbs := {{<li><a href={{addr}}><code>{{toString components[i]}}</code></a></li>}} ++ breadcrumbs

  let htmlId? := (← read).moduleIds.find? mod.name

  let children : Array Html :=
    if let some d := root[mod.name]? then
      d.children.map fun (c : Name × Dir) =>
        {{<li><a href={{(components.map toString |> "/".intercalate) ++ "/" ++ toString c.1}}>{{mod.name ++ c.1 |> toString}}</a></li>}}
    else #[]

  let base ← read
  let ctx := { base with
    options := {}
    traverseContext := { currentModule := mod.name }
    codeOptions := {}
  }

  let (results, st) ← mod.contents.mapIdxM renderCode |>.run ctx (← get).hlState
  -- Discard the accumulated headers because there's no local ToC here
  let body : Html := .seq (results.map (·.1))

  modify ({ · with hlState := st })

  let contents := page (toString mod.name) siteRoot headContents mod.name root htmlId? body

  let outFile := mod.name.components.map (·.toString) |>.foldl (init := outDir) (· / ·)

  IO.FS.createDirAll outFile

  if selected.isEmpty then
    IO.FS.writeFile (outFile / "index.html") <| "<!DOCTYPE html>\n" ++ contents.asString
  else
    -- Prototype only: all source commands remain on whole-command pages.
    -- The original compared entrypoint contains every selected declaration.
    let cap := 4 * 1024 * 1024
    let fileCap := 8 * 1024 * 1024
    let mut chunks : Array (Array Html) := #[]
    let mut pending : Array Html := #[]
    let mut pendingBytes := 0
    let mut selectedHtml : Array Html := #[]
    let mut found : Array Name := #[]
    for item in mod.contents, result in results do
      let h := result.1
      let bytes := h.asString.utf8ByteSize
      if bytes > cap then
        throw <| IO.userError "one source command exceeds pagination cap"
      if pendingBytes + bytes > cap && !pending.isEmpty then
        chunks := chunks.push pending
        pending := #[]
        pendingBytes := 0
      pending := pending.push h
      pendingBytes := pendingBytes + bytes
      if item.defines.any selected.contains then
        selectedHtml := selectedHtml.push h
        found := found ++ item.defines.filter selected.contains
    if !pending.isEmpty then chunks := chunks.push pending
    if selected.any (fun n => found.count n != 1) then
      throw <| IO.userError "selected declaration missing or duplicated"
    let modulePath := "/".intercalate (components.map toString)
    let links : Array Html := chunks.mapIdx fun i _ =>
      {{<a href={{s!"{modulePath}/source-{i + 1}.html"}}>{{s!"Source page {i + 1}"}}</a>}}
    let navigation : Html := {{<nav aria-label="Complete source">{{links}}</nav>}}
    let mut pageNames : Array String := #[]
    for chunk in chunks, i in [0:chunks.size] do
      let name := s!"source-{i + 1}.html"
      let text := "<!DOCTYPE html>\n" ++
        (page (toString mod.name) siteRoot headContents mod.name root htmlId?
          (navigation ++ .seq chunk)).asString
      if text.utf8ByteSize > fileCap then
        throw <| IO.userError "source page exceeds unchanged file cap"
      IO.FS.writeFile (outFile / name) text
      pageNames := pageNames.push name
    let selectedText := "<!DOCTYPE html>\n" ++
      (page (toString mod.name) siteRoot headContents mod.name root htmlId?
        (.seq selectedHtml)).asString
    if selectedText.utf8ByteSize > fileCap then
      throw <| IO.userError "compared declaration surface exceeds unchanged file cap"
    IO.FS.writeFile (outFile / "index.html") selectedText
    IO.FS.writeFile (outFile / "source-pages.json") <| Json.compress <| Json.mkObj [
      ("schema_version", toJson (1 : Nat)),
      ("module", toJson mod.name.toString),
      ("source_commands", toJson mod.contents.size),
      ("selected_declarations", toJson (selected.map toString)),
      ("pages", toJson pageNames)]

def emitDir (outDir : System.FilePath) (dir : Dir) (selected : Array Name := #[]) : EmitM Unit := do
  let root := dir
  let mut todo := [dir]
  repeat
    match todo with
    | [] => break
    | d :: ds =>
      todo := ds
      if let some m := d.mod then
        emitMod root outDir m selected
      for c in d.children do
        todo := c.2 :: todo

open Verso Output Html in
/--
Emits the search page at `search/index.html`. Result rendering is deferred to `search-page.js`;
`<base href="../"/>` makes the search infrastructure resolve against the site root.
-/
def emitSearchResultsPage (outDir : System.FilePath) : IO Unit := do
  let pageHtml : Html := {{
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <base href="../"/>
        <title>"Search"</title>
        <!-- Start the xref.json download in parallel with script loading. The
             search page JS can't fetch it until it runs, so without the preload
             the data fetch sits at the tail of the critical path. -->
        <link rel="preload" href="xref.json" as="fetch"/>
        {{ headContents }}
      </head>
      <body>
        <main class="search-page" id="main-content">
          <h1>"Search"</h1>
          <div data-search-host class="search-page-host"></div>
          <noscript><p>"This search feature requires JavaScript."</p></noscript>
          <div id="search-page-results"></div>
          <script type="module" src="-verso-search/search-page.js"></script>
        </main>
      </body>
    </html>
  }}
  IO.FS.createDirAll (outDir / "search")
  IO.FS.writeFile (outDir / "search" / "index.html") <| "<!DOCTYPE html>\n" ++ pageHtml.asString

def main (args : List String) : IO UInt32 := do
  let config ←
    match getConfig args with
    | .error e => throw <| .userError e
    | .ok v => pure v
  let selected ← match config.declarationsFile with
    | none => pure #[]
    | some f => do
      let j ← IO.ofExcept <| Json.parse (← IO.FS.readFile f)
      let names ← IO.ofExcept <| fromJson? (α := Array String) j
      if names.isEmpty || names.toList.eraseDups.length != names.size then
        throw <| IO.userError "declaration list must be nonempty and unique"
      pure (names.map String.toName)
  withLogger fun logger => do
    IO.FS.createDirAll config.outputDir
    IO.FS.writeFile (config.outputDir / "popper.js") Verso.Code.Highlighted.WebAssets.popper
    IO.FS.writeFile (config.outputDir / "tippy.js") Verso.Code.Highlighted.WebAssets.tippy
    IO.FS.writeFile (config.outputDir / "tippy-border.css") Verso.Code.Highlighted.WebAssets.tippy.border.css
    IO.FS.writeFile (config.outputDir / "code.css") code.css
    IO.FS.writeFile (config.outputDir / "verso-vars.css") «verso-vars.css»
    emitSearchBox (config.outputDir / "-verso-search") (searchPagePath := some "search/")
    let dir ← loadDir config.inputDir
    let dir := dir.sort
    let (dir, traverseState) ← traverse dir
    let ctx := {
      definitionIds := traverseState.definitionIds
      linkTargets := traverseState.linkTargets
      moduleIds := traverseState.moduleIds
      traverseState
    }
    let ((), st) ← emitDir config.outputDir dir selected |>.run ctx |>.run {} |>.run logger
    emitIndex {} traverseState dir (config.outputDir / "-verso-search") logger
    emitSearchResultsPage config.outputDir
    let domainData : Verso.NameMap Verso.Multi.Domain := ({} : Verso.NameMap _)
      |>.insert constDomainName traverseState.constantDefDomain
      |>.insert moduleDomainName traverseState.moduleDomain
    IO.FS.writeFile (config.outputDir / "xref.json") <| Json.compress <| Verso.Multi.xrefJson domainData traverseState.allLinks
    IO.FS.writeFile (config.outputDir / "-verso-docs.json") st.hlState.dedup.docJson.compress
