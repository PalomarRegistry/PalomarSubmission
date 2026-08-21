import Lean
import Lean.Util.FoldConsts

open Lean

private structure AuditRow where
  name : String
  declaration : String
deriving ToJson

private def usage : String :=
  "usage: palomar-audit MODULE (theorem|def NAME)+"

private def parseRequests (args : List String) : IO (Name × Array (String × String × Name)) := do
  let moduleText :: requestArgs := args
    | throw <| IO.userError usage
  if moduleText.isEmpty || requestArgs.isEmpty then
    throw <| IO.userError usage
  let rec loop (remaining : List String) (requests : Array (String × String × Name)) := do
    match remaining with
    | [] => pure requests
    | kind :: name :: tail =>
      unless kind == "theorem" || kind == "def" do
        throw <| IO.userError usage
      if name.isEmpty then
        throw <| IO.userError usage
      loop tail (requests.push (kind, name, name.toName))
    | _ => throw <| IO.userError usage
  return (moduleText.toName, ← loop requestArgs #[])

/--
Copy only declaration names, universe parameters, and types into the trusted
pretty-printing environment. No submitted environment-extension state crosses
this boundary. Each proxy is rechecked as an axiom before it is used to let
Lean inspect types while printing another type.
-/
private partial def addTypeProxy
    (rawEnv : Environment) (name : Name) : StateT (Environment × NameSet) IO Unit := do
  let (env, seen) ← get
  if env.contains name || seen.contains name then
    return
  let some dependency := rawEnv.find? name
    | throw <| IO.userError s!"audit declaration depends on missing constant {name}"
  set (env, seen.insert name)
  for used in dependency.type.getUsedConstants do
    addTypeProxy rawEnv used
  let (env, seen) ← get
  let proxy : Declaration := .axiomDecl {
    name := dependency.name
    levelParams := dependency.levelParams
    type := dependency.type
    isUnsafe := false
  }
  let ctx : Core.Context := {
    fileName := "<palomar-core-notation-audit-proxy>"
    fileMap := default
  }
  let (_, state) ← (Lean.addDecl proxy).toIO ctx { env }
  set (state.env, seen)

private def addTypeProxies
    (rawEnv : Environment) (trustedEnv : Environment) (roots : Array Name) : IO Environment := do
  let (_, env, _) ← StateT.run (roots.forM (addTypeProxy rawEnv)) (trustedEnv, {})
  return env

private def printSignature (env : Environment) (name : Name) : IO String := do
  unless env.contains name do
    throw <| IO.userError s!"audit declaration is missing: {name}"
  let opts := ({} : Options)
    |>.set `pp.notation false
    |>.set `pp.fullNames true
    |>.set `pp.fieldNotation false
  let ctx : Core.Context := {
    fileName := "<palomar-core-notation-audit>"
    fileMap := default
    options := opts
  }
  let state : Core.State := { env }
  let (signature, _) ← (PrettyPrinter.ppSignature name).run' {} |>.toIO ctx state
  return signature.fmt.pretty

unsafe def main (args : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  let (moduleName, requests) ← parseRequests args

  -- `loadExts := false` is the security boundary: imported delaborators,
  -- unexpanders, formatters, and other author-controlled extensions remain
  -- serialized data and are never initialized or evaluated.
  let rawEnv ← importModules (loadExts := false) #[{ module := moduleName }] {} 0

  -- Loading the trusted Lean environment second restores only the toolchain's
  -- own pretty-printer extensions. The raw import deliberately initializes no
  -- environment extensions, including Lean's.
  enableInitializersExecution
  let coreEnv ← importModules (loadExts := true) #[{ module := `Lean }] {} 0
  let env ← addTypeProxies rawEnv coreEnv (requests.map (fun request => request.2.2))

  let mut rows : Array AuditRow := #[]
  let mut totalBytes := 0
  for (kind, nameText, name) in requests do
    let declaration := s!"{kind} {← printSignature env name}"
    totalBytes := totalBytes + declaration.utf8ByteSize
    if totalBytes > 512 * 1024 then
      throw <| IO.userError "audit declarations exceed the 512 KiB limit"
    rows := rows.push { name := nameText, declaration }
  IO.println (toJson rows).compress
  return 0
